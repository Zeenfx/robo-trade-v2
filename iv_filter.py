"""Robô···Trade — filtro de IV Rank e IV Percentile."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from config import Config
from data_fetcher import BrapiClient, OptionChain


log = logging.getLogger("robo_trade")


@dataclass
class IVResult:
    ticker: str
    iv_rank: float | None
    iv_percentile: float | None
    approved: bool


def check_iv(
    client: BrapiClient,
    ticker: str,
    cfg: Config,
) -> IVResult:
    chain = client.get_option_chain(ticker)

    if chain is None:
        log.warning("%s: cadeia de opcoes indisponivel", ticker)
        return IVResult(
            ticker=ticker,
            iv_rank=None,
            iv_percentile=None,
            approved=False,
        )

    iv_rank = chain.iv_rank
    iv_percentile = chain.iv_percentile

    if iv_rank is None or iv_percentile is None:
        log.warning("%s: IV Rank/Percentile indisponiveis", ticker)
        return IVResult(
            ticker=ticker,
            iv_rank=iv_rank,
            iv_percentile=iv_percentile,
            approved=False,
        )

    approved = (
        iv_rank <= cfg.iv_rank_max
        and iv_percentile <= cfg.iv_percentile_max
    )

    if approved:
        log.info(
            "%s: IV aprovado (Rank=%.1f%%, Percentile=%.1f%%)",
            ticker,
            iv_rank,
            iv_percentile,
        )
    else:
        log.info(
            "%s: IV reprovado (Rank=%.1f%%, Percentile=%.1f%%)",
            ticker,
            iv_rank,
            iv_percentile,
        )

    return IVResult(
        ticker=ticker,
        iv_rank=iv_rank,
        iv_percentile=iv_percentile,
        approved=approved,
    )