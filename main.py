import logging
import asyncio
from datetime import datetime, UTC
import httpx
from brapi_client import get_quote, get_option_chain

logger = logging.getLogger(__name__)

DELTA_ALVO = 0.3
TELEGRAM_TOKEN = "7518015689:AAH8BfzKj8vN9G5L2qR4mT6sU8wX0yZ1aB3"
CHAT_ID = "6079803525"

# Universo expandido - 30 ações líquidas com opções da B3
ATIVOS = [
    # Blue chips
    'PETR4', 'VALE3', 'ITUB4', 'BBDC4', 'ABEV3', 'B3SA3',
    'WEGE3', 'RENT3', 'LREN3', 'SUZB3', 'CCRO3', 'CMIG4',
    # Varejo
    'MGLU3', 'VVAR3', 'BHIA3', 'AMER3', 'LAME3', 'GUAR3',
    # Elétricas/Utilities
    'CPFE3', 'CSNA3', 'EGIE3', 'ENBR3', 'EQTL3', 'SBSP3',
    # Outros setores
    'EMBR3', 'RAIL3', 'RADL3', 'CYRE3', 'MULT3', 'HAPV3'
]

def find_best_call(calls, delta_alvo=0.3):
    """Encontra a call mais próxima do delta alvo"""
    if not calls:
        return None
    best = min(calls, key=lambda c: abs(c.get('delta', 0.5) - delta_alvo))
    return best

def get_profit_link(symbol: str, option_symbol: str) -> str:
    """Gera link direto para o gráfico no Profit (Nelogica)"""
    # Link universal que abre no Profit mobile se instalado
    return f"https://profit.net.br/chart/{symbol}"

def format_message(ativo: str, option: dict, underlying_price: float) -> str:
    """Formata mensagem para Telegram"""
    option_type = "CALL" if option.get('side') == 'call' else "PUT"
    moneyness = ((underlying_price - option['strike']) / underlying_price) * 100
    moneyness_str = f"{'ITM' if moneyness > 0 else 'OTM'} ({moneyness:+.1f}%)"
    
    profit_link = get_profit_link(ativo, option['symbol'])
    
    msg = f"""🚨 GATILHO DE OPÇÃO

📈 Ativo: {ativo}
🔹 Tipo: {option_type}
🔹 Opção: {option['symbol']}
🔹 Strike: R$ {option['strike']:.2f}
🔹 Preço: R$ {option['price']:.2f}
🔹 Delta: {option.get('delta', 0):.2f}
🔹 IV: {option.get('iv', 0)*100:.1f}%
🔹 Volume: {option.get('volume', 0):,}
🔹 Open Interest: {option.get('open_interest', 0):,}

💰 Preço do Ativo: R$ {underlying_price:.2f}
📊 Moneyness: {moneyness_str}

⚙️ Setup: Delta ~{DELTA_ALVO} (alvo: {DELTA_ALVO})

🔗 <a href="{profit_link}">Abrir no Profit</a>
"""
    return msg

async def send_telegram(msg: str):
    """Envia mensagem para o Telegram"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": msg,
        "parse_mode": "HTML"
    }
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            logger.info("Telegram enviado com sucesso")
        except Exception as e:
            logger.error(f"Erro ao enviar Telegram: {e}")

def main():
    logger.info(f'Horário UTC: {datetime.now(UTC).isoformat()}')
    logger.info(f'MODO=opcoes, TESTE_MODE=False')
    logger.info(f'Universo: {len(ATIVOS)} acoes')
    logger.info(f'DELTA_ALVO={DELTA_ALVO}')
    
    gatilhos_encontrados = []
    
    for ativo in ATIVOS:
        logger.info(f'Analisando gatilho para {ativo}')
        
        # Busca preço do ativo
        quote = get_quote(ativo)
        underlying_price = quote.price
        if not underlying_price:
            logger.warning(f"Sem preço para {ativo}")
            continue
        
        # Busca cadeia de opções
        chain = get_option_chain(ativo)
        if not chain.calls:
            logger.warning(f'Screener: sem calls para {ativo}')
            continue
        
        # Seleciona melhor call
        best_call = find_best_call(chain.calls, DELTA_ALVO)
        if best_call and best_call.get('price') and best_call['price'] > 0:
            logger.info(f"Gatilho encontrado: {ativo} - {best_call['symbol']} @ R${best_call['price']:.2f}")
            gatilhos_encontrados.append((ativo, best_call, underlying_price))
        else:
            logger.warning(f'Screener: nenhuma call válida para {ativo}')
    
    # Envia alertas no Telegram
    if gatilhos_encontrados:
        logger.info(f"Enviando {len(gatilhos_encontrados)} alertas no Telegram")
        for ativo, option, price in gatilhos_encontrados:
            msg = format_message(ativo, option, price)
            asyncio.run(send_telegram(msg))
    else:
        logger.info("Nenhum gatilho encontrado nesta execução")
    
    logger.info('Fim da execução (v3).')

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    main()
