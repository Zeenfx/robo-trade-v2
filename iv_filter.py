"""Filtro de volatilidade implícita (IV Rank e IV Percentile)."""
import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def calc_iv_rank(
    iv_atual: float,
    iv_min_52sem: float,
    iv_max_52sem: float,
) -> float:
    """
    Calcula IV Rank: posiciona IV atual entre mínima e máxima de 52 semanas.
    
    IV Rank = (IV_atual - IV_min) / (IV_max - IV_min) * 100
    """
    if iv_max_52sem == iv_min_52sem:
        return 50.0
    
    iv_rank = (iv_atual - iv_min_52sem) / (iv_max_52sem - iv_min_52sem) * 100
    return max(0.0, min(100.0, iv_rank))


def calc_iv_percentile(iv_atual: float, iv_historico: List[float]) -> float:
    """
    Calcula IV Percentile: percentual de dias com IV menor que IV atual.
    
    IV Percentile = (dias com IV < IV_atual) / (total de dias) * 100
    """
    if not iv_historico:
        return 50.0
    
    n_dias_menor = sum(1 for iv in iv_historico if iv < iv_atual)
    iv_percentile = (n_dias_menor / len(iv_historico)) * 100
    return max(0.0, min(100.0, iv_percentile))


def filtro_volatilidade(
    iv_atual: float,
    iv_historico: List[float],
    iv_rank_max_buy: float = 40,
    iv_percentile_max_buy: float = 40,
) -> Tuple[bool, float, float]:
    """
    Aplica filtro de volatilidade para compra de opção.
    
    Retorna:
        (aprovado, iv_rank, iv_percentile)
    """
    # Extrair mínima e máxima de 52 semanas (últimos ~252 dias)
    if len(iv_historico) < 20:
        logger.warning(f"Histórico de IV muito curto: {len(iv_historico)} dias")
        return False, 0.0, 0.0
    
    iv_min_52sem = min(iv_historico)
    iv_max_52sem = max(iv_historico)
    
    iv_rank = calc_iv_rank(iv_atual, iv_min_52sem, iv_max_52sem)
    iv_percentile = calc_iv_percentile(iv_atual, iv_historico)
    
    aprovado = (iv_rank <= iv_rank_max_buy) and (iv_percentile <= iv_percentile_max_buy)
    
    logger.info(
        f"IV filter: IV={iv_atual:.2f} | IV Rank={iv_rank:.1f}% | IV Percentile={iv_percentile:.1f}% | aprovado={aprovado}"
    )
    
    return aprovado, iv_rank, iv_percentile
