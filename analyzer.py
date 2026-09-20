"""Robô···Trade — análise D1/H1/M15 e definicao do cenario."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Sequence

from data_fetcher import HistoryCandle, Quote


log = logging.getLogger("robo_trade")


@dataclass
class Analysis:
    ticker: str
    trend_d1: str
    structure_h1: str
    trigger_m15: str
    bias: str  # long, short, lateral
    region_of_interest: str
    invalidation: str
    target: str
    technical_space: str  # curto, medio, amplo


def analyze_ticker(
    ticker: str,
    quote: Quote,
    history: list[HistoryCandle],
) -> Analysis:
    """Analise simplificada D1/H1/M15.
    
    Em producao, implementar logica completa de tendencia, estrutura,
    gatilho, regiao de interesse, invalidacao, alvo e espaco tecnico.
    """
    if len(history) < 20:
        return Analysis(
            ticker=ticker,
            trend_d1="dados insuficientes",
            structure_h1="dados insuficientes",
            trigger_m15="dados insuficientes",
            bias="lateral",
            region_of_interest="n/a",
            invalidation="n/a",
            target="n/a",
            technical_space="curto",
        )

    closes = [c.close for c in history[-20:]]
    highs = [c.high for c in history[-20:]]
    lows = [c.low for c in history[-20:]]

    avg_close = sum(closes) / len(closes)
    max_high = max(highs)
    min_low = min(lows)

    if closes[-1] > avg_close and quote.last > max_high:
        trend_d1 = "alta"
        bias = "long"
    elif closes[-1] < avg_close and quote.last < min_low:
        trend_d1 = "baixa"
        bias = "short"
    else:
        trend_d1 = "lateral"
        bias = "lateral"

    structure_h1 = f"range {min_low:.2f}–{max_high:.2f}"
    trigger_m15 = "aguardar confirmacao M15"

    if bias == "long":
        region_of_interest = f"{min_low:.2f}–{avg_close:.2f}"
        invalidation = f"abaixo de {min_low:.2f}"
        target = f"{max_high:.2f}"
        technical_space = "medio"
    elif bias == "short":
        region_of_interest = f"{avg_close:.2f}–{max_high:.2f}"
        invalidation = f"acima de {max_high:.2f}"
        target = f"{min_low:.2f}"
        technical_space = "medio"
    else:
        region_of_interest = f"{min_low:.2f}–{max_high:.2f}"
        invalidation = "fora do range"
        target = "oposto do range"
        technical_space = "curto"

    return Analysis(
        ticker=ticker,
        trend_d1=trend_d1,
        structure_h1=structure_h1,
        trigger_m15=trigger_m15,
        bias=bias,
        region_of_interest=region_of_interest,
        invalidation=invalidation,
        target=target,
        technical_space=technical_space,
    )