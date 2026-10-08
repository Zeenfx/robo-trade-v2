import os
import logging
import asyncio
from datetime import datetime, UTC
import httpx
from brapi_client import get_quote, get_option_chain

logger = logging.getLogger(__name__)

TESTE_MODE = os.getenv("TESTE_MODE", "true").lower() == "true"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
ATIVOS = ["PETR4", "VALE3", "ITUB4", "BBDC4", "ABEV3", "B3SA3", "WEGE3", "RENT3", "LREN3", "SUZB3", "MGLU3", "CMIG4"]

logger.info("=== CONFIGURAÇÃO ===")
logger.info("TESTE_MODE: %s", TESTE_MODE)
logger.info("TELEGRAM_TOKEN configurado: %s", "SIM" if TELEGRAM_TOKEN else "NÃO")
logger.info("TELEGRAM_CHAT_ID: %s", TELEGRAM_CHAT_ID if TELEGRAM_CHAT_ID else "NÃO CONFIGURADO")
logger.info("ATIVOS: %d", len(ATIVOS))
logger.info("====================")


def analyze_trend(quote):
    price = quote.price
    if not price:
        return None, 0.0
    change_pct = float(quote._data.get("changePercent") or 0.0)
    if TESTE_MODE:
        return ("CALL", 2.8) if sum(map(ord, quote.symbol)) % 2 == 0 else ("PUT", -2.6)
    if change_pct >= 2.0:
        return "CALL", change_pct
    if change_pct <= -2.0:
        return "PUT", change_pct
    return None, change_pct


def select_option_for_buy(chain, signal_type):
    options = chain.calls if signal_type == "CALL" else chain.puts
    if not options:
        return None
    target_delta = 0.55 if signal_type == "CALL" else 0.45
    valid = [opt for opt in options if float(opt.get("price") or 0) > 0]
    return min(valid, key=lambda opt: abs(abs(float(opt.get("delta") or 0)) - target_delta)) if valid else None


def format_message(ativo, signal, option, underlying_price, change_pct):
    mode = "🧪 TESTE" if TESTE_MODE else "⚠️ ESTUDO"
    direction = "ALTA" if signal == "CALL" else "BAIXA"
    
    msg = f"""{mode} - {signal} {ativo}

Cenário: {direction} ({change_pct:+.1f}%)
Opção: {option['symbol']}
Strike: R$ {float(option['strike']):.2f}
Prêmio: R$ {float(option['price']):.2f}
Delta: {float(option.get('delta') or 0):.2f}
Ativo: R$ {underlying_price:.2f}

[NÃO OPERAR - APENAS TESTE]"""
    return msg


async def send_telegram(message, button_url):
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN não configurado!")
        return False
    if not TELEGRAM_CHAT_ID:
        logger.error("TELEGRAM_CHAT_ID não configurado!")
        return False
    
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "📈 Abrir no Profit", "url": button_url}
            ]]
        }
    }
    
    logger.info(f"Enviando Telegram para {TELEGRAM_CHAT_ID}")
    logger.info(f"URL: {url[:50]}...")
    logger.info(f"Botão: {button_url}")
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, timeout=15)
            logger.info(f"Status: {response.status_code}")
            if response.status_code == 200:
                logger.info("✅ Telegram enviado!")
                return True
            else:
                logger.error(f"❌ Erro: {response.status_code}")
                logger.error(f"Response: {response.text}")
                return False
    except Exception as e:
        logger.error(f"❌ Exceção: {e}")
        return False


def main():
    logger.info("Iniciando main()...")
    sent = 0
    errors = 0
    
    for ativo in ATIVOS:
        try:
            logger.info(f"\n=== Processando {ativo} ===")
            quote = get_quote(ativo)
            if not quote.price:
                logger.warning("Sem cotação")
                continue
            
            logger.info(f"Preço: {quote.price}")
            signal, change_pct = analyze_trend(quote)
            logger.info(f"Signal: {signal}, Change: {change_pct}%")
            
            if not signal:
                continue
            
            chain = get_option_chain(ativo)
            logger.info(f"Calls: {len(chain.calls)}, Puts: {len(chain.puts)}")
            
            option = select_option_for_buy(chain, signal)
            if not option:
                logger.warning("Sem opção válida")
                continue
            
            logger.info(f"Opção selecionada: {option['symbol']}")
            message = format_message(ativo, signal, option, float(quote.price), change_pct)
            button_url = f"profitmobile://chart/{ativo}"
            
            success = asyncio.run(send_telegram(message, button_url))
            if success:
                sent += 1
            else:
                errors += 1
        except Exception as e:
            logger.error(f"Erro {ativo}: {e}")
            errors += 1
    
    logger.info(f"\n=== FIM ===")
    logger.info(f"Enviados: {sent}, Erros: {errors}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    main()
