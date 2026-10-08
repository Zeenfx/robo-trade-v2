"""
Configurações do RobTrade V7
"""
import os
from datetime import datetime, time

# ==================== API ====================
BRAPI_TOKEN = os.getenv("BRAPI_TOKEN", "")
BRAPI_BASE_URL = "https://brapi.dev/api/v2"

# ==================== Telegram ====================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ==================== Universo de Ativos ====================
# Lista dinâmica: robô identifica ativos com opções
# Filtro mínimo de liquidez
MIN_VOLUME_MEDIO = 10_000_000  # R$ 10M volume médio diário
MIN_PRECO = 2.0  # Preço mínimo da ação

# ==================== Triagem ====================
# Movimento relevante (pelo menos um)
MIN_VARIACAO_INTRADIA = 2.5  # %
MIN_GAP = 1.5  # %
MIN_EXPANSAO_RANGE = 1.8  # 1.8x média últimos 20 dias

# Participação acima do padrão (pelo menos um)
MIN_VOLUME_FINANCEIRO = 1.5  # 1.5x média recente
MIN_NUM_NEGOCIOS = 1.5  # 1.5x média recente
MIN_VOLUME_RELATIVO = 1.5  # VR mínimo

# ==================== Análise Gráfica ====================
# Timeframes
TIMEFRAMES = ["D1", "H1", "M15"]

# Tendência (MM20)
MM_PERIOD = 20

# Volume no rompimento
MIN_VOLUME_ROMPIMENTO = 1.5  # 1.5x média 20 períodos

# Pavio de rejeição
MIN_PAVIO_REJEICAO = 2.0  # Pavio ≥ 2x corpo do candle

# ==================== Volatilidade ====================
# IV Rank e IV Percentile favoráveis
MAX_IV_RANK = 25
MAX_IV_PERCENTILE = 30
IV_WINDOW = 252  # Pregões (≈ 1 ano)

# ==================== Opções ====================
# Liquidez mínima da opção
MIN_VOLUME_OPCAO = 100  # Contratos negociados
MIN_OPEN_INTEREST = 500  # Contratos em aberto
MAX_SPREAD_PCT = 5.0  # Spread máximo % do preço

# Vencimento
MIN_DTE = 7  # Dias mínimos até vencimento
MAX_DTE = 60  # Dias máximos até vencimento

# Greeks (para compra de CALL/PUT)
MIN_DELTA = 0.50  # Delta mínimo
MAX_DELTA = 0.70  # Delta máximo

# ==================== Risco ====================
# Stop e alvo
MIN_RISCO_RETORNO = 2.0  # Alvo ≥ 2x o risco
MAX_STOP_PCT = 3.0  # Stop máximo 3% da entrada

# ==================== Agendamento ====================
# Horários de varredura
PRE_ABERTURA = time(8, 30)  # Pré-abertura
POS_ABERTURA_INICIO = time(9, 45)
POS_ABERTURA_FIM = time(10, 15)
PREGAO_INICIO = time(10, 15)
PREGAO_FIM = time(17, 55)
POS_MERCADO = time(18, 0)  # Pós-mercado

# Intervalos
INTERVALO_POS_ABERTURA = 5 * 60  # 5 minutos
INTERVALO_PREGAO = 15 * 60  # 15 minutos

# ==================== Priorização ====================
MAX_CANDIDATOS_POR_CICLO = 5  # Máximo de candidatos analisados profundamente

# ==================== Backtesting ====================
BACKTEST_WINDOW = 365  # Últimos 365 dias

# ==================== Logs ====================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

# ==================== Banco de Dados ====================
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/robo_trade.db")

# ==================== Estratégias Habilitadas ====================
ESTRATEGIAS_HABILITADAS = {
    "compra_call": True,
    "compra_put": True,
}
