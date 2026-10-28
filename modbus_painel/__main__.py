"""Linha de comando: ler, descobrir units, abrir o painel ou subir o simulador."""
import argparse
import sys
import time

from . import __version__
from .cliente import FUNCOES, ClienteModbus, ErroModbus
from .painel import Leitor, carregar, servir
from .simulador import Simulador


def _mostrar(valores, inicio, funcao):
    print(f"  {FUNCOES[funcao]} a partir de {inicio}:")
    for i in range(0, len(valores), 8):
        print("   " + "  ".join(f"{inicio + i + j:>5}: {v:<6}" for j, v in enumerate(valores[i:i + 8])))


def cmd_ler(a):
    with ClienteModbus(a.host, a.porta, a.unit) as cli:
        while True:
            hora = time.strftime("%H:%M:%S")
            try:
                valores = cli.ler(a.endereco, a.quantidade, a.funcao)
                print(f"[{hora}] {a.host}:{a.porta} unit {a.unit}")
                _mostrar(valores, a.endereco, a.funcao)
                ok = True
            except ErroModbus as e:
                print(f"[{hora}] erro: {e}")
                ok = False
            if not a.repetir:
                return 0 if ok else 1
            time.sleep(a.repetir)


def cmd_units(a):
    print(f"procurando unit ids em {a.host}:{a.porta} (função {a.funcao}, endereço {a.endereco})…")
    with ClienteModbus(a.host, a.porta, timeout=1.0, tentativas=1) as cli:
        achados = cli.descobrir_units(a.endereco, a.funcao)
    print("  responderam: " + (", ".join(map(str, achados)) if achados else "nenhum"))
    return 0 if achados else 1


def cmd_painel(a):
    cfg = carregar(a.config)
    servir(cfg, Leitor(cfg), a.ouvir, a.porta_web)


def cmd_simular(a):
    sim = Simulador(a.ouvir, a.porta, units=a.units)
    print(f"simulador Modbus em {a.ouvir}:{a.porta}, units {a.units} (Ctrl+C para parar)")
    try:
        sim.serve_forever()
    except KeyboardInterrupt:
        pass


def main(argv=None):
    p = argparse.ArgumentParser(prog="modbus-painel", description="Leitura Modbus TCP e painel web ao vivo.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="comando", required=True)

    def conexao(sp, porta=502):
        sp.add_argument("host")
        sp.add_argument("-p", "--porta", type=int, default=porta)
        sp.add_argument("-f", "--funcao", type=int, default=3, choices=[1, 2, 3, 4],
                        help="1=coils 2=entradas digitais 3=holding 4=input (padrão 3)")
        sp.add_argument("-a", "--endereco", type=int, default=0)

    sp = sub.add_parser("ler", help="lê registradores e mostra na tela")
    conexao(sp)
    sp.add_argument("-u", "--unit", type=int, default=1)
    sp.add_argument("-n", "--quantidade", type=int, default=10)
    sp.add_argument("-r", "--repetir", type=float, metavar="SEG", help="repete a cada SEG segundos")
    sp.set_defaults(func=cmd_ler)

    sp = sub.add_parser("units", help="descobre quais unit ids respondem (útil atrás de gateway)")
    conexao(sp)
    sp.set_defaults(func=cmd_units)

    sp = sub.add_parser("painel", help="abre o painel web a partir de um arquivo TOML")
    sp.add_argument("config")
    sp.add_argument("--ouvir", default="127.0.0.1", help="endereço do painel (0.0.0.0 para a rede)")
    sp.add_argument("--porta-web", type=int, default=8899)
    sp.set_defaults(func=cmd_painel)

    sp = sub.add_parser("simular", help="sobe uma extrusora simulada para testar sem CLP")
    sp.add_argument("--ouvir", default="127.0.0.1")
    sp.add_argument("-p", "--porta", type=int, default=5020)
    sp.add_argument("--units", type=int, nargs="+", default=[1])
    sp.set_defaults(func=cmd_simular)

    a = p.parse_args(argv)
    try:
        return a.func(a)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
