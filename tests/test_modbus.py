import io
import socket
import struct
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from modbus_painel.__main__ import main
from modbus_painel.cliente import ClienteModbus, ErroModbus, com_sinal, para_32_bits
from modbus_painel.painel import Config, Leitor, Valor, blocos, carregar
from modbus_painel.simulador import Simulador

RAIZ = Path(__file__).resolve().parent.parent


class TestCliente(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sim = Simulador("127.0.0.1", 0, units=(1, 3)).iniciar_em_segundo_plano()
        cls.porta = cls.sim.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.sim.shutdown()
        cls.sim.server_close()

    def cliente(self, **kw):
        return ClienteModbus("127.0.0.1", self.porta, **kw)

    def test_le_holding_registers(self):
        with self.cliente() as c:
            self.assertEqual(c.ler(110, 6), [160, 170, 180, 185, 190, 190])

    def test_le_coils(self):
        with self.cliente() as c:
            self.assertEqual(c.ler(0, 3, funcao=1), [1, 0, 0])

    def test_reaproveita_a_conexao(self):
        with self.cliente() as c:
            c.ler(100)
            sock = c._sock
            c.ler(101)
            self.assertIs(c._sock, sock)

    def test_unit_inexistente_devolve_excecao_do_gateway(self):
        with self.cliente(unit=9) as c:
            with self.assertRaises(ErroModbus) as ctx:
                c.ler(100)
        self.assertEqual(ctx.exception.codigo, 11)

    def test_reconecta_depois_que_o_aparelho_derruba(self):
        with self.cliente() as c:
            c.ler(100)
            c._sock.shutdown(socket.SHUT_RDWR)  # simula o CLP fechando o socket ocioso
            self.assertEqual(c.ler(110), [160])

    def test_descobre_units(self):
        with self.cliente(timeout=1, tentativas=1) as c:
            self.assertEqual(c.descobrir_units(faixa=range(0, 6)), [1, 3])

    def test_sem_aparelho_vira_erro_modbus(self):
        livre = socket.socket()
        livre.bind(("127.0.0.1", 0))
        porta = livre.getsockname()[1]
        livre.close()
        with ClienteModbus("127.0.0.1", porta, timeout=0.5, tentativas=2) as c:
            with self.assertRaises(ErroModbus):
                c.ler(0)

    def test_valida_parametros(self):
        c = self.cliente()
        with self.assertRaises(ValueError):
            c.ler(0, 126)
        with self.assertRaises(ValueError):
            c.ler(0, 1, funcao=6)  # escrita não existe: o cliente é só leitura

    def test_resposta_em_pedacos(self):
        """A resposta chega byte a byte, como acontece em links lentos."""
        servidor = socket.socket()
        servidor.bind(("127.0.0.1", 0))
        servidor.listen(1)

        def atender():
            conn, _ = servidor.accept()
            tid = struct.unpack(">H", conn.recv(12)[:2])[0]
            resposta = struct.pack(">HHHBBBH", tid, 0, 5, 1, 3, 2, 1234)
            for byte in resposta:
                conn.send(bytes([byte]))
            conn.close()

        threading.Thread(target=atender, daemon=True).start()
        with ClienteModbus("127.0.0.1", servidor.getsockname()[1], tentativas=1) as c:
            self.assertEqual(c.ler(0), [1234])
        servidor.close()


class TestConversoes(unittest.TestCase):
    def test_com_sinal(self):
        self.assertEqual(com_sinal(0xFFFF), -1)
        self.assertEqual(com_sinal(100), 100)

    def test_32_bits(self):
        self.assertEqual(para_32_bits(1, 2), 65538)
        self.assertEqual(para_32_bits(0xFFFF, 0xFFFE, sinal=True), -2)


class TestBlocos(unittest.TestCase):
    def test_junta_enderecos_proximos(self):
        self.assertEqual(blocos([100, 101, 105, 110]), [(100, 11)])

    def test_separa_enderecos_distantes(self):
        self.assertEqual(blocos([100, 300]), [(100, 1), (300, 1)])

    def test_nao_passa_do_limite_do_protocolo(self):
        self.assertTrue(all(qtd <= 125 for _, qtd in blocos(range(0, 400, 5))))


class ClienteFalso:
    """Devolve valores fixos e conta as leituras."""

    def __init__(self, regs):
        self.regs = regs
        self.leituras = 0

    def ler(self, inicio, qtd, funcao=3, unit=None):
        self.leituras += 1
        return [self.regs.get(inicio + i, 0) for i in range(qtd)]


class TestLeitor(unittest.TestCase):
    def leitor(self, regs, *valores):
        cfg = Config(host="x", grupos={"G": list(valores)})
        cli = ClienteFalso(regs)
        return Leitor(cfg, cli), cli

    def valor(self, leitor, i=0):
        leitor.atualizar()
        return leitor.instantaneo()["grupos"]["G"][i]

    def test_escala_e_sinal(self):
        l, _ = self.leitor({10: 385, 11: 0xFFF6},
                           Valor("Corrente", 10, escala=10), Valor("Delta", 11, sinal=True))
        l.atualizar()
        g = l.instantaneo()["grupos"]["G"]
        self.assertEqual((g[0]["valor"], g[1]["valor"]), (38.5, -10))

    def test_valor_implausivel_mantem_o_ultimo_bom(self):
        regs = {10: 180}
        l, _ = self.leitor(regs, Valor("Zona", 10, minimo=0, maximo=400))
        self.assertEqual(self.valor(l)["valor"], 180)
        regs[10] = 65000  # leitura com lixo
        v = self.valor(l)
        self.assertEqual((v["valor"], v["antigo"]), (180, True))

    def test_implausivel_sem_historico_fica_vazio(self):
        l, _ = self.leitor({10: 9999}, Valor("Zona", 10, maximo=400))
        self.assertIsNone(self.valor(l)["valor"])

    def test_alerta_fora_da_tolerancia(self):
        l, _ = self.leitor({10: 150, 20: 180}, Valor("Zona", 10, ajuste=20, tolerancia=8))
        v = self.valor(l)
        self.assertEqual((v["ajuste"], v["fora"]), (180, True))

    def test_uma_leitura_para_registradores_vizinhos(self):
        l, cli = self.leitor({}, *[Valor(f"Z{i}", 100 + i, ajuste=110 + i) for i in range(6)])
        l.atualizar()
        self.assertEqual(cli.leituras, 1)


class TestExemplo(unittest.TestCase):
    def test_exemplo_carrega_e_le_do_simulador(self):
        cfg = carregar(RAIZ / "exemplos" / "extrusora.toml")
        sim = Simulador("127.0.0.1", 0).iniciar_em_segundo_plano()
        try:
            cfg.porta = sim.server_address[1]
            leitor = Leitor(cfg)
            leitor.atualizar()
            estado = leitor.instantaneo()
            self.assertIsNone(estado["erro"])
            zonas = estado["grupos"]["Temperatura das zonas"]
            self.assertEqual([z["ajuste"] for z in zonas], [160, 170, 180, 185, 190, 190])
            self.assertTrue(all(z["valor"] is not None for z in zonas))
        finally:
            leitor.cliente.fechar()
            sim.shutdown()
            sim.server_close()

    def test_cli_ler(self):
        sim = Simulador("127.0.0.1", 0).iniciar_em_segundo_plano()
        try:
            saida = io.StringIO()
            with redirect_stdout(saida):
                codigo = main(["ler", "127.0.0.1", "-p", str(sim.server_address[1]), "-a", "110", "-n", "6"])
            self.assertEqual(codigo, 0)
            self.assertIn("110: 160", saida.getvalue())
        finally:
            sim.shutdown()
            sim.server_close()


if __name__ == "__main__":
    unittest.main()
