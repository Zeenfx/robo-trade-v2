"""Parametros e credenciais. Tudo ajustavel por variavel de ambiente ou aqui."""
import os

def _load_env(path=".env"):
    """Carrega .env simples (CHAVE=valor) sem sobrescrever variaveis ja definidas."""
    here = os.path.dirname(os.path.abspath(__file__))
    p = os.path.join(here, path)
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

_load_env()

BRAPI_TOKEN = os.getenv("BRAPI_TOKEN") or os.getenv("BRAPI_API_KEY", "")
BRAPI_BASE = "https://brapi.dev/api/v2"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
_HERE = os.path.dirname(os.path.abspath(__file__))
_VOL = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "")
ON_RAILWAY = bool(os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_PROJECT_ID"))
_EXPLICIT_DB = os.getenv("DB_PATH") or os.getenv("DATABASE_PATH")
DB_PATH = _EXPLICIT_DB or (os.path.join(_VOL, "robo_trade.db") if _VOL else os.path.join(_HERE, "robo_trade.db"))
DB_PERSISTENT = bool(_EXPLICIT_DB or _VOL) or not ON_RAILWAY   # no Railway sem Volume o banco some a cada execucao
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
# Link do Profit ({ticker} = codigo da opcao ou do ativo). "off" desativa.
PROFIT_LINK_TEMPLATE = os.getenv("PROFIT_LINK_TEMPLATE", "https://profitchart.com.br/chart?ticker={ticker}&timeframe=5m")
TZ = "America/Sao_Paulo"
# Mensagem das 09:30 com a watchlist (desligada: especificacao pede nenhum aviso antes do sinal completo)
# Regra B: IV nao bloqueia (so etiqueta). IV_GATE=true volta ao bloqueio Rank<=25 e Pct<=30 (regra A).
IV_GATE = os.getenv("IV_GATE", "false").lower() == "true"
ATR_TO_SIGMA = 0.8           # converte ATR D1 em desvio-padrao diario aproximado
TIME_EXIT_DAYS = 10          # sugestao de saida: vencimento menos N dias
IV_TAG_EXPENSIVE_RANK = 75   # Rank acima disso = cara
SHADOW = os.getenv("SHADOW", "true").lower() == "true"   # registra em segredo sinais das regras B/C de IV
SHADOW_C_RANK_MAX = 60
# Conferencia unica: IV/Rank/Pct de um ativo em datas passadas (formato TICKER:AAAA-MM-DD,AAAA-MM-DD). Roda 1x por valor.
CHECK_IV = os.getenv("CHECK_IV", "MGLU3:2026-09-29,2026-09-30,2026-10-01,2026-10-02,2026-10-05,2026-10-06")
WATCHLIST_MSG = os.getenv("WATCHLIST_MSG", "false").lower() == "true"

DEFAULT_UNIVERSE = [
    "PETR4", "VALE3", "PRIO3", "BBAS3", "B3SA3", "BBDC4", "AXIA3", "ITUB4", "BPAC11",
    "ABEV3", "ITSA4", "MGLU3", "CSNA3", "RENT3", "LREN3", "WEGE3", "SUZB3",
]
UNIVERSE = [t.strip().upper() for t in os.getenv("UNIVERSE", "").split(",") if t.strip()] or DEFAULT_UNIVERSE

# --- Filtro de IV (opcao barata) ---
IV_WINDOW = 252
IV_MIN_HISTORY = 60          # minimo de pregoes para calcular rank/percentile
IV_RANK_MAX = 25
IV_PERCENTILE_MAX = 30
IV_TARGET_DTE = 40           # vencimento escolhido para medir IV ATM
IV_MIN_DTE = 25
IV_MAX_DTE = 70

# --- Gatilho grafico ---
BREAKOUT_LOOKBACK = 20       # rompimento de max/min dos ultimos N pregoes
VR_MIN = 1.5                 # volume relativo minimo (ritmo do dia vs media 20d)
M15_CONFIRM_CLOSES = 2       # fechamentos M15 consecutivos alem do nivel
ATR_PERIOD = 14
STOP_ATR_BUFFER = 0.5        # stop = nivel rompido -/+ 0.5 ATR
MAX_STOP_ATR = 2.0           # descarta se stop > 2 ATR do preco
RR_TARGET = 2.0              # alvo = 2x o risco
MAX_CANDIDATES_PER_CYCLE = 5
SESSION_START_H, SESSION_END_H = 10.0, 18.0   # para ritmo de volume

# --- Selecao da opcao ---
DELTA_MIN, DELTA_MAX = 0.40, 0.60
MIN_OPEN_INTEREST = 500
MIN_VOLUME = 50
MAX_SPREAD_PCT = 8.0
OPT_MIN_DTE, OPT_MAX_DTE = 25, 65   # janela larga o bastante para sempre haver um vencimento mensal
OPTION_STOP_LOSS_PCT = 50    # manejo: sair se premio perder 50%
