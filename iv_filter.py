import logging

logger = logging.getLogger(__name__)


def calc_iv_rank(iv_atual, historico):
    if not historico:
        return 50.0
    minimo, maximo = min(historico), max(historico)
    if maximo == minimo:
        return 50.0
    return max(0.0, min(100.0, (iv_atual - minimo) * 100 / (maximo - minimo)))


def calc_iv_percentile(iv_atual, historico):
    if not historico:
        return 50.0
    return sum(iv < iv_atual for iv in historico) * 100 / len(historico)


def filtro_volatilidade(iv_atual, historico, rank_max=40.0, percentile_max=40.0):
    if len(historico) < 20:
        logger.info("Histórico de IV insuficiente")
        return False, 0.0, 0.0
    rank = calc_iv_rank(iv_atual, historico)
    percentile = calc_iv_percentile(iv_atual, historico)
    aprovado = rank <= rank_max and percentile <= percentile_max
    logger.info("IV atual=%.2f rank=%.1f%% percentile=%.1f%% aprovado=%s", iv_atual, rank, percentile, aprovado)
    return aprovado, rank, percentile
