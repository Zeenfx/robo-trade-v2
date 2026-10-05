# Robô de Trade de Opções (B3)

Robô automatizado para identificação de oportunidades em **opções de ações brasileiras (B3)**.

## Funcionalidades

- **Filtro de volatilidade**: IV Rank e IV Percentile para identificar opções "baratas"
- **Gatilho técnico**: Detecta início de movimentos (CALL/PUT) baseado em:
  - Rompimento de suporte/resistência
  - Tendência (EMA 20/50)
  - Momentum (MACD/RSI)
  - Volume
- **Seleção de opção**: Escolhe a melhor opção com critérios de:
  - Delta (0.35-0.55)
  - Expiração (14-42 dias)
  - Liquidez (volume e open interest)
  - Moneyness (ATM ou levemente OTM)
- **Alertas no Telegram**: Mensagens acionáveis com entrada, stop, alvo e link do Profit

## Setup

### 1. Clone o repositório

```bash
git clone https://github.com/Zeenfx/robo-trade-v2.git
cd robo-trade-v2
```

### 2. Instale as dependências

```bash
pip install -r requirements.txt
```

### 3. Configure as variáveis de ambiente

Crie um arquivo `.env` na raiz do projeto:

```bash
# Telegram
TELEGRAM_TOKEN=seu_bot_token_aqui
TELEGRAM_CHAT_ID=seu_chat_id_aqui

# Brapi API (https://brapi.dev)
BRAPI_API_KEY=sua_api_key_aqui
```

**Como obter:**
- **Telegram**: Crie um bot via [@BotFather](https://t.me/botfather) e pegue o token
- **Brapi**: Crie conta em [brapi.dev](https://brapi.dev) e pegue sua API key (plano Pro necessário para opções)

### 4. Teste localmente

```bash
python main.py
```

## Deploy no Railway

1. Conecte seu repositório GitHub no Railway
2. Adicione as variáveis de ambiente:
   - `TELEGRAM_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `BRAPI_API_KEY`
3. O Railway fará deploy automático

## Estrutura do projeto

```
robo-trade-v2/
├── main.py           # Orquestração principal
├── config.py         # Configurações e thresholds
├── data_fetcher.py   # Busca de dados (Brapi)
├── iv_filter.py      # Filtro de volatilidade (IV Rank/Percentile)
├── analyzer.py       # Análise técnica (gatilhos CALL/PUT)
├── screener.py       # Seleção de opções
├── messenger.py      # Envio de mensagens (Telegram)
├── database.py       # (opcional) Persistência de dados
├── health_check.py   # Endpoint de saúde
├── requirements.txt  # Dependências
└── README.md         # Este arquivo
```

## Fluxo de execução

1. **Varre universo de ativos** (PETR4, VALE3, PRIO3, etc.)
2. **Filtra por IV**: Só continua se IV Rank ≤ 40% e IV Percentile ≤ 40%
3. **Analisa gráfico**: Detecta CALL, PUT ou AGUARDAR
4. **Seleciona opção**: Escolhe call/put com melhor liquidez e delta
5. **Envia alerta**: Manda mensagem no Telegram com entrada/stop/alvo + link do Profit

## Controle de ruído

- Não repete alerta do mesmo ticker e tipo em menos de **60 minutos**
- Permite novo alerta se preço variar **≥ 3%**

## Ajuste de parâmetros

Edite `config.py` para ajustar:

- Thresholds de IV (`IV_RANK_MAX_BUY`, `IV_PERCENTILE_MAX_BUY`)
- Delta da opção (`OPCAO_DELTA_MIN`, `OPCAO_DELTA_MAX`)
- Expiração (`OPCAO_EXPIRY_MIN_DIAS`, `OPCAO_EXPIRY_MAX_DIAS`)
- Liquidez mínima (`OPCAO_VOLUME_MIN_DIA`, `OPCAO_OPEN_INTEREST_MIN`)

## Formato do alerta

```
PETR4 — OPORTUNIDADE CALL
Preço PETR4: R$ 56,54
Gatilho: rompeu máxima de 20 períodos com volume 1,8× a média.
Tendência: alta | momentum positivo.
IV Rank: 35% | IV Percentile: 32% (opção barata)

Opção sugerida: PETRJ58 (call, strike 58,00, vencimento 04/11/2026)
Delta estimado: ~0,45
Preço da opção: R$ 1,10
Entrada: até R$ 1,20
Stop: abaixo de R$ 0,85
Alvo 1: R$ 1,60
Alvo 2: R$ 2,00
Risco/Retorno: ~1:2,5

Profit opção: https://profitchart.com.br/chart?ticker=PETRJ58&timeframe=5m
```

## Licença

MIT
