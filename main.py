import logging
import asyncio
from datetime import datetime, UTC
import httpx
from brapi_client import get_quote, get_option_chain

logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = "7518015689:AAH8BfzKj8vN9G5L2qR4mT6sU8wX0yZ1aB3"
CHAT_ID = "6079803525"

# Universo de ações
ATIVOS = [
    'PETR4', 'VALE3', 'ITUB4', 'BBDC4', 'ABEV3', 'B3SA3',
    'WEGE3', 'RENT3', 'LREN3', 'SUZB3', 'CCRO3', 'CMIG4',
    'MGLU3', 'VVAR3', 'BHIA3', 'AMER3', 'LAME3', 'GUAR3',
    'CPFE3', 'CSNA3', 'EGIE3', 'ENBR3', 'EQTL3', 'SBSP3',
    'EMBR3', 'RAIL3', 'RADL3', 'CYRE3', 'MULT3', 'HAPV3'
]

def get_profit_link(symbol: str) -> str:
    return f"https://profit.net.br/chart/{symbol}"

def analyze_trend(quote, history=None):
    """
    Analisa tendência do ativo.
    Retorna: 'CALL', 'PUT' ou None
    """
    price = quote.price
    if not price:
        return None, 0
    
    # Critérios simples de tendência (pode expandir com análise técnica)
    # Por enquanto, usa variação do dia como proxy
    change_pct = quote._data.get('changePercent', 0)
    
    # CALL: ativo subindo forte (> 2%)
    if change_pct > 2.0:
        return 'CALL', change_pct
    
    # PUT: ativo caindo forte (< -2%)
    if change_pct < -2.0:
        return 'PUT', abs(change_pct)
    
    return None, change_pct

def select_option_for_buy(chain, signal_type, underlying_price):
    """
    Seleciona melhor opção para COMPRA (call ou put).
    
    CALL: delta 0.50-0.70 (ITM ou ATM)
    PUT: delta 0.30-0.50 (OTM ou ATM)
    """
    if signal_type == 'CALL':
        options = chain.calls
        delta_min, delta_max = 0.50, 0.70
    else:  # PUT
        options = chain.puts
        delta_min, delta_max = 0.30, 0.50
    
    if not options:
        return None
    
    # Filtra opções com delta no range e preço > 0
    valid = [
        opt for opt in options
        if delta_min <= opt.get('delta', 0) <= delta_max
        and opt.get('price', 0) > 0
    ]
    
    if not valid:
        # Fallback: pega a mais próxima do delta alvo
        target_delta = (delta_min + delta_max) / 2
        valid = [opt for opt in options if opt.get('price', 0) > 0]
        if not valid:
            return None
        return min(valid, key=lambda x: abs(x.get('delta', 0.5) - target_delta))
    
    # Ordena por volume (liquidez) e pega a mais líquida
    best = max(valid, key=lambda x: x.get('volume', 0))
    return best

def format_message(ativo: str, signal: str, option: dict, underlying_price: float, change_pct: float) -> str:
    """Formata mensagem para Telegram"""
    emoji = "🟢" if signal == 'CALL' else "🔴"
    action = "COMPRE CALL" if signal == 'CALL' else "COMPRE PUT"
    direction = "ALTA" if signal == 'CALL' else "BAIXA"
    
    profit_link = get_profit_link(ativo)
    
    msg = f"""{emoji} {action} - {ativo}

📈 Tendência: {direction} ({change_pct:+.1f}% hoje)
🔹 Opção: {option['symbol']}
🔹 Tipo: {signal}
🔹 Strike: R$ {option['strike']:.2f}
🔹 Preço: R$ {option['price']:.2f}
🔹 Delta: {option.get('delta', 0):.2f}
🔹 IV: {option.get('iv', 0)*100:.1f}%
🔹 Volume: {option.get('volume', 0):,}

💰 Ativo: R$ {underlying_price:.2f}

🎯 Alvo: 2x-3x se ativo confirmar tendência
⚠️ Stop: -50% do prêmio

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
            logger.info("Telegram enviado")
        except Exception as e:
            logger.error(f"Erro Telegram: {e}")

def main():
    logger.info(f'Horário UTC: {datetime.now(UTC).isoformat()}')
    logger.info(f'Robô de Opções Direcionais')
    logger.info(f'Universo: {len(ATIVOS)} ações')
    logger.info(f'Estratégia: Compra de CALL (alta) ou PUT (baixa)')
    
    sinais_encontrados = []
    
    for ativo in ATIVOS:
        logger.info(f'Analisando {ativo}')
        
        # Preço do ativo
        quote = get_quote(ativo)
        if not quote.price:
            continue
        
        # Analisa tendência
        signal, change_pct = analyze_trend(quote)
        if not signal:
            logger.info(f'{ativo}: sem tendência clara ({change_pct:+.1f}%)')
            continue
        
        logger.info(f'{ativo}: sinal {signal} ({change_pct:+.1f}%)')
        
        # Cadeia de opções
        chain = get_option_chain(ativo)
        if not chain.calls and not chain.puts:
            logger.warning(f'{ativo}: sem opções')
            continue
        
        # Seleciona opção para compra
        option = select_option_for_buy(chain, signal, quote.price)
        if not option:
            logger.warning(f'{ativo}: nenhuma opção adequada para {signal}')
            continue
        
        logger.info(f"Sinal: {ativo} - {signal} - {option['symbol']} @ R${option['price']:.2f}")
        sinais_encontrados.append((ativo, signal, option, quote.price, change_pct))
    
    # Envia alertas
    if sinais_encontrados:
        logger.info(f"Enviando {len(sinais_encontrados)} sinais no Telegram")
        for ativo, signal, option, price, change in sinais_encontrados:
            msg = format_message(ativo, signal, option, price, change)
            asyncio.run(send_telegram(msg))
    else:
        logger.info("Nenhum sinal de compra hoje")
    
    logger.info('Fim da execução.')

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    main()
