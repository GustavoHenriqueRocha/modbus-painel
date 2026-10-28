"""Painel web ao vivo, montado a partir de um arquivo TOML.

Uma única thread lê o aparelho em sequência (CLPs pequenos não gostam de
várias conexões ao mesmo tempo) e junta registradores próximos numa leitura
só. Valor fora da faixa plausível é descartado e o último bom é mantido,
marcado como "antigo", para uma leitura com ruído não piscar na tela.
"""
import json
import threading
import time
import tomllib
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .cliente import ClienteModbus, ErroModbus, com_sinal

MAIOR_BLOCO = 120   # registradores por leitura (o limite do protocolo é 125)
MAIOR_BURACO = 10   # lê até 10 registradores inúteis para economizar uma ida e volta


@dataclass
class Valor:
    nome: str
    registrador: int
    unidade: str = ""
    escala: float = 1          # valor exibido = bruto / escala
    funcao: int = 3
    unit: int | None = None
    sinal: bool = False
    minimo: float | None = None
    maximo: float | None = None
    ajuste: int | None = None  # registrador do setpoint, se houver
    tolerancia: float | None = None


@dataclass
class Config:
    host: str
    porta: int = 502
    unit: int = 1
    intervalo: float = 3.0
    titulo: str = "Painel Modbus"
    grupos: dict[str, list[Valor]] = field(default_factory=dict)


def carregar(caminho):
    dados = tomllib.loads(Path(caminho).read_text(encoding="utf-8"))
    ap = dados["aparelho"]
    cfg = Config(
        host=ap["host"], porta=ap.get("porta", 502), unit=ap.get("unit", 1),
        intervalo=ap.get("intervalo", 3.0), titulo=dados.get("titulo", "Painel Modbus"),
    )
    for grupo in dados.get("grupo", []):
        cfg.grupos[grupo["nome"]] = [Valor(**v) for v in grupo.get("valor", [])]
    if not cfg.grupos:
        raise ValueError("o arquivo não tem nenhum [[grupo]] com [[grupo.valor]]")
    return cfg


def blocos(enderecos):
    """Agrupa endereços em faixas contínuas (inicio, quantidade) para ler de uma vez."""
    faixas = []
    for e in sorted(set(enderecos)):
        if faixas and e - faixas[-1][1] <= MAIOR_BURACO and e - faixas[-1][0] < MAIOR_BLOCO:
            faixas[-1][1] = e
        else:
            faixas.append([e, e])
    return [(ini, fim - ini + 1) for ini, fim in faixas]


class Leitor:
    def __init__(self, cfg, cliente=None):
        self.cfg = cfg
        self.cliente = cliente or ClienteModbus(cfg.host, cfg.porta, cfg.unit)
        self.estado = {"grupos": {}, "atualizado": None, "erro": None}
        self._bons = {}
        self._lock = threading.Lock()

    def _ler_tudo(self):
        pedidos = {}
        for valores in self.cfg.grupos.values():
            for v in valores:
                chave = (v.unit if v.unit is not None else self.cfg.unit, v.funcao)
                pedidos.setdefault(chave, []).append(v.registrador)
                if v.ajuste is not None:
                    pedidos[chave].append(v.ajuste)
        brutos, erros = {}, []
        for (unit, funcao), enderecos in pedidos.items():
            for inicio, qtd in blocos(enderecos):
                try:
                    lidos = self.cliente.ler(inicio, qtd, funcao, unit)
                except ErroModbus as e:
                    erros.append(f"unit {unit}, {inicio}–{inicio + qtd - 1}: {e}")
                    continue
                for i, bruto in enumerate(lidos):
                    brutos[(unit, funcao, inicio + i)] = bruto
        return brutos, erros

    def _converter(self, v, unit, registrador, brutos):
        bruto = brutos.get((unit, v.funcao, registrador))
        if bruto is None:
            return None
        if v.sinal:
            bruto = com_sinal(bruto)
        valor = bruto / v.escala
        return int(valor) if valor == int(valor) else round(valor, 2)

    def atualizar(self):
        brutos, erros = self._ler_tudo()
        grupos = {}
        for nome_grupo, valores in self.cfg.grupos.items():
            saida = []
            for v in valores:
                unit = v.unit if v.unit is not None else self.cfg.unit
                atual = self._converter(v, unit, v.registrador, brutos)
                plausivel = atual is not None and (v.minimo is None or atual >= v.minimo) \
                    and (v.maximo is None or atual <= v.maximo)
                antigo = False
                if plausivel:
                    self._bons[v.nome] = atual
                elif v.nome in self._bons:
                    atual, antigo = self._bons[v.nome], True
                else:
                    atual = None
                ajuste = self._converter(v, unit, v.ajuste, brutos) if v.ajuste is not None else None
                fora = (atual is not None and ajuste is not None and v.tolerancia is not None
                        and abs(atual - ajuste) > v.tolerancia)
                saida.append({"nome": v.nome, "valor": atual, "unidade": v.unidade,
                              "ajuste": ajuste, "antigo": antigo, "fora": fora})
            grupos[nome_grupo] = saida
        with self._lock:
            self.estado = {"grupos": grupos, "atualizado": time.strftime("%H:%M:%S"),
                           "erro": "; ".join(erros) or None}

    def instantaneo(self):
        with self._lock:
            return json.loads(json.dumps(self.estado))

    def rodar(self):
        while True:
            try:
                self.atualizar()
            except Exception as e:  # o painel nunca pode morrer por causa de uma leitura
                with self._lock:
                    self.estado["erro"] = str(e)
            time.sleep(self.cfg.intervalo)


PAGINA = (Path(__file__).parent / "pagina.html").read_text(encoding="utf-8")


def servir(cfg, leitor, host="127.0.0.1", porta=8899):
    pagina = PAGINA.replace("{{titulo}}", cfg.titulo).replace("{{intervalo}}", str(int(cfg.intervalo * 1000)))

    class Atendente(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path.startswith("/dados"):
                corpo, tipo = json.dumps(leitor.instantaneo()).encode(), "application/json"
            elif self.path in ("/", "/index.html"):
                corpo, tipo = pagina.encode(), "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", tipo)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(corpo)

    threading.Thread(target=leitor.rodar, daemon=True).start()
    servidor = ThreadingHTTPServer((host, porta), Atendente)
    print(f"painel em http://{host}:{porta}  (lendo {cfg.host}:{cfg.porta} a cada {cfg.intervalo:g} s)")
    servidor.serve_forever()
