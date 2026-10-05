import logging
from datetime import datetime

from analyzer import SinalAtivo, detectar_gatilho
from config import *
from data_fetcher import get_iv_history, get_opcoes_cadeia, get_price_data
from iv_filter import filtro_volatilidade
from messenger import enviar_telegram, formatar_mensagem
from screener import selecionar_opcao

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def processar(ativo):
    iv_historico = get_iv_history(ativo, IV_HISTORY_DAYS)
    if not iv_historico:
        logger.info("%s: sem histórico IV; ignorado", ativo)
        return
    aprovado, rank, percentile = filtro_volatilidade(iv_historico[-1], iv_historico, IV_RANK_MAX_BUY, IV_PERCENTILE_MAX_BUY)
    if not aprovado:
        logger.info("%s: IV não favorável", ativo)
        return
    dados = get_price_data(ativo, TIMEFRAME)
    if dados is None or len(dados) < 52:
        logger.info("%s: dados de preço insuficientes", ativo)
        return
    analise = detectar_gatilho(dados, LOOKBACK_ROMPIMENTO, VOLUME_MULT, EMA_SHORT, EMA_LONG)
    if analise.sinal == SinalAtivo.AGUARDAR:
        logger.info("%s: aguardar — %s", ativo, analise.motivo)
        return
    cadeia = get_opcoes_cadeia(ativo)
    opcao = selecionar_opcao(ativo, analise.sinal.value, analise.preco_atual, cadeia, OPCAO_DELTA_MIN, OPCAO_DELTA_MAX, OPCAO_EXPIRY_MIN_DIAS, OPCAO_EXPIRY_MAX_DIAS, OPCAO_VOLUME_MIN_DIA, OPCAO_OPEN_INTEREST_MIN)
    if opcao is None:
        logger.info("%s: sinal %s, mas nenhuma opção líquida compatível", ativo, analise.sinal.value)
        return
    mensagem = formatar_mensagem(ativo, analise, rank, percentile, opcao)
    enviar_telegram(mensagem)


def main():
    logger.info("Robô de opções %s iniciado | agora=%s", ROBO_VERSION, datetime.now().isoformat())
    logger.info("Varredura de oportunidades em opções iniciada")
    for ativo in UNIVERSE:
        try:
            processar(ativo)
        except Exception as exc:
            logger.exception("%s: erro durante varredura: %s", ativo, exc)
    logger.info("Varredura de oportunidades em opções concluída")


if __name__ == "__main__":
    main()
