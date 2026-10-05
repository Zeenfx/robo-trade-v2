"""Configurações do robô de trade de opções."""
import os
from datetime import time

# Telegram
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Universo de ativos
UNIVERSE = [
    "PETR4",
    "VALE3",
    "PRIO3",
    "BBAS3",
    "B3SA3",
    "BBDC4",
    "AXIA3",
    "ITUB4",
    "BPAC11",
    "ABEV3",
    "ITSA4",
    "MGLU3",
    "CSNA3",
    "RENT3",
    "LREN3",
    "WEGE3",
    "SUZB3",
]

# Timeframe para análise do ativo (minutos)
TIMEFRAME = 5

# Janela histórica para IV (dias)
IV_HISTORY_DAYS = 252

# Filtros de IV Rank e IV Percentile
# Para compra de opção (opção "barata")
IV_RANK_MAX_BUY = 40
IV_PERCENTILE_MAX_BUY = 40

# Para venda de opção (opção "cara") - futuro uso
IV_RANK_MIN_SELL = 60
IV_PERCENTILE_MIN_SELL = 60

# Gatilhos no ativo
LOOKBACK_ROMPIMENTO = 20  # janelas para máxima/mínima
VOLUME_MULT = 1.5  # volume atual deve ser >= X * média(20)

# EMA para tendência
EMA_SHORT = 20
EMA_LONG = 50

# Seleção de opção
OPCAO_DELTA_MIN = 0.35
OPCAO_DELTA_MAX = 0.55
OPCAO_EXPIRY_MIN_DIAS = 14
OPCAO_EXPIRY_MAX_DIAS = 42
OPCAO_VOLUME_MIN_DIA = 10000  # volume mínimo diário da opção
OPCAO_OPEN_INTEREST_MIN = 50000  # open interest mínimo

# Controle de ruído
MINUTOS_ENTRE_ALERTAS_SAME_TICKER = 60
MINIMA_VARIACAO_PERCENTUAL_PARA_NOVO_ALERTA = 3.0

# Horário de execução (B3)
MARKET_START = time(9, 0)
MARKET_END = time(17, 30)
