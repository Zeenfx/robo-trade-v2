# Robô Trade — alertas de compra de CALL/PUT (B3) via Telegram

Apoio à decisão. **Não executa ordens.**

## Lógica (IV primeiro, gráfico depois)
1. **08:30 (dados do pregão anterior, disponíveis na brapi desde ~21h):** `eod` coleta a IV ATM (vencimento ~40 DTE) de cada ativo, salva no SQLite e calcula IV Rank / IV Percentile (252 pregões). Aprovados (Rank ≤ 25 e Percentile ≤ 30) entram na **watchlist** do dia seguinte.
2. **09:30 (opcional, desligado):** `WATCHLIST_MSG=true` envia a lista de ativos em observação.
3. **09:45–17:45:** `intraday` (5 min até 10:10, depois 15 min) analisa **só a watchlist**: rompimento da máxima/mínima de 20 pregões, 2 fechamentos M15 confirmando, ritmo de volume ≥ 1,5x, stop = nível ∓ 0,5 ATR, alvo 1:2.
4. Com sinal, escolhe a opção (call p/ alta, put p/ baixa): delta 0,40–0,60, 30–60 DTE, OI ≥ 500, volume ≥ 50, spread ≤ 8%. Envia mensagem curta + completa (com risco e manejo). Um alerta por ativo/direção/dia.

Todos os limiares estão em `config.py`.

> **Deploy:** veja `DEPLOY_RAILWAY.md`. Modo cron (`python main.py tick` a cada 5 min) ou sempre ligado (`python main.py serve`).

## Rodar local
```bash
pip install -r requirements.txt
cp .env.example .env   # preencha e exporte as variáveis
export $(grep -v '^#' .env | xargs)
python -m pytest tests            # testes
python main.py backfill 365       # UMA vez: reconstrói IV de 12 meses (muitas chamadas à API)
python main.py eod                # calcula IV de hoje e monta a watchlist
python main.py status             # mostra watchlist
DRY_RUN=true python main.py intraday
python main.py serve              # agendador (fuso America/Sao_Paulo)
```

## Deploy no Railway (resumo; passo a passo em DEPLOY_RAILWAY.md)
1. Serviço a partir do repositório; `railway.json` define `python main.py tick` (cron `*/5 11-20 * * 1-5`).
2. **Volume:** crie um Volume montado em `/data` e defina `DB_PATH=/data/robo_trade.db`. Sem volume o SQLite é apagado a cada deploy e o histórico de IV se perde.
3. Variáveis: `BRAPI_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (e opcionais do `.env.example`).
4. No primeiro deploy, rode o `backfill` uma vez (Railway → shell do serviço) e depois `eod`. Sem histórico (mínimo 60 pregões) nenhum ativo é aprovado.

Telegram: crie o bot no @BotFather e pegue o chat id enviando uma mensagem ao bot e abrindo `https://api.telegram.org/bot<TOKEN>/getUpdates`.

## Limitações conhecidas (leia antes de confiar nos alertas)
- **Não testado contra a API real.** Endpoints e campos foram implementados conforme a documentação da brapi; rode `eod` e `intraday` com `DRY_RUN=true` e confira antes de operar. Limites do plano Pro (requisições/min, profundidade do intraday) precisam ser verificados.
- **IV Rank/Percentile são calculados aqui** (a brapi não documenta esses campos), sobre a IV ATM do vencimento mais próximo de 40 DTE. A troca de vencimento causa pequenos saltos na série.
- **Bid/ask e prêmio vêm do pregão anterior** (dados de opções EOD). A mensagem avisa; confirme o book no Profit antes de decidir.
- Universo é lista fixa (`config.py` / `UNIVERSE`); universo dinâmico fica para depois.
- Feriados: sem candles do dia, o ciclo é ignorado.
- Sem backtest ainda; os limiares são pontos de partida, não validados.
- Link do Profit: usa `PROFIT_LINK_TEMPLATE` (padrão herdado da v2, `profitchart.com.br/chart?ticker=...`). A Nelogica não documenta esse formato; confirme se abre no iPhone. O código da opção sai também em toque-para-copiar.
