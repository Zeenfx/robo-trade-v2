import os
import logging
import time
import httpx
from brapi_client import get_quote, get_option_chain

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

TESTE_MODE = os.getenv("TESTE_MODE", "true").lower() == "true"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

logger.info(f"TESTE_MODE={TESTE_MODE}")
logger.info(f"TELEGRAM_TOKEN={'CONFIGURADO' if TELEGRAM_TOKEN else 'NAO CONFIGURADO'}")
logger.info(f"TELEGRAM_CHAT_ID={TELEGRAM_CHAT_ID or 'NAO CONFIGURADO'}")

ATIVOS = ["PETR4", "VALE3", "ITUB4"]

def analyze_trend(quote):
    if not quote.price:
        return None, 0.0
    change_pct = float(quote._data.get("changePercent") or 0.0)
    if TESTE_MODE:
        return ("CALL", 2.8) if sum(map(ord, quote.symbol)) % 2 == 0 else ("PUT", -2.6)
    if change_pct >= 2.0:
        return "CALL", change_pct
    if change_pct <= -2.0:
        return "PUT", change_pct
    return None, change_pct

def select_option(chain, signal_type):
    options = chain.calls if signal_type == "CALL" else chain.puts
    if not options:
        return None
    target = 0.55 if signal_type == "CALL" else 0.45
    valid = [o for o in options if float(o.get("price") or 0) > 0]
    return min(valid, key=lambda o: abs(abs(float(o.get("delta") or 0)) - target)) if valid else None

def send_telegram(ativo, signal, option, price, change):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("Telegram nao configurado!")
        return False
    
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    msg = f"TESTE {signal} {ativo}\nOpcao: {option['symbol']}\nStrike: {option['strike']}\nPremio: {option['price']}\nAtivo: {price}\n\nLink: profitmobile://chart/{ativo}"
    
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg,
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "Abrir Profit", "url": f"profitmobile://chart/{ativo}"}
            ]]
        }
    }
    
    try:
        with httpx.Client() as client:
            resp = client.post(url, json=payload, timeout=15)
            logger.info(f"Telegram status: {resp.status_code}")
            if resp.status_code == 200:
                logger.info("SUCESSO!")
                return True
            else:
                logger.error(f"Erro: {resp.text}")
                return False
    except Exception as e:
        logger.error(f"Excecao: {e}")
        return False

logger.info("Iniciando robo...")
for ativo in ATIVOS:
    logger.info(f"Processando {ativo}")
    quote = get_quote(ativo)
    if not quote.price:
        logger.warning(f"Sem preco {ativo}")
        continue
    
    signal, change = analyze_trend(quote)
    logger.info(f"Signal: {signal}")
    if not signal:
        continue
    
    chain = get_option_chain(ativo)
    option = select_option(chain, signal)
    if not option:
        logger.warning("Sem opcao")
        continue
    
    logger.info(f"Enviando {option['symbol']}")
    send_telegram(ativo, signal, option, quote.price, change)
    time.sleep(1)

logger.info("Fim.")
