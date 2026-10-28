"""Cliente Modbus TCP em Python puro, somente leitura.

Mantém uma conexão aberta, numera as transações, junta respostas que chegam
em pedaços e reconecta sozinho quando o aparelho derruba a conexão (comum em
CLPs e gateways que fecham o socket depois de alguns segundos ociosos).
"""
import socket
import struct
import threading
import time

FUNCOES = {1: "coils", 2: "entradas digitais", 3: "holding registers", 4: "input registers"}

EXCECOES = {
    1: "função ilegal",
    2: "endereço ilegal",
    3: "valor ilegal",
    4: "falha no aparelho",
    5: "reconhecido, processando",
    6: "aparelho ocupado",
    10: "gateway sem caminho",
    11: "gateway: o alvo não respondeu",
}


class ErroModbus(Exception):
    """Falha de comunicação ou exceção devolvida pelo aparelho."""

    def __init__(self, mensagem, codigo=None):
        super().__init__(mensagem)
        self.codigo = codigo


class ClienteModbus:
    def __init__(self, host, porta=502, unit=1, timeout=3.0, tentativas=3):
        self.host = host
        self.porta = porta
        self.unit = unit
        self.timeout = timeout
        self.tentativas = tentativas
        self._sock = None
        self._tid = 0
        self._lock = threading.Lock()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.fechar()

    def fechar(self):
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _receber(self, n):
        dados = b""
        while len(dados) < n:
            pedaco = self._sock.recv(n - len(dados))
            if not pedaco:
                raise ConnectionError("o aparelho fechou a conexão")
            dados += pedaco
        return dados

    def _transacao(self, unit, funcao, endereco, quantidade):
        if self._sock is None:
            self._sock = socket.create_connection((self.host, self.porta), timeout=self.timeout)
        self._tid = (self._tid + 1) & 0xFFFF
        pdu = struct.pack(">BHH", funcao, endereco, quantidade)
        self._sock.sendall(struct.pack(">HHHB", self._tid, 0, len(pdu) + 1, unit) + pdu)

        # Cabeçalho MBAP (7 bytes) e depois o resto, do tamanho que ele anuncia.
        # Respostas de transações antigas (após um timeout) são descartadas.
        while True:
            tid, _, tamanho, _ = struct.unpack(">HHHB", self._receber(7))
            corpo = self._receber(tamanho - 1)
            if tid == self._tid:
                break

        if corpo[0] & 0x80:
            codigo = corpo[1]
            raise ErroModbus(f"exceção {codigo} ({EXCECOES.get(codigo, 'desconhecida')})", codigo)
        dados = corpo[2:2 + corpo[1]]
        if funcao in (1, 2):
            return [(byte >> bit) & 1 for byte in dados for bit in range(8)][:quantidade]
        return list(struct.unpack(f">{len(dados) // 2}H", dados))

    def ler(self, endereco, quantidade=1, funcao=3, unit=None):
        """Lê `quantidade` valores a partir de `endereco`. Funções 1, 2, 3 ou 4."""
        if funcao not in FUNCOES:
            raise ValueError(f"função {funcao} não suportada (use 1, 2, 3 ou 4)")
        limite = 2000 if funcao in (1, 2) else 125
        if not 1 <= quantidade <= limite:
            raise ValueError(f"quantidade deve ficar entre 1 e {limite}")
        unit = self.unit if unit is None else unit

        ultimo_erro = None
        with self._lock:
            for tentativa in range(self.tentativas):
                try:
                    return self._transacao(unit, funcao, endereco, quantidade)
                except ErroModbus:
                    raise  # o aparelho respondeu: repetir não muda nada
                except (OSError, ConnectionError, struct.error) as e:
                    ultimo_erro = e
                    self.fechar()
                    time.sleep(0.2 * (tentativa + 1))
        raise ErroModbus(f"sem resposta de {self.host}:{self.porta} ({ultimo_erro})")

    def descobrir_units(self, endereco=0, funcao=3, faixa=range(0, 248)):
        """Devolve os unit ids que respondem com dados. Útil atrás de gateways."""
        achados = []
        for unit in faixa:
            try:
                self.ler(endereco, 1, funcao, unit)
                achados.append(unit)
            except ErroModbus:
                pass
        return achados


def com_sinal(valor):
    """Converte um registrador de 16 bits para inteiro com sinal."""
    return valor - 0x10000 if valor & 0x8000 else valor


def para_32_bits(alto, baixo, sinal=False):
    """Junta dois registradores (palavra alta primeiro) num inteiro de 32 bits."""
    valor = (alto << 16) | baixo
    return valor - 0x1_0000_0000 if sinal and valor & 0x8000_0000 else valor
