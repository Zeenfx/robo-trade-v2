"""Screener de opções: seleciona opção por delta aproximado."""

from __future__ import annotations

import logging
from typing import Any

from brapi_client import OptionChain, get_option_chain, get_quote
from config import ATIVOS, DELTA_ALVO, EXPIRACAO_DIAS

logger = logging.getLogger(__name__)


def _delta_aproximado(option: dict[str, Any], underlying_price: float) -> float | None:
    """
    Estima delta de uma opção de forma simplificada:

    - call: delta ≈ max(0, min(1, 1 - (K / S)))
    - put:  delta ≈ -max(0, min(1, 1 - (S / K)))
    """
    try:
        strike = option.get("strike")
        if strike is None or strike <= 0 or underlying_price <= 0:
            return None
        kind = option.get("contractSymbol", "").upper()
        is_call = "C" in kind or "CALL" in kind

        if is_call:
            delta = max(0.0, min(1.0, 1.0 - (strike / underlying_price)))
        else:
            delta = -max(0.0, min(1.0, 1.0 - (underlying_price / strike)))
        return delta
    except Exception:
        return None


def selecionar_opcao(ticker: str | None = None) -> dict[str, Any] | None:
    """
    Seleciona uma opção (call) para o ativo informado (ou varre ATIVOS)
    que tenha delta próximo de DELTA_ALVO.
    """
    tickers = [ticker] if ticker else ATIVOS

    for underlying in tickers:
        chain: OptionChain | None = get_option_chain(underlying)
        if not chain or not chain.calls:
            logger.warning("Screener: sem calls para %s", underlying)
            continue

        quote = get_quote(underlying)
        if not quote or quote.price is None:
            logger.warning("Screener: sem cotação para %s", underlying)
            continue

        underlying_price = quote.price
        target = abs(DELTA_ALVO)
        best = None
        best_diff = 1.0

        for opt in chain.calls:
            delta = _delta_aproximado(opt, underlying_price)
            if delta is None:
                continue
            diff = abs(delta - target)
            if diff < best_diff:
                best_diff = diff
                best = {
                    "option": opt,
                    "underlying": underlying,
                    "underlying_price": underlying_price,
                    "delta_est": delta,
                }
            if diff < 0.05:
                break

        if not best:
            logger.warning("Screener: nenhuma call com delta próximo de %s em %s", target, underlying)
            continue

        logger.info(
            "Screener: selecionada %s (delta≈%.2f) para %s @ %.2f",
            best["option"].get("contractSymbol"),
            best["delta_est"],
            underlying,
            underlying_price,
        )
        return best

    logger.warning("Screener: nenhum ativo com calls válidas.")
    return None
