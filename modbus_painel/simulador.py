"""Servidor Modbus TCP de mentira, para testar e demonstrar sem um CLP de verdade.

Simula uma extrusora: seis zonas de aquecimento que sobem até o ajuste,
rotação da rosca, corrente e pressão de massa, com um pouco de ruído.
Responde às funções 1, 2, 3 e 4. Um unit id fora da lista devolve a
exceção 11, como um gateway faria.
"""
import math
import random
import socketserver
import struct
import threading
import time

# Mapa de registradores do exemplo (exemplos/extrusora.toml usa os mesmos).
ZONAS_PV = range(100, 106)       # temperatura atual, °C
ZONAS_SV = range(110, 116)       # ajuste, °C
ROSCA_RPM = 120                  # rpm
ROSCA_CORRENTE = 121             # A × 10
PRESSAO_MASSA = 122              # bar
LIGADA = 0                       # coil 0: máquina ligada


class Extrusora:
    def __init__(self, ajustes=(160, 170, 180, 185, 190, 190)):
        self.ajustes = list(ajustes)
        self.inicio = time.monotonic()
        self.lock = threading.Lock()

    def registradores(self):
        t = time.monotonic() - self.inicio
        regs = {}
        for i, sv in enumerate(self.ajustes):
            subida = 1 - math.exp(-t / (20 + 5 * i))  # aquece devagar a partir de 25 °C
            pv = 25 + (sv - 25) * subida + random.uniform(-0.8, 0.8)
            regs[ZONAS_PV[i]] = round(pv)
            regs[ZONAS_SV[i]] = sv
        regs[ROSCA_RPM] = round(60 + 4 * math.sin(t / 7))
        regs[ROSCA_CORRENTE] = round(10 * (38 + 3 * math.sin(t / 5) + random.uniform(-0.5, 0.5)))
        regs[PRESSAO_MASSA] = round(180 + 15 * math.sin(t / 11))
        return regs

    def ler(self, funcao, endereco, quantidade):
        if funcao in (1, 2):
            return [1 if funcao == 1 and endereco + i == LIGADA else 0 for i in range(quantidade)]
        regs = self.registradores()
        return [regs.get(endereco + i, 0) for i in range(quantidade)]


class _Atendente(socketserver.BaseRequestHandler):
    def handle(self):
        maquina, units = self.server.maquina, self.server.units
        while True:
            cabecalho = self._receber(7)
            if not cabecalho:
                return
            tid, _, tamanho, unit = struct.unpack(">HHHB", cabecalho)
            pdu = self._receber(tamanho - 1)
            if not pdu:
                return
            funcao = pdu[0]
            if unit not in units:
                resposta = struct.pack(">BB", funcao | 0x80, 11)
            elif funcao not in (1, 2, 3, 4):
                resposta = struct.pack(">BB", funcao | 0x80, 1)
            else:
                endereco, quantidade = struct.unpack(">HH", pdu[1:5])
                valores = maquina.ler(funcao, endereco, quantidade)
                if funcao in (1, 2):
                    dados = bytes(
                        sum(bit << j for j, bit in enumerate(valores[i:i + 8]))
                        for i in range(0, len(valores), 8)
                    )
                else:
                    dados = struct.pack(f">{len(valores)}H", *valores)
                resposta = struct.pack(">BB", funcao, len(dados)) + dados
            self.request.sendall(struct.pack(">HHHB", tid, 0, len(resposta) + 1, unit) + resposta)

    def _receber(self, n):
        dados = b""
        while len(dados) < n:
            pedaco = self.request.recv(n - len(dados))
            if not pedaco:
                return b""
            dados += pedaco
        return dados


class Simulador(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, host="127.0.0.1", porta=5020, units=(1,), maquina=None):
        super().__init__((host, porta), _Atendente)
        self.units = set(units)
        self.maquina = maquina or Extrusora()

    def iniciar_em_segundo_plano(self):
        threading.Thread(target=self.serve_forever, daemon=True).start()
        return self
