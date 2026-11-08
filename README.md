# twish-eventos-discord

Avisa no Discord quando começa, muda ou termina um evento no [Twish](https://twish.com.br) — o jogo de pesca da Twitch.

O script consulta endpoints **públicos** do Twish, traduz o evento e publica uma mensagem pronta no seu webhook:

```
🎣 NOVO EVENTO NO TWISH!

Evento: 🏪 Desconto na Loja
Status: 🟢 Ativo
Relevância: Alta — Desconto real de 15% informado pela API.
Benefício: Desconto de 15% nos itens da loja.
Duração: ~45 min (termina ~21:30)
💡 Recomendação: Vale comprar equipamento agora, conferindo o preço com desconto.

🔗 https://twish.com.br/c/seucanal/1234567/inventory
```

## O que ele faz e o que ele não faz

- ✅ Lê `/api/{canal}/events/active` e `/api/{canal}/shop`.
- ✅ Avisa início, alteração e fim de evento, sem repetir depois de reiniciar.
- ✅ Mostra duração, relevância, recomendação e avisa quando a API manda dados incompletos.
- ❌ Não faz login, não lê chat, não joga, não automatiza nada.
- ❌ Não usa banco de dados, não precisa de token, não guarda o webhook em disco (só no `.env`).

Projeto não oficial, sem vínculo com o Twish.

## Requisitos

- Python 3.9 ou mais novo (só biblioteca padrão, nada para instalar).
- Um webhook do Discord: **Configurações do servidor → Integrações → Webhooks → Novo webhook**.

## Uso rápido

```bash
cp .env.example .env          # Windows: copy .env.example .env
# edite o .env com o webhook e o canal

python twish_eventos_discord.py --testar     # confere se o webhook recebe
python twish_eventos_discord.py --uma-vez    # uma coleta, imprime o resumo
python twish_eventos_discord.py --loop       # fica rodando (padrão)
```

Sem `.env`, dá para passar tudo por argumento:

```bash
python twish_eventos_discord.py \
  --canal meucanal --webhook "https://discord.com/api/webhooks/..." --loop
```

## Comandos

| Comando | O que faz |
|---|---|
| `--loop` | Monitora continuamente (padrão quando nenhum modo é passado). |
| `--uma-vez` | Faz uma coleta e envia o que houver; imprime um JSON com o resumo. |
| `--testar` | Envia uma mensagem de teste ao webhook. |
| `--status` | Mostra o último estado salvo, sem chamar a API. |
| `--dump` | Imprime o JSON bruto dos dois endpoints (útil para acompanhar mudança de schema). |

## Configuração

Variáveis de ambiente (ou `.env`):

| Variável | Obrigatória | Descrição |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | sim, para enviar | URL do webhook do Discord. |
| `TWISH_CANAL` | sim | Nome do canal do Twish (sem `#`). |
| `TWISH_JOGADOR` | não | ID do jogador, usado só no link final da mensagem. |
| `TWISH_API_BASE` | não | Base da API (padrão `https://twish.com.br/api`). |
| `INTERVALO` | não | Segundos entre coletas (padrão 60). |

Flags equivalentes: `--canal`, `--webhook`, `--jogador`, `--base`, `--intervalo`, `--estado`, `--sem-link`, `--env`, `-v`.

O webhook é validado pelo prefixo `https://discord.com/api/webhooks/` e **nunca** é escrito no estado ou no log: erros passam por um filtro que troca a URL por `webhook:`.

## Como funciona

1. **Coleta** a cada 60 s: evento da hora + grande evento em `/events/active`, e o desconto da loja em `/shop` (se a loja falhar, segue só com os eventos).
2. **Normaliza** tudo em uma lista de eventos com `key`, nome, emoji, benefício, segundos restantes e um `fingerprint` do conteúdo.
3. **Compara** com o estado anterior e gera três tipos de ação: `inicio`, `alteracao` e `fim`.
4. **Formata** a mensagem com relevância, recomendação e aviso de dados incompletos.
5. **Envia** ao Discord com até 3 tentativas, respeitando `Retry-After` em caso de 429. Se um aviso de fim falhar, fica em `fim_pendente` e é reenviado na coleta seguinte.

### Deduplicação

O `fingerprint` muda só quando o evento muda de verdade — o contador `seconds_remaining` é ignorado, então a contagem regressiva não gera spam. O estado fica em `eventos_discord_estado.json`:

- reiniciar o script não repete aviso;
- o mesmo evento voltando depois de `ends_at + 60 s` conta como novo;
- eventos que nunca chegaram a ser avisados não geram mensagem de "terminou".

### Relevância

A classificação é local, derivada do `kind` que a API devolve — não é um dado oficial:

| Nível | Exemplos |
|---|---|
| **Alta** | XP em dobro, venda turbinada, desconto na loja, cooldown zerado, Sorte Rara de Secreto, pesca bloqueada. |
| **Média** | Cooldown especial, pote de capturas, cardume, garrafa ao mar, comprador do porto, fisgada difícil, onda. |
| **Baixa** | Coins em dobro, Sorte Rara que não é Secreto. |
| **Desconhecida** | `kind` novo ou dado que a API não mandou. |

Se faltar informação (multiplicador, taxa, raridade, espécies), a mensagem não inventa: mostra `⚠️ Dados incompletos` com o que faltou.

### Tipos de evento reconhecidos

`double_xp`, `store_discount`, `sell_multiplier`, `tier_catch_boost`, `double_twishcoins`, `cooldown_multiplier`, `cooldown_reset`, `catch_contest`, `catch_pot`, `species_boost`, `catch_drop`, `species_buyer`, `fishing_frozen`, `bite_factor`, `wash_away`.

Um tipo novo não quebra nada: vira "Desconhecida" com o nome que a API mandou. Para adicionar suporte, inclua a entrada em `KINDS` e o caso em `_avaliar_evento`.

## Rodando como serviço

Linux (systemd):

```ini
[Unit]
Description=Twish Eventos Discord
After=network-online.target

[Service]
WorkingDirectory=/opt/twish-eventos-discord
ExecStart=/usr/bin/python3 twish_eventos_discord.py --loop
Restart=always

[Install]
WantedBy=multi-user.target
```

Windows: agendador de tarefas com `python twish_eventos_discord.py --loop`, ou um `.bat` na pasta de inicialização.

## Desenvolvimento

```bash
python -m unittest discover -s tests -t . -v
```

Os testes são offline: usam `fetcher` e `enviar` injetados, sem rede e sem webhook real.

Se a API mudar de schema, rode `--dump` e compare com os campos usados em `extrair_ativos`/`_avaliar_evento`.

## Licença

MIT — veja [LICENSE](LICENSE).
