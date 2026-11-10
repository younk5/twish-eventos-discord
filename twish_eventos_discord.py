# -*- coding: utf-8 -*-
"""Avisa no Discord quando um evento do Twish começa, muda ou termina.

O script lê dois endpoints públicos do Twish, os mesmos que o site consome:

    /api/{canal}/events/active  -> evento da hora + grande evento
    /api/{canal}/shop           -> desconto da loja

Só leitura. Nada de login, nada de pescar sozinho: ele olha, traduz e repassa o
aviso. O estado fica em ``eventos_discord_estado.json``, então reiniciar o script
não repete mensagem. O webhook nunca é gravado no estado, no log ou na saída.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

__version__ = "1.0.0"

API_BASE = "https://twish.com.br/api"
SITE = "https://twish.com.br"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
INTERVALO_PADRAO = 60
LIMITE_DISCORD = 1900
ESTADO_PADRAO = Path("eventos_discord_estado.json")

log = logging.getLogger("twish_eventos")

# O que cada kind significa na prática, escrito a partir do schema real. Se
# algum campo não veio, o texto não completa por conta própria: quem avisa isso
# é a linha de dados incompletos.
KINDS = {
    "double_xp": ("Bônus de XP", "Toda pescaria com peixe rende o dobro de pontos e XP.",
                  "Priorize pescar agora; é a melhor janela para evoluir de nível."),
    "store_discount": ("Desconto na Loja", "Desconto aplicado nos itens da loja.",
                       "Se o saldo cobre o equipamento, compre agora; fora do evento, espere."),
    "sell_multiplier": ("Venda Turbinada", "A venda de peixes paga um multiplicador.",
                        "Venda os peixes acumulados agora; é o melhor momento para moedas."),
    "tier_catch_boost": ("Sorte Rara", "A chance de uma raridade aumenta por um tempo.",
                         "Use turbo/isca só se a raridade sorteada for Secreto; senão guarde."),
    "double_twishcoins": ("Coins em Dobro", "Bits e bônus de apoio rendem o dobro de twishcoins.",
                          "Não muda a pesca; aproveite se for apoiar o canal."),
    "cooldown_multiplier": ("Cooldown especial", "O cooldown do $pescar muda durante o evento.",
                            "Continue pescando; o cooldown informado vale durante o evento."),
    "cooldown_reset": ("Cooldown zerado", "O cooldown do $pescar foi zerado.",
                       "Aproveite para lançar; a próxima janela já está liberada."),
    "catch_contest": ("Disputa de peixe", "Vale o primeiro peixe de cada jogador.",
                      "Pesque com $pescar e tente o peixe mais próximo do alvo."),
    "catch_pot": ("Pote de capturas", "Cada peixe coloca parte do valor num pote.",
                  "Pesque com $pescar; o pote é dividido entre quem participou."),
    "species_boost": ("Cardume passageiro", "Algumas espécies mordem mais e pagam bônus.",
                      "Pesque com $pescar enquanto o cardume estiver no barco."),
    "catch_drop": ("Garrafa ao Mar", "A primeira lançada pode trazer uma garrafa.",
                   "Faça a primeira lançada; depois o bônus não acumula."),
    "species_buyer": ("Comprador do Porto", "Algumas espécies pagam mais na venda.",
                      "Venda essas espécies pelo site; bônus de venda automática não conta."),
    "fishing_frozen": ("Pesca bloqueada", "$pescar fica bloqueado no canal.",
                       "Não insista: espere o evento terminar."),
    "bite_factor": ("Fisgada mais difícil", "A chance de fisgar cai durante o evento.",
                    "Se possível, use uma isca que garanta a fisgada."),
    "wash_away": ("Onda", "A onda leva parte das lançadas.",
                  "Peixes de espécie protegida ficam a salvo; não venda os protegidos."),
}

RELEVANCIA = {
    "alta": ("Alta", 3),
    "media": ("Média", 2),
    "baixa": ("Baixa", 1),
    "desconhecida": ("Desconhecida", 0),
}

# Traduz apenas os ids de raridade que a API já mandou. Raridade nova entra
# aqui no dia em que aparecer.
TIER_NOMES = {
    "common": "Comum", "comum": "Comum",
    "uncommon": "Incomum", "incomum": "Incomum",
    "rare": "Raro", "raro": "Raro",
    "legendary": "Lendário", "lendario": "Lendário",
    "mythic": "Mítico", "mitico": "Mítico",
    "secret": "Secreto", "secreto": "Secreto",
    "divine": "Divino", "divino": "Divino",
    "ancient_god": "Deus Ancião", "deus_anciao": "Deus Ancião",
}

Fetcher = Callable[[str, str], Any]
Enviador = Callable[..., bool]


# ---------------------------------------------------------------------------
# Formatação
# ---------------------------------------------------------------------------

def _agora_str(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time()).strftime("%d/%m/%Y %H:%M:%S")


def _fmt_duracao(segundos: float | None) -> str:
    if segundos is None:
        return "não informada"
    s = max(0, int(round(segundos)))
    h, resto = divmod(s, 3600)
    m, _ = divmod(resto, 60)
    if h:
        return "%dh%02d" % (h, m)
    return "%d min" % max(1, m)


def _fmt_quando(ts: float | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def _valor_formatado(valor: Any) -> str:
    try:
        f = float(valor)
    except (TypeError, ValueError):
        return ""
    if f >= 1000:
        return ("%.0f" % f).replace(".", ",")
    return ("%.2f" % f).rstrip("0").rstrip(".").replace(".", ",")


def _numero(valor: Any) -> float | None:
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def _pct(valor: Any) -> int | None:
    """Taxa chega como 0,2 num endpoint e 20 no outro. Devolve 20 nos dois casos."""
    n = _numero(valor)
    if n is None or n <= 0:
        return None
    return int(round(n * 100)) if n <= 1 else int(round(n))


def _sem_acento(texto: Any) -> str:
    tabela = str.maketrans("áàâãäéèêëíìîïóòôõöúùûüç",
                           "aaaaaeeeeiiiiooooouuuuc")
    return str(texto or "").translate(tabela).lower().strip()


# ---------------------------------------------------------------------------
# Normalização da API
# ---------------------------------------------------------------------------

def _tiers_nomes(evento: dict) -> list[str]:
    """Junta os nomes de raridade de ``tiers`` e ``target_tier_label``, sem repetir."""
    nomes: list[str] = []
    for item in evento.get("tiers") or []:
        if isinstance(item, dict):
            bruto = (item.get("label") or item.get("name") or
                     item.get("nome") or item.get("id"))
            chave = str(bruto or "").strip().lower()
            nome = (item.get("label") or item.get("name") or item.get("nome") or
                    TIER_NOMES.get(chave, bruto))
        else:
            chave = str(item or "").strip().lower()
            nome = TIER_NOMES.get(chave, item)
        if nome:
            nomes.append(str(nome))
    alvo = evento.get("target_tier_label")
    if alvo:
        nomes.append(str(alvo))
    vistos: set[str] = set()
    saida: list[str] = []
    for nome in nomes:
        chave = _sem_acento(nome)
        if chave and chave not in vistos:
            vistos.add(chave)
            saida.append(nome)
    return saida


def _especies_nomes(evento: dict) -> list[str]:
    nomes: list[str] = []
    for item in evento.get("species") or []:
        if isinstance(item, dict):
            nome = (item.get("name") or item.get("nome") or
                    item.get("label") or item.get("id"))
        else:
            nome = item
        if nome:
            nomes.append(str(nome))
    vistos: set[str] = set()
    saida: list[str] = []
    for nome in nomes:
        chave = _sem_acento(nome)
        if chave and chave not in vistos:
            vistos.add(chave)
            saida.append(nome)
    return saida


def _resultado_evento(relevancia: str, motivo: str, recomendacao: str = "",
                      faltando: list[str] | None = None) -> dict:
    rotulo, prioridade = RELEVANCIA.get(relevancia, RELEVANCIA["desconhecida"])
    falta = list(faltando or [])
    return {
        "relevancia": relevancia,
        "relevancia_label": rotulo,
        "prioridade": prioridade,
        "motivo": motivo,
        "recomendacao": recomendacao,
        "dados_suficientes": not falta,
        "faltando": falta,
    }


def _avaliar_evento(evento: dict, taxa_loja: float | None = None) -> dict:
    """Diz o quão relevante é o evento e o que dá para recomendar com os dados.

    Quando falta número para justificar a sugestão, a sugestão vira cautela e o
    campo ``faltando`` conta o que a API não mandou.
    """
    kind = str(evento.get("kind") or "")
    valor = _numero(evento.get("value"))
    tiers = _tiers_nomes(evento)
    especies = _especies_nomes(evento)
    nomes_tiers = ", ".join(tiers)

    if kind == "sell_multiplier":
        if valor is not None and valor > 0:
            mult = _valor_formatado(valor)
            return _resultado_evento(
                "alta",
                "A API informou multiplicador de venda de %sx." % mult,
                "Vale vender o estoque acumulado agora: cada peixe paga %sx o "
                "preço normal." % mult)
        return _resultado_evento(
            "alta",
            "Evento de venda turbinada ativo, sem multiplicador informado pela API.",
            "Vale vender o estoque acumulado agora; não dá para estimar o ganho "
            "porque a API não informou o multiplicador.",
            ["multiplicador de venda"])

    if kind == "store_discount":
        pct = _pct(evento.get("value"))
        if pct is None:
            pct = _pct(taxa_loja)
        if pct is not None:
            return _resultado_evento(
                "alta",
                "Desconto real de %d%% informado pela API." % pct,
                "Vale comprar equipamento agora. Confira o preço final antes de fechar.")
        return _resultado_evento(
            "media",
            "Evento de desconto ativo, mas a API não informou a taxa.",
            "Não recomendo comprar às cegas: confira a taxa real e o preço na "
            "loja antes.",
            ["taxa de desconto"])

    if kind == "tier_catch_boost":
        if not tiers:
            return _resultado_evento(
                "desconhecida",
                "A API não informou qual raridade foi sorteada.",
                "Não dá para recomendar turbo/isca sem saber a raridade. "
                "Confira a Sorte Rara no site.",
                ["raridade sorteada"])
        if any("secreto" in _sem_acento(t) for t in tiers):
            if valor is not None and valor > 0:
                return _resultado_evento(
                    "alta",
                    "Raridade sorteada: %s. A API informa chance %sx."
                    % (nomes_tiers, _valor_formatado(valor)),
                    "Vale usar turbo/isca agora: o alvo real é Secreto.")
            return _resultado_evento(
                "alta",
                "Raridade sorteada: %s." % nomes_tiers,
                "Vale usar turbo/isca agora: o alvo real é Secreto.",
                ["multiplicador da chance"])
        if any(_sem_acento(t) in ("lendario", "raro") for t in tiers):
            return _resultado_evento(
                "baixa",
                "Raridade sorteada: %s; não é Secreto." % nomes_tiers,
                "Não gaste turbo/isca neste evento: guarde para uma Sorte Rara "
                "de Secreto.")
        return _resultado_evento(
            "baixa",
            "Raridade sorteada: %s; não é Secreto." % nomes_tiers,
            "Não gaste turbo/isca sem justificativa; a raridade sorteada não é Secreto.")

    if kind == "double_xp":
        return _resultado_evento(
            "alta",
            "Evento de XP em dobro informado pela API.",
            "Priorize pescar agora: é a melhor janela para evoluir de nível.")

    if kind == "double_twishcoins":
        return _resultado_evento(
            "baixa",
            "Afeta apoio ao canal, não a pesca nem a venda de peixes.",
            "Não muda a pesca; só considere se for apoiar o canal.")

    if kind == "cooldown_multiplier":
        if valor is not None and valor > 0:
            return _resultado_evento(
                "media",
                "A API informou cooldown de %sx." % _valor_formatado(valor),
                "Continue pescando durante o evento; não compre nada por causa dele.")
        return _resultado_evento(
            "media",
            "O cooldown muda, mas a API não informou o multiplicador.",
            "Continue pescando e confira o $cooldown; não compre nada por causa "
            "deste evento.",
            ["multiplicador de cooldown"])

    if kind == "cooldown_reset":
        return _resultado_evento(
            "alta",
            "A API indica cooldown zerado.",
            "Aproveite para lançar agora com $pescar; não há custo.")

    if kind == "catch_contest":
        return _resultado_evento(
            "media",
            "Disputa ativa; vale o primeiro peixe de cada jogador.",
            "Pesque com $pescar; o evento não pede compra.")

    if kind == "catch_pot":
        return _resultado_evento(
            "media",
            "Pote de capturas ativo; cada peixe soma ao pote.",
            "Pesque com $pescar enquanto durar; não há custo de item.")

    if kind == "species_boost":
        if especies:
            return _resultado_evento(
                "media",
                "Espécies listadas pela API: %s." % ", ".join(especies),
                "Pesque com $pescar enquanto durar; priorize as espécies listadas.")
        return _resultado_evento(
            "desconhecida",
            "A API não informou quais espécies estão em alta.",
            "Não dá para dizer quais peixes priorizar; acompanhe o evento no site.",
            ["espécies do cardume"])

    if kind == "catch_drop":
        return _resultado_evento(
            "media",
            "A primeira lançada tem bônus; depois não acumula.",
            "Faça a primeira lançada com $pescar; o bônus não acumula.")

    if kind == "species_buyer":
        if especies:
            return _resultado_evento(
                "media",
                "Espécies com preço maior na venda: %s." % ", ".join(especies),
                "Venda essas espécies pelo site; bônus de venda automática não "
                "conta para este evento.")
        return _resultado_evento(
            "desconhecida",
            "A API não informou quais espécies pagam mais.",
            "Não venda às cegas: confira as espécies do Comprador do Porto no site.",
            ["espécies com bônus"])

    if kind == "fishing_frozen":
        return _resultado_evento(
            "alta",
            "A API indica que $pescar está bloqueado.",
            "Não insista; espere o evento terminar.")

    if kind == "bite_factor":
        motivo = "A API indica fisgada mais difícil."
        if valor is not None and valor > 0:
            motivo += " Fator informado: %s." % _valor_formatado(valor)
        return _resultado_evento(
            "media",
            motivo,
            "Use uma isca que garanta a fisgada se já tiver; não compre por "
            "causa deste evento.")

    if kind == "wash_away":
        if especies:
            return _resultado_evento(
                "media",
                "A API lista as espécies afetadas: %s." % ", ".join(especies),
                "Não venda as espécies listadas enquanto a onda durar.")
        return _resultado_evento(
            "media",
            "Onda ativa; a API não informou quais espécies são afetadas.",
            "Não venda peixes por impulso durante a onda; confira o efeito no site.",
            ["espécies afetadas"])

    return _resultado_evento(
        "desconhecida",
        "Tipo de evento não reconhecido pela API (%s)." % (kind or "sem kind"),
        "Não dá para recomendar ação: acompanhe o evento no site.",
        ["tipo/efeito do evento"])


def _texto_evento(evento: dict, com_desconto: float | None = None) -> tuple[str, str, str]:
    kind = str(evento.get("kind") or "")
    nome, beneficio, recomendacao = KINDS.get(
        kind, (evento.get("name") or "Evento do Twish",
               evento.get("message") or "A API não informou o efeito deste evento.",
               ""))
    if evento.get("name"):
        nome = evento["name"]
    if evento.get("message"):
        beneficio = evento["message"]
    valor = _numero(evento.get("value"))
    if kind == "double_xp":
        beneficio = "Toda pescaria com peixe de verdade rende o dobro de pontos e XP."
    elif kind == "sell_multiplier":
        if valor is not None and valor > 0:
            beneficio = "A venda de peixes paga %sx o preço normal." % _valor_formatado(valor)
        else:
            beneficio = "A venda de peixes paga um multiplicador; a API não informou o valor."
    elif kind == "double_twishcoins":
        beneficio = "Bits e bônus de apoio rendem o dobro de twishcoins."
    elif kind == "tier_catch_boost":
        alvo = ", ".join(_tiers_nomes(evento)) or "uma raridade"
        if valor is not None and valor > 0:
            beneficio = "A chance de %s aumenta %sx durante o evento." % (
                alvo, _valor_formatado(valor))
        else:
            beneficio = ("A chance de %s aumenta durante o evento; a API não "
                         "informou o multiplicador." % alvo)
    elif kind == "store_discount":
        pct = _pct(evento.get("value"))
        if pct is None:
            pct = _pct(com_desconto)
        if pct is not None:
            beneficio = "Desconto de %d%% nos itens da loja." % pct
    return nome, beneficio, recomendacao


def _fingerprint(dado: dict) -> str:
    """Muda só quando o evento muda de verdade.

    ``seconds_remaining`` fica de fora de propósito. Se entrasse, cada coleta
    viraria uma alteração e o canal só veria contagem regressiva.
    """
    estavel = {
        "kind": dado.get("kind"),
        "name": dado.get("name"),
        "message": dado.get("message"),
        "value": dado.get("value"),
        "phase": dado.get("phase"),
        "target_tier_label": dado.get("target_tier_label"),
        "effects": dado.get("effects"),
        "species": [s.get("id") if isinstance(s, dict) else s
                    for s in (dado.get("species") or [])],
        "tiers": [t.get("id") if isinstance(t, dict) else t
                  for t in (dado.get("tiers") or [])],
        "loot": dado.get("loot"),
        "ransom": dado.get("ransom"),
    }
    return json.dumps(estavel, ensure_ascii=False, sort_keys=True)


def extrair_ativos(payload: dict | None, loja: dict | None,
                   agora: float | None = None) -> list[dict]:
    """Transforma o JSON cru dos dois endpoints na lista que o resto do script usa."""
    agora = agora or time.time()
    payload = payload if isinstance(payload, dict) else {}
    loja = loja if isinstance(loja, dict) else {}
    itens: list[dict] = []
    tem_desconto_evento = False
    taxa_loja = _numero(loja.get("discount_rate"))
    if taxa_loja is not None and taxa_loja <= 0:
        taxa_loja = None

    for chave, dado in (("event", payload.get("event")),
                        ("grande", payload.get("grande_evento"))):
        if not isinstance(dado, dict):
            continue
        kind = str(dado.get("kind") or chave)
        nome, beneficio, _ = _texto_evento(dado, taxa_loja)
        avaliacao = _avaliar_evento(dado, taxa_loja)
        if kind == "store_discount":
            tem_desconto_evento = True
        try:
            seg = float(dado.get("seconds_remaining"))
        except (TypeError, ValueError):
            seg = None
        key = "%s:%s:%s" % (chave, kind, dado.get("name") or nome)
        itens.append({
            "key": key,
            "origem": chave,
            "kind": kind,
            "nome": nome,
            "emoji": dado.get("emoji") or "🎣",
            "beneficio": beneficio,
            **avaliacao,
            "segundos": seg,
            "ends_at": (agora + seg) if seg is not None else None,
            "fingerprint": _fingerprint(dado),
            "raw": dado,
        })

    # Desconto da loja é evento de verdade, só que às vezes aparece apenas em
    # /shop e não em /events/active. Se já veio como store_discount, não repete.
    if not tem_desconto_evento and taxa_loja is not None:
        try:
            seg = float(loja.get("discount_seconds_remaining"))
        except (TypeError, ValueError):
            seg = None
        dado = {"kind": "store_discount", "name": "Desconto na Loja",
                "value": taxa_loja, "seconds_remaining": seg}
        nome, beneficio, _ = _texto_evento(dado, taxa_loja)
        avaliacao = _avaliar_evento(dado, taxa_loja)
        itens.append({
            "key": "loja:store_discount:Desconto na Loja",
            "origem": "loja",
            "kind": "store_discount",
            "nome": nome,
            "emoji": "🏪",
            "beneficio": beneficio,
            **avaliacao,
            "segundos": seg,
            "ends_at": (agora + seg) if seg is not None else None,
            "fingerprint": _fingerprint(dado),
            "raw": dado,
        })
    return itens


def comparar(anteriores: dict, atuais: list[dict], agora: float | None = None) -> list[dict]:
    """Compara o estado salvo com a foto de agora e devolve o que avisar.

    As ações são ``inicio``, ``alteracao`` e ``fim``. O que já saiu no Discord não
    sai de novo, e quem controla isso é o campo ``notificado``.
    """
    agora = agora or time.time()
    atuais_por_key = {e["key"]: e for e in atuais}
    acoes: list[dict] = []
    for key, e in atuais_por_key.items():
        prev = anteriores.get(key)
        if not prev:
            acoes.append({"tipo": "inicio", "evento": e})
            continue
        if prev.get("notificado") != e["fingerprint"]:
            tipo = "inicio" if not prev.get("notificado") else "alteracao"
            acoes.append({"tipo": tipo, "evento": e})
            continue
        # Mesmo evento voltando depois do fim que estava previsto na coleta anterior.
        fim_prev = prev.get("ends_at")
        if fim_prev and agora > float(fim_prev) + 60 and e.get("ends_at") and \
                float(e["ends_at"]) > float(fim_prev) + 60:
            acoes.append({"tipo": "inicio", "evento": e})
    for key, prev in anteriores.items():
        if key in atuais_por_key:
            continue
        if prev.get("notificado"):
            acoes.append({"tipo": "fim", "evento": prev})
    return acoes


def mensagem(acao: dict, link: str = "") -> str:
    tipo = acao.get("tipo")
    e = acao.get("evento") or {}
    if tipo == "fim":
        cab = "⚪ **EVENTO TERMINOU NO TWISH**"
        status = "⚪ Encerrado"
    elif tipo == "alteracao":
        cab = "🔄 **EVENTO ATUALIZADO NO TWISH**"
        status = "🟡 Atualizado"
    else:
        cab = "🎣 **NOVO EVENTO NO TWISH!**"
        status = "🟢 Ativo"
    linhas = [
        cab, "",
        "**Evento:** %s %s" % (e.get("emoji") or "🎣", e.get("nome") or "Evento"),
        "**Status:** %s" % status,
    ]
    if e.get("relevancia_label"):
        linhas.append("**Relevância:** %s — %s"
                      % (e["relevancia_label"],
                         e.get("motivo") or "sem detalhe da API"))
    linhas.append("**Benefício:** %s" % (e.get("beneficio") or "Efeito especial temporário."))
    if tipo != "fim":
        seg = e.get("segundos")
        if seg is not None:
            linhas.append("**Duração:** ~%s (termina ~%s)"
                          % (_fmt_duracao(seg), _fmt_quando(e.get("ends_at"))))
        else:
            linhas.append("**Duração:** não informada pela API")
        if e.get("recomendacao"):
            linhas.append("💡 **Recomendação:** %s" % e["recomendacao"])
        if e.get("dados_suficientes") is False:
            falta = e.get("faltando") or []
            detalhe = (": %s" % ", ".join(falta)) if falta else ""
            linhas.append("⚠️ **Dados incompletos:** a API não informou%s. "
                          "A recomendação acima fica limitada ao que veio." % detalhe)
    else:
        linhas.append("**Duração:** encerrada")
    if link:
        linhas.extend(["", "🔗 %s" % link])
    return "\n".join(linhas)[:LIMITE_DISCORD]


def link_inventario(canal: str, jogador: str) -> str:
    if not (canal and jogador):
        return ""
    return "%s/c/%s/%s/inventory" % (SITE,
                                     urllib.parse.quote(canal),
                                     urllib.parse.quote(jogador))


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _redigir(texto: str) -> str:
    """Troca a URL do webhook por um rótulo antes de qualquer coisa ir para o log."""
    return (texto.replace("https://discord.com/api/webhooks/", "webhook:")
                 .replace("https://discordapp.com/api/webhooks/", "webhook:"))


def _get_json(caminho: str, base: str = API_BASE,
              tentativas: int = 2, timeout: float = 15) -> Any:
    url = base.rstrip("/") + caminho
    ultimo: Exception | None = None
    for i in range(max(1, tentativas)):
        req = urllib.request.Request(
            url,
            headers={"User-Agent": UA, "Accept": "application/json",
                     "Referer": "https://twish.com.br/"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError,
                TimeoutError, ValueError) as e:
            ultimo = e
            if i + 1 < tentativas:
                time.sleep(2.0 * (i + 1))
    raise RuntimeError("falha na API: %s" % str(ultimo)[:160])


def postar(texto: str, url: str | None = None, tentativas: int = 3) -> bool:
    """Envia ao Discord e insiste em erro de rede, 429 e 5xx.

    Em 429 respeita o ``Retry-After``. Devolve False quando desistiu; quem chamou
    decide se guarda o aviso para a próxima coleta.
    """
    url = (url or os.environ.get("DISCORD_WEBHOOK_URL", "")).strip()
    if not url:
        return False
    if not (url.startswith("https://discord.com/api/webhooks/")
            or url.startswith("https://discordapp.com/api/webhooks/")):
        log.error("webhook com formato inválido (não enviado)")
        return False
    corpo = json.dumps({
        "username": "Twish Eventos",
        "content": texto[:LIMITE_DISCORD],
        "allowed_mentions": {"parse": []},
    }).encode("utf-8")
    for i in range(max(1, tentativas)):
        req = urllib.request.Request(url, data=corpo, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": "TwishEventos/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                if 200 <= r.status < 300:
                    return True
        except urllib.error.HTTPError as e:
            if e.code == 429 and i + 1 < tentativas:
                espera = 3.0
                try:
                    espera = min(15.0, float(e.headers.get("Retry-After") or 3))
                except (TypeError, ValueError):
                    pass
                time.sleep(espera)
                continue
            log.warning("discord: HTTP %s%s", e.code,
                        " (nova tentativa)" if i + 1 < tentativas else "")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            log.warning("discord: falha de conexão%s (%s)",
                        " (nova tentativa)" if i + 1 < tentativas else "",
                        _redigir(str(e)[:100]))
        if i + 1 < tentativas:
            time.sleep(2.0 * (i + 1))
    return False


def testar_webhook(url: str | None = None) -> bool:
    texto = ("🎣 **TESTE DO MONITOR DE EVENTOS**\n\n"
             "Este webhook recebeu o teste. "
             "Os próximos eventos do Twish chegarão aqui automaticamente.")
    return postar(texto, url=url, tentativas=2)


# ---------------------------------------------------------------------------
# Estado
# ---------------------------------------------------------------------------

def _ler_estado(caminho: Path) -> dict:
    try:
        d = json.loads(Path(caminho).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _salvar_estado(caminho: Path, d: dict) -> None:
    caminho = Path(caminho)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    tmp = caminho.with_suffix(caminho.suffix + ".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, caminho)


# ---------------------------------------------------------------------------
# Coleta + envio
# ---------------------------------------------------------------------------

def observar_uma_vez(
    canal: str,
    jogador: str = "",
    webhook_url: str = "",
    estado_path: Path = ESTADO_PADRAO,
    base: str = API_BASE,
    fetcher: Fetcher = _get_json,
    enviar: Enviador = postar,
) -> dict:
    """Faz uma coleta, envia o que tiver e devolve o resumo em dicionário.

    É o mesmo resumo que o ``--uma-vez`` imprime e que o laço usa para escolher
    entre log de informação e log de aviso.
    """
    agora = time.time()
    resumo: dict = {"em": agora, "eventos": [], "enviados": 0, "erro": ""}
    canal_q = urllib.parse.quote(str(canal or "").strip())
    try:
        eventos = fetcher("/%s/events/active" % canal_q, base)
        try:
            loja = fetcher("/%s/shop" % canal_q, base)
        except RuntimeError as e:
            loja = {}
            log.info("loja indisponível (%s)", e)
    except RuntimeError as e:
        resumo["erro"] = str(e)[:180]
        estado = _ler_estado(estado_path)
        estado["ultimo_erro"] = resumo["erro"]
        estado["ultima_tentativa"] = agora
        _salvar_estado(estado_path, estado)
        log.warning("coleta falhou (%s)", resumo["erro"])
        return resumo

    atuais = extrair_ativos(eventos, loja, agora)
    estado = _ler_estado(estado_path)
    anteriores = estado.get("eventos") or {}
    pendentes = estado.get("fim_pendente") or {}
    atuais_keys = {e["key"] for e in atuais}

    # Evento que voltou não tem mais fim pendente para reenviar.
    for key in list(pendentes):
        if key in atuais_keys:
            pendentes.pop(key, None)

    # Primeiro os términos que ficaram pendentes. Aviso atrasado ainda serve;
    # aviso perdido, não.
    for key, prev in list(pendentes.items()):
        if enviar(mensagem({"tipo": "fim", "evento": prev}), url=webhook_url):
            resumo["enviados"] += 1
            pendentes.pop(key, None)
        else:
            resumo["erro"] = "falha ao enviar ao Discord; vou tentar de novo"

    acoes = comparar(anteriores, atuais, agora)
    # Ordem de envio: o que é relevante primeiro, término por último. No celular
    # a pessoa vê o que importa antes da notícia de que já acabou.
    acoes.sort(key=lambda a: (a.get("tipo") == "fim",
                              -int((a.get("evento") or {}).get("prioridade") or 0)))
    link = link_inventario(canal, jogador)
    notificados = dict(anteriores)
    for acao in acoes:
        e = acao["evento"]
        texto = mensagem(acao, link)
        if enviar(texto, url=webhook_url):
            resumo["enviados"] += 1
            if acao["tipo"] == "fim":
                notificados.pop(e.get("key"), None)
            else:
                notificados[e["key"]] = {
                    "key": e["key"], "nome": e["nome"], "kind": e["kind"],
                    "emoji": e.get("emoji"), "beneficio": e.get("beneficio"),
                    "relevancia": e.get("relevancia"),
                    "relevancia_label": e.get("relevancia_label"),
                    "prioridade": e.get("prioridade"),
                    "motivo": e.get("motivo"),
                    "recomendacao": e.get("recomendacao"),
                    "dados_suficientes": e.get("dados_suficientes"),
                    "faltando": e.get("faltando"),
                    "segundos": e.get("segundos"), "ends_at": e.get("ends_at"),
                    "fingerprint": e["fingerprint"], "notificado": e["fingerprint"],
                    "visto_em": agora,
                }
        else:
            resumo["erro"] = "falha ao enviar ao Discord; vou tentar de novo"
            if acao["tipo"] == "fim":
                pendentes[e.get("key")] = e

    # A foto atual e o que já foi avisado ficam separados. É essa diferença que
    # evita aviso repetido sem perder aviso.
    novos = {}
    for e in atuais:
        prev = anteriores.get(e["key"]) or {}
        novo = {
            "key": e["key"], "nome": e["nome"], "kind": e["kind"],
            "emoji": e.get("emoji"), "beneficio": e.get("beneficio"),
            "relevancia": e.get("relevancia"),
            "relevancia_label": e.get("relevancia_label"),
            "prioridade": e.get("prioridade"),
            "motivo": e.get("motivo"),
            "recomendacao": e.get("recomendacao"),
            "dados_suficientes": e.get("dados_suficientes"),
            "faltando": e.get("faltando"),
            "segundos": e.get("segundos"), "ends_at": e.get("ends_at"),
            "fingerprint": e["fingerprint"], "visto_em": agora,
            "notificado": prev.get("notificado"),
        }
        if prev.get("notificado") == e["fingerprint"]:
            novo["notificado"] = e["fingerprint"]
        elif e["key"] in notificados and \
                notificados[e["key"]].get("fingerprint") == e["fingerprint"]:
            novo["notificado"] = e["fingerprint"]
        novos[e["key"]] = novo

    estado.update({
        "em": agora,
        "ultima_coleta": agora,
        "eventos": novos,
        "fim_pendente": pendentes,
        "ultimo_erro": resumo["erro"],
        "url_configurada": bool((webhook_url or
                                 os.environ.get("DISCORD_WEBHOOK_URL", "")).strip()),
    })
    _salvar_estado(estado_path, estado)
    resumo["eventos"] = [{"nome": e["nome"], "kind": e["kind"],
                          "segundos": e.get("segundos"),
                          "relevancia": e.get("relevancia"),
                          "relevancia_label": e.get("relevancia_label"),
                          "recomendacao": e.get("recomendacao"),
                          "dados_suficientes": e.get("dados_suficientes"),
                          "faltando": e.get("faltando")} for e in atuais]
    return resumo


def resumo_status(estado_path: Path = ESTADO_PADRAO) -> dict:
    e = _ler_estado(estado_path)
    ativos = []
    for k, v in (e.get("eventos") or {}).items():
        if k.endswith(":fim_pendente"):
            continue
        ativos.append({"nome": v.get("nome"), "kind": v.get("kind"),
                       "segundos": v.get("segundos"),
                       "relevancia": v.get("relevancia"),
                       "relevancia_label": v.get("relevancia_label"),
                       "prioridade": v.get("prioridade"),
                       "motivo": v.get("motivo"),
                       "recomendacao": v.get("recomendacao"),
                       "dados_suficientes": v.get("dados_suficientes"),
                       "faltando": v.get("faltando")})
    return {
        "configurado": bool(_webhook_configurado()),
        "ultima_coleta": e.get("ultima_coleta") or e.get("em"),
        "ultimo_erro": e.get("ultimo_erro") or "",
        "ativos": ativos,
        "notificados": len([1 for v in (e.get("eventos") or {}).values()
                            if v.get("notificado")]),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _webhook_configurado(webhook_url: str | None = None) -> str:
    return (webhook_url or os.environ.get("DISCORD_WEBHOOK_URL", "")).strip()


def carregar_env(caminho: Path | None = None) -> dict[str, str]:
    """Lê um .env de chave=valor sem pisar no que já está no ambiente."""
    caminho = Path(caminho or ".env")
    dados: dict[str, str] = {}
    if not caminho.exists():
        return dados
    try:
        for linha in caminho.read_text(encoding="utf-8").splitlines():
            linha = linha.strip()
            if not linha or linha.startswith("#") or "=" not in linha:
                continue
            k, v = linha.split("=", 1)
            dados[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    for k, v in dados.items():
        os.environ.setdefault(k, v)
    return dados


def _status_texto(estado_path: Path) -> str:
    s = resumo_status(estado_path)
    linhas = ["webhook: " + ("configurado" if s["configurado"] else "faltando no ambiente"),
              "eventos ativos: %d" % len(s["ativos"])]
    if s["ultima_coleta"]:
        linhas.append("última coleta: " + _agora_str(s["ultima_coleta"]))
    for e in s["ativos"]:
        rotulo = e.get("relevancia_label") or "?"
        linhas.append(" - %s (%s) [%s]" % (e.get("nome"), e.get("kind"), rotulo))
        if e.get("recomendacao"):
            linhas.append("   recomendação: " + e["recomendacao"])
        if e.get("faltando"):
            linhas.append("   dados incompletos: " + ", ".join(e["faltando"]))
    if s["ultimo_erro"]:
        linhas.append("último erro: " + s["ultimo_erro"])
    return "\n".join(linhas)


def _saida_utf8() -> None:
    """Força UTF-8 na saída, senão o console do Windows engasga com os emojis."""
    for fluxo in (sys.stdout, sys.stderr):
        try:
            fluxo.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _loop(canal: str, jogador: str, webhook_url: str, estado_path: Path,
          intervalo: int, base: str) -> int:
    log.info("monitor iniciado (intervalo %ds, canal %s)", intervalo, canal)
    while True:
        try:
            r = observar_uma_vez(canal=canal, jogador=jogador,
                                 webhook_url=webhook_url,
                                 estado_path=estado_path, base=base)
            if r.get("enviados"):
                log.info("%d aviso(s) enviados ao Discord", r["enviados"])
            elif r.get("erro"):
                log.warning("%s", r["erro"])
        except KeyboardInterrupt:
            log.info("monitor encerrado")
            return 0
        except Exception as e:  # noqa: BLE001: erro bobo não pode derrubar um
            # monitor que fica dias rodando
            log.exception("erro no laço: %s", str(e)[:160])
        try:
            time.sleep(max(15, int(intervalo)))
        except KeyboardInterrupt:
            log.info("monitor encerrado")
            return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="twish-eventos",
        description="Publica no Discord os eventos ativos do Twish (somente leitura).",
    )
    parser.add_argument("--canal", help="canal do Twish (ou TWISH_CANAL)")
    parser.add_argument("--jogador", help="id do jogador, só para o link final "
                                          "(ou TWISH_JOGADOR)")
    parser.add_argument("--webhook", help="URL do webhook (ou DISCORD_WEBHOOK_URL)")
    parser.add_argument("--estado", help="arquivo de estado "
                                         "(padrão: eventos_discord_estado.json)")
    parser.add_argument("--intervalo", type=int,
                        default=int(os.environ.get("INTERVALO", INTERVALO_PADRAO)),
                        help="segundos entre coletas no --loop (padrão: 60)")
    parser.add_argument("--base", default=os.environ.get("TWISH_API_BASE", API_BASE),
                        help="base da API (padrão: %s)" % API_BASE)
    parser.add_argument("--sem-link", action="store_true",
                        help="não incluir o link do inventário nas mensagens")
    parser.add_argument("--env", default=".env", help="arquivo .env opcional")
    parser.add_argument("-v", "--verbose", action="store_true", help="log detalhado")

    modo = parser.add_mutually_exclusive_group()
    modo.add_argument("--loop", action="store_true", help="roda continuamente")
    modo.add_argument("--uma-vez", action="store_true", help="uma coleta e sai")
    modo.add_argument("--testar", action="store_true", help="envia mensagem de teste")
    modo.add_argument("--status", action="store_true", help="mostra o último estado")
    modo.add_argument("--dump", action="store_true",
                      help="imprime o JSON bruto dos endpoints e sai")
    args = parser.parse_args(argv)

    _saida_utf8()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    carregar_env(Path(args.env))
    canal = (args.canal or os.environ.get("TWISH_CANAL", "")).strip()
    jogador = (args.jogador or os.environ.get("TWISH_JOGADOR", "")).strip()
    webhook_url = (args.webhook or os.environ.get("DISCORD_WEBHOOK_URL", "")).strip()
    estado_path = Path(args.estado or ESTADO_PADRAO)

    if args.status:
        print(_status_texto(estado_path))
        return 0

    if args.testar:
        ok = testar_webhook(webhook_url)
        print("teste enviado" if ok else "falhou (veja o log; o webhook não é mostrado)")
        return 0 if ok else 1

    if not canal:
        print("erro: informe o canal com --canal ou TWISH_CANAL", file=sys.stderr)
        return 2

    if args.dump:
        for caminho in ("/%s/events/active" % urllib.parse.quote(canal),
                        "/%s/shop" % urllib.parse.quote(canal)):
            try:
                print(caminho)
                print(json.dumps(_get_json(caminho, args.base), ensure_ascii=False,
                                 indent=2))
            except RuntimeError as e:
                print("erro: %s" % e, file=sys.stderr)
                return 1
        return 0

    if args.uma_vez:
        r = observar_uma_vez(canal=canal, jogador="" if args.sem_link else jogador,
                             webhook_url=webhook_url, estado_path=estado_path,
                             base=args.base)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if not r.get("erro") else 1

    if webhook_url and not webhook_url.startswith(
            ("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")):
        print("erro: o webhook precisa ser uma URL do Discord", file=sys.stderr)
        return 2
    if not webhook_url:
        log.warning("webhook não configurado: nada será enviado (use --testar para conferir)")

    return _loop(canal=canal, jogador="" if args.sem_link else jogador,
                 webhook_url=webhook_url, estado_path=estado_path,
                 intervalo=args.intervalo, base=args.base)


if __name__ == "__main__":
    raise SystemExit(main())

