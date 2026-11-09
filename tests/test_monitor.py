# -*- coding: utf-8 -*-
"""Testes que rodam sem rede: nada aqui chama a API nem um webhook de verdade."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import twish_eventos_discord as m


def payload_double_xp():
    return {
        "event": {
            "kind": "double_xp", "name": "XP em Dobro", "emoji": "⚡",
            "value": 2, "seconds_remaining": 600,
        },
        "grande_evento": None,
    }


def payload_sorte_rara(tier="secret", value=3):
    return {
        "event": {
            "kind": "tier_catch_boost", "name": "Sorte Rara",
            "tiers": [{"id": tier}], "target_tier_label": None,
            "value": value, "seconds_remaining": 900,
        },
    }


class TestNormalizacao(unittest.TestCase):
    def test_pct_aceita_taxa_e_porcentagem(self):
        self.assertEqual(m._pct(0.2), 20)
        self.assertEqual(m._pct(20), 20)
        self.assertIsNone(m._pct(0))
        self.assertIsNone(m._pct(None))

    def test_extrai_evento_e_desconto_da_loja(self):
        agora = 1_700_000_000.0
        itens = m.extrair_ativos(payload_double_xp(),
                                 {"discount_rate": 0.15,
                                  "discount_seconds_remaining": 300}, agora)
        kinds = {i["kind"] for i in itens}
        self.assertEqual(kinds, {"double_xp", "store_discount"})
        xp = next(i for i in itens if i["kind"] == "double_xp")
        self.assertEqual(xp["relevancia"], "alta")
        self.assertEqual(xp["ends_at"], agora + 600)
        loja = next(i for i in itens if i["kind"] == "store_discount")
        self.assertIn("15%", loja["beneficio"])

    def test_nao_duplica_desconto(self):
        itens = m.extrair_ativos(
            {"event": {"kind": "store_discount", "name": "Desconto na Loja",
                       "value": 0.1, "seconds_remaining": 60}},
            {"discount_rate": 0.1, "discount_seconds_remaining": 60})
        self.assertEqual(len(itens), 1)

    def test_grande_evento_tambem_entra(self):
        itens = m.extrair_ativos(
            {"event": None,
             "grande_evento": {"kind": "sell_multiplier", "value": 2,
                               "seconds_remaining": 120}}, {})
        self.assertEqual(itens[0]["origem"], "grande")
        self.assertIn("2x", itens[0]["beneficio"])

    def test_sorte_rara_secreto_alta_sem_secreto_baixa(self):
        secreto = m.extrair_ativos(payload_sorte_rara("secret"), {})[0]
        self.assertEqual(secreto["relevancia"], "alta")
        comum = m.extrair_ativos(payload_sorte_rara("common"), {})[0]
        self.assertEqual(comum["relevancia"], "baixa")

    def test_dado_ausente_vira_incompleto(self):
        item = m.extrair_ativos(
            {"event": {"kind": "tier_catch_boost", "seconds_remaining": 60}}, {})[0]
        self.assertFalse(item["dados_suficientes"])
        self.assertIn("raridade sorteada", item["faltando"])
        self.assertIn("Dados incompletos", m.mensagem({"tipo": "inicio", "evento": item}))


class TestDeduplicacao(unittest.TestCase):
    def test_contador_de_segundos_nao_gera_alteracao(self):
        antes = m.extrair_ativos(payload_double_xp(), {}, agora=100.0)
        depois = m.extrair_ativos(payload_double_xp(), {}, agora=160.0)
        estado = {e["key"]: {**e, "notificado": e["fingerprint"]} for e in antes}
        self.assertEqual(m.comparar(estado, depois, agora=160.0), [])

    def test_mudanca_de_valor_gera_alteracao(self):
        antes = m.extrair_ativos(payload_double_xp(), {}, agora=100.0)
        depois = m.extrair_ativos(payload_sorte_rara("secret", 5), {}, agora=160.0)
        estado = {e["key"]: {**e, "notificado": e["fingerprint"]} for e in antes}
        acoes = m.comparar(estado, depois, agora=160.0)
        tipos = sorted(a["tipo"] for a in acoes)
        self.assertEqual(tipos, ["fim", "inicio"])

    def test_evento_que_some_gera_fim(self):
        antes = m.extrair_ativos(payload_double_xp(), {}, agora=100.0)
        estado = {e["key"]: {**e, "notificado": e["fingerprint"]} for e in antes}
        acoes = m.comparar(estado, [], agora=200.0)
        self.assertEqual([a["tipo"] for a in acoes], ["fim"])

    def test_evento_que_nao_foi_notificado_nao_gera_fim(self):
        antes = m.extrair_ativos(payload_double_xp(), {}, agora=100.0)
        estado = {e["key"]: {**e, "notificado": None} for e in antes}
        self.assertEqual(m.comparar(estado, [], agora=200.0), [])


class TestMensagem(unittest.TestCase):
    def test_formato_completo(self):
        item = m.extrair_ativos(payload_double_xp(), {}, agora=1_700_000_000.0)[0]
        texto = m.mensagem({"tipo": "inicio", "evento": item},
                           m.link_inventario("canal", "123"))
        self.assertIn("NOVO EVENTO", texto)
        self.assertIn("Relevância:", texto)
        self.assertIn("Recomendação:", texto)
        self.assertIn("https://twish.com.br/c/canal/123/inventory", texto)

    def test_sem_link_quando_nao_informado(self):
        item = m.extrair_ativos(payload_double_xp(), {})[0]
        self.assertNotIn("http", m.mensagem({"tipo": "fim", "evento": item}))

    def test_respeita_limite_do_discord(self):
        item = m.extrair_ativos(payload_double_xp(), {})[0]
        item["recomendacao"] = "x" * 5000
        self.assertLessEqual(len(m.mensagem({"tipo": "inicio", "evento": item})), 1900)

    def test_link_so_com_canal_e_jogador(self):
        self.assertEqual(m.link_inventario("", "123"), "")
        self.assertEqual(m.link_inventario("canal", ""), "")


class TestEnvio(unittest.TestCase):
    def test_webhook_invalido_nao_tenta_rede(self):
        self.assertFalse(m.postar("oi", url="http://exemplo.com/hook"))
        self.assertFalse(m.postar("oi", url=""))

    def test_webhook_ausente_do_log(self):
        segredo = "https://discord.com/api/webhooks/123/abc"
        self.assertNotIn("discord.com", m._redigir("falhou em " + segredo))


class TestColetaCompleta(unittest.TestCase):
    def _fetcher(self, eventos, loja=None):
        def fake(caminho, base):
            return loja or {} if caminho.endswith("/shop") else eventos
        return fake

    def test_deduplica_entre_duas_coletas(self):
        enviados = []
        with tempfile.TemporaryDirectory() as tmp:
            estado = Path(tmp) / "estado.json"
            fetcher = self._fetcher(payload_double_xp(), {"discount_rate": 0.1})
            enviar = lambda texto, url=None: enviados.append(texto) or True
            r1 = m.observar_uma_vez("canal", "123", "https://discord.com/api/webhooks/x/y",
                                    estado, fetcher=fetcher, enviar=enviar)
            r2 = m.observar_uma_vez("canal", "123", "https://discord.com/api/webhooks/x/y",
                                    estado, fetcher=fetcher, enviar=enviar)
            self.assertEqual(r1["enviados"], 2)
            self.assertEqual(r2["enviados"], 0)
            self.assertEqual(len(enviados), 2)

    def test_fim_falho_fica_pendente_e_reenvia(self):
        enviados = []
        falhar = {"sim": True}

        def enviar(texto, url=None):
            if "TERMINOU" in texto and falhar["sim"]:
                return False
            enviados.append(texto)
            return True

        with tempfile.TemporaryDirectory() as tmp:
            estado = Path(tmp) / "estado.json"
            m.observar_uma_vez("canal", "", "https://discord.com/api/webhooks/x/y",
                               estado, fetcher=self._fetcher(payload_double_xp()),
                               enviar=enviar)
            r = m.observar_uma_vez("canal", "", "https://discord.com/api/webhooks/x/y",
                                   estado, fetcher=self._fetcher({}), enviar=enviar)
            self.assertIn("erro", r)
            dados = json.loads(estado.read_text(encoding="utf-8"))
            self.assertTrue(dados.get("fim_pendente"))
            falhar["sim"] = False
            r = m.observar_uma_vez("canal", "", "https://discord.com/api/webhooks/x/y",
                                   estado, fetcher=self._fetcher({}), enviar=enviar)
            self.assertEqual(r["enviados"], 1)
            dados = json.loads(estado.read_text(encoding="utf-8"))
            self.assertFalse(dados.get("fim_pendente"))

    def test_erro_de_api_nao_quebra_estado(self):
        def quebrado(caminho, base):
            raise RuntimeError("caiu")

        with tempfile.TemporaryDirectory() as tmp:
            estado = Path(tmp) / "estado.json"
            r = m.observar_uma_vez("canal", "", "", estado, fetcher=quebrado,
                                   enviar=lambda *a, **k: True)
            self.assertIn("caiu", r["erro"])
            dados = json.loads(estado.read_text(encoding="utf-8"))
            self.assertEqual(dados["ultimo_erro"], r["erro"])


if __name__ == "__main__":
    unittest.main()
