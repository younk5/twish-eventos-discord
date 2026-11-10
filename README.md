# twish-eventos-discord

Avisa no Discord quando começa, muda ou termina um evento no [Twish](https://twish.com.br), o jogo de pesca da Twitch.

A ideia é pequena: o Twish publica os eventos ativos em endpoints abertos, os mesmos que o site consome. O script lê esses endpoints de tempos em tempos, monta uma mensagem legível e joga no seu webhook. Não tem SDK, não tem token, não tem nada para instalar além do Python.

A mensagem sai assim:

```
🎣 **NOVO EVENTO NO TWISH!**

**Evento:** 🏪 Desconto na Loja
**Status:** 🟢 Ativo
**Relevância:** Alta — Desconto real de 15% informado pela API.
**Benefício:** Desconto de 15% nos itens da loja.
**Duração:** ~45 min (termina ~21:30)
💡 **Recomendação:** Vale comprar equipamento agora. Confira o preço final antes de fechar.

🔗 https://twish.com.br/c/seucanal/1234567/inventory
```

## O que ele faz

- Lê `/api/{canal}/events/active` (evento da hora e grande evento) e `/api/{canal}/shop` (desconto da loja).
- Posta quando o evento começa, quando o conteúdo muda e quando termina. Reiniciar o script no meio de um evento não repete nada.
- Mostra a duração, o quanto aquilo importa e o que dá para fazer. Se a API não mandou o número que justificaria a recomendação, ele avisa que faltou dado em vez de chutar.

## O que ele não faz

Não faz login, não lê o chat, não pesca por você. Não usa banco de dados e não precisa de token, só da URL do webhook no `.env`. O estado de uma coleta para a outra fica num JSON ao lado do script.

Projeto não oficial, sem vínculo com o Twish.

## Requisitos

Python 3.9 ou mais novo. Só biblioteca padrão, então não tem `pip install` nenhum para rodar.

Um webhook do Discord: Configurações do servidor → Integrações → Webhooks → Novo webhook.

## Uso rápido

```bash
cp .env.example .env          # Windows: copy .env.example .env
# preencha o webhook e o canal

python twish_eventos_discord.py --testar     # confere se o webhook recebe
python twish_eventos_discord.py --uma-vez    # uma coleta só, imprime o resumo
python twish_eventos_discord.py --loop       # fica rodando (é o padrão)
```

Se preferir não usar `.env`, tudo cabe em argumento:

```bash
python twish_eventos_discord.py \
  --canal meucanal --webhook "https://discord.com/api/webhooks/..." --loop
```

## Comandos

| Comando | O que faz |
|---|---|
| `--loop` | Monitora sem parar. É o padrão quando nenhum modo é passado. |
| `--uma-vez` | Faz uma coleta e envia o que houver; imprime um JSON com o resumo. |
| `--testar` | Manda uma mensagem de teste ao webhook. |
| `--status` | Mostra o último estado salvo, sem chamar a API. |
| `--dump` | Imprime o JSON bruto dos dois endpoints, útil quando o schema muda. |

## Configuração

Variáveis de ambiente, que podem vir do `.env`:

| Variável | Obrigatória | Descrição |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | sim, para enviar | URL do webhook do Discord. |
| `TWISH_CANAL` | sim | Nome do canal do Twish, sem `#`. |
| `TWISH_JOGADOR` | não | ID do jogador, usado só no link do fim da mensagem. |
| `TWISH_API_BASE` | não | Base da API (padrão `https://twish.com.br/api`). |
| `INTERVALO` | não | Segundos entre coletas (padrão 60). |

As flags equivalentes são `--canal`, `--webhook`, `--jogador`, `--base`, `--intervalo`, `--estado`, `--sem-link`, `--env` e `-v`.

O webhook é conferido pelo prefixo `https://discord.com/api/webhooks/` e nunca é escrito no estado nem no log: antes de qualquer erro sair no console, a URL já virou `webhook:`.

## Como funciona

Cada coleta, de 60 em 60 segundos por padrão, faz mais ou menos isto:

1. Pede `/events/active` e `/shop`. Se a loja cair, segue só com os eventos.
2. Normaliza a resposta numa lista de eventos, cada um com `key`, nome, emoji, benefício, segundos restantes e um `fingerprint` do conteúdo.
3. Compara com o estado anterior e separa o que virou `inicio`, `alteracao` ou `fim`.
4. Monta a mensagem com relevância, recomendação e, quando for o caso, o aviso de dados incompletos.
5. Envia ao Discord com até três tentativas, respeitando o `Retry-After` em 429. Se um aviso de fim falhar, ele fica em `fim_pendente` e é reenviado na coleta seguinte.

### Por que ele não spamma o canal

O `fingerprint` muda apenas quando o evento muda de verdade, e a contagem de segundos fica de fora dele. Sem isso, cada coleta viraria uma alteração e o canal receberia uma mensagem por minuto dizendo que faltam 43 minutos. O estado fica em `eventos_discord_estado.json`:

- reiniciar o script não repete aviso;
- o mesmo evento voltando depois de `ends_at + 60 s` conta como novo;
- evento que nunca chegou a ser avisado não gera mensagem de "terminou".

### Relevância

A classificação é local, montada a partir do `kind` que a API devolve. Não é dado oficial do jogo.

| Nível | Exemplos |
|---|---|
| **Alta** | XP em dobro, venda turbinada, desconto na loja, cooldown zerado, Sorte Rara de Secreto, pesca bloqueada. |
| **Média** | Cooldown especial, pote de capturas, cardume, garrafa ao mar, comprador do porto, fisgada difícil, onda. |
| **Baixa** | Coins em dobro, Sorte Rara que não é Secreto. |
| **Desconhecida** | `kind` novo ou dado que a API não mandou. |

Quando falta informação (multiplicador, taxa, raridade, espécies), a mensagem não inventa: mostra `⚠️ Dados incompletos` e diz o que ficou faltando.

### Tipos de evento reconhecidos

`double_xp`, `store_discount`, `sell_multiplier`, `tier_catch_boost`, `double_twishcoins`, `cooldown_multiplier`, `cooldown_reset`, `catch_contest`, `catch_pot`, `species_boost`, `catch_drop`, `species_buyer`, `fishing_frozen`, `bite_factor`, `wash_away`.

Tipo novo não quebra nada: ele entra como "Desconhecida", com o nome que a API mandou. Para dar suporte, inclua a entrada em `KINDS` e o caso em `_avaliar_evento`.

## Se algo der errado

- O `--testar` diz que falhou: quase sempre é webhook copiado pela metade. A URL inteira começa em `https://discord.com/api/webhooks/`.
- O `--status` volta vazio: ainda não houve coleta nenhuma. Rode `--uma-vez` primeiro.
- Evento aparecendo como "Desconhecida": ou o Twish criou um `kind` novo, ou mudou o schema. Rode `--dump` e veja o que está vindo.

## Rodando como serviço

Linux, com systemd:

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

No Windows dá para usar o agendador de tarefas com `python twish_eventos_discord.py --loop`, ou um `.bat` na pasta de inicialização.

## Desenvolvimento

```bash
python -m unittest discover -s tests -t . -v
```

Os testes rodam offline: `fetcher` e `enviar` são injetados, então não tem rede nem webhook de verdade no meio.

Se a API mudar de schema, rode `--dump` e compare com os campos usados em `extrair_ativos` e `_avaliar_evento`.

## Licença

MIT, veja [LICENSE](LICENSE).


