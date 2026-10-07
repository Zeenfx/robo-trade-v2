import logging
from datetime import datetime
from analyzer import SinalAtivo, detectar_gatilho
from screener import selecionar_opcao
from config import (
    ATIVOS, UNIVERSE, DELTA_ALVO, EXPIRACAO_DIAS,
    LOTE, LOTE_OPCOES, STOP_LOSS_PCT, ALVO_PCT,
    HORARIO_INICIO, HORARIO_FIM, INTERVALO_MIN,
    MODO, TESTE_MODE, BRAPI_TOKEN
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

def main():
    logger.info('Robô de opções iniciado (v3).')
    logger.info(f'Horário UTC: {datetime.utcnow().isoformat()}')
    logger.info(f'MODO={MODO}, TESTE_MODE={TESTE_MODE}')
    logger.info(f'ATIVOS={ATIVOS}')
    logger.info(f'UNIVERSE={UNIVERSE}')
    logger.info(f'DELTA_ALVO={DELTA_ALVO}, EXPIRACAO_DIAS={EXPIRACAO_DIAS}')
    logger.info(f'LOTE={LOTE}, LOTE_OPCOES={LOTE_OPCOES}')
    logger.info(f'STOP_LOSS_PCT={STOP_LOSS_PCT}, ALVO_PCT={ALVO_PCT}')
    logger.info(f'HORARIO_INICIO={HORARIO_INICIO}, HORARIO_FIM={HORARIO_FIM}, INTERVALO_MIN={INTERVALO_MIN}')
    logger.info(f'BRAPI_TOKEN configurado={bool(BRAPI_TOKEN)}')
    
    for ticker in UNIVERSE:
        logger.info(f'Analisando gatilho para {ticker}')
        try:
            sinal: SinalAtivo | None = detectar_gatilho(ticker)
            if sinal:
                logger.info(f'Gatilho encontrado: {sinal}')
            else:
                logger.info(f'Nenhum gatilho encontrado para {ticker} nesta execução.')
        except Exception as e:
            logger.error(f'Erro ao analisar {ticker}: {e}')
            continue
    
    logger.info('Fim da execução (v3).')

if __name__ == '__main__':
    main()
