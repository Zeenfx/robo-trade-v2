# Robô···Trade

Robô···de análise técnica para ativos da B3 com opções listadas, conforme especificacao aprovada em 17/09/2026.

## Visao geral

- Triagem interna de candidatos (sem alertas parciais).
- Análise D1/H1/M15.
- Filtro de IV Rank (≤25%) e IV Percentile (≤30%).
- Envio de duas mensagens no Telegram apenas para setups aprovados (dia D+1).
- Nenhuma execucao automatica de ordens.

## Estrutura

```text
robo-trade/
├── config.py          # Configuracao por variaveis de ambiente
├── data_fetcher.py    # Dados de acoes, opcoes e historico (Brapi)
├── screener.py        # Triagem ampla e ranking de candidatos
├── analyzer.py        # Análise D1/H1/M15
├── iv_filter.py       # IV Rank, IV Percentile e elegibilidade
├── messenger.py       # Mensagens do Telegram e atalho Profit
├── database.py        # Persistencia em SQLite
├── main.py            # Orquestracao e modos de execucao
├── requirements.txt   # Dependencias
└── README.md          # Este arquivo
```

## Variaveis de ambiente

No Railway, configurar:

| Nome | Valor exemplo | Obrigatorio |
|------|---------------|-------------|
| `BRAPI_BASE_URL` | `https://brapi.dev/api` | nao |
| `BRAPI_TOKEN` | `SEU_TOKEN` | sim |
| `TELEGRAM_TOKEN` | `123456:AA...` | sim |
| `TELEGRAM_CHAT_ID` | `123456789` | sim |
| `MAX_CANDIDATES_PER_CYCLE` | `5` | nao |
| `PRICE_CHANGE_PCT` | `2.5` | nao |
| `GAP_PCT` | `1.5` | nao |
| `RANGE_MULTIPLIER` | `1.8` | nao |
| `VOLUME_MULTIPLIER` | `1.5` | nao |
| `IV_RANK_MAX` | `25` | nao |
| `IV_PERCENTILE_MAX` | `30` | nao |
| `DB_PATH` | `robo_trade.db` | nao |
| `PROFIT_BASE_URL` | `https://profit.profit.com.br` | nao |
| `MODE` | `intraday` / `post_market` / `dispatch` | sim |

## Execucao no Railway

Criar tres workers ou usar cron:

1. **Intraday** (a cada 15 minutos, 10:15–16:55):
   - `MODE=intraday`

2. **Post-market** (apos fechamento):
   - `MODE=post_market`

3. **Dispatch D+1** (manha do dia seguinte):
   - `MODE=dispatch`

Cada worker executa `python main.py`.

## Fluxo de mensagens

- Dia D: varredura e análise → registro interno (sem Telegram).
- Dia D (pos-fechamento): atualiza IV → filtra.
- Dia D+1: se setup aprovado (tecnico + IV), envia:
  1. Aviso curto.
  2. Análise completa com atalho Profit.

## Backtesting

Implementar posteriormente, usando historico de 12 meses e metricas:
- Taxa de acerto.
- Relacao risco/retorno.
- Frequencia de setups.
- Drawdown maximo simulado.