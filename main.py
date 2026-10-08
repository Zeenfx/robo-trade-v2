import logging
from datetime import datetime, UTC
from brapi_client import get_quote, get_option_chain

logger = logging.getLogger(__name__)

DELTA_ALVO = 0.3

# Universo expandido - 30 ações líquidas com opções da B3
# Com plano Startup (150k req/mês), dá pra varrer essa lista 2-3x/dia
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

def main():
    logger.info(f'Horário UTC: {datetime.now(UTC).isoformat()}')
    logger.info(f'MODO=opcoes, TESTE_MODE=False')
    logger.info(f'Univero: {len(ATIVOS)} acoes')
    logger.info(f'ATIVOS={ATIVOS}')
    logger.info(f'DELTA_ALVO={DELTA_ALVO}')
    
    for ativo in ATIVOS:
        logger.info(f'Analisando gatilho para {ativo}')
        chain = get_option_chain(ativo)
        if not chain.calls:
            logger.warning(f'Screener: sem calls para {ativo}')
            continue
        
        best_call = find_best_call(chain.calls, DELTA_ALVO)
        if best_call:
            logger.info(f"Screener: selecionada {best_call['symbol']} (delta≈{best_call['delta']:.2f}) para {ativo}")
            if best_call.get('price') and best_call['price'] > 0:
                logger.info(f"Gatilho encontrado: {ativo} - {best_call['symbol']} @ R${best_call['price']:.2f}")
            else:
                logger.warning(f"Sem preço para {best_call['symbol']}")
        else:
            logger.warning(f'Screener: nenhuma call encontrada para {ativo}')
    
    logger.info('Fim da execução (v3).')

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    main()
