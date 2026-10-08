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
    """Envia mensagem com botão inline"""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram não configurado")
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
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, timeout=15)
            response.raise_for_status()
            logger.info("Telegram enviado com sucesso")
            return True
    except Exception as e:
        logger.error(f"Erro Telegram: {e}")
        logger.error(f"Response: {response.text if 'response' in locals() else 'N/A'}")
        return False


def main():
    logger.info("Robô iniciado | TESTE_MODE=%s | ativos=%d", TESTE_MODE, len(ATIVOS))
    sent = 0
    errors = 0
    
    for ativo in ATIVOS:
        try:
            quote = get_quote(ativo)
            if not quote.price:
                logger.warning("Sem cotação para %s", ativo)
                continue
            
            signal, change_pct = analyze_trend(quote)
            if not signal:
                continue
            
            chain = get_option_chain(ativo)
            option = select_option_for_buy(chain, signal)
            if not option:
                logger.warning("Sem %s válida para %s", signal, ativo)
                continue
            
            message = format_message(ativo, signal, option, float(quote.price), change_pct)
            button_url = f"profitmobile://chart/{ativo}"
            
            logger.info(f"Enviando {signal} {ativo} - button: {button_url}")
            success = asyncio.run(send_telegram(message, button_url))
            
            if success:
                sent += 1
            else:
                errors += 1
        except Exception as e:
            logger.error(f"Erro ao processar {ativo}: {e}")
            errors += 1
    
    logger.info("Execução concluída | enviados=%d erros=%d", sent, errors)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    main()
