"""Robô Trade — triagem ampla e ranking de candidatos."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from config import Config
from data_fetcher import BrapiClient, HistoryCandle, Quote


log = logging.getLogger("robo_trade")


@dataclass
class Candidate:
    ticker: str
    quote: Quote
    history: list[HistoryCandle]
    score: float
    reason: str


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def price_movement(quote: Quote, cfg: Config) -> bool:
    return abs(quote.change_pct) >= cfg.price_change_pct


def has_relevant_movement(
    quote: Quote,
    history: list[HistoryCandle],
    cfg: Config,
) -> bool:
    if price_movement(quote, cfg):
        return True

    if history:
        prev_close = history[-1].close
        if prev_close > 0:
            gap = (quote.open - prev_close) / prev_close * 100
            if abs(gap) >= cfg.gap_pct:
                return True

        ranges = [c.high - c.low for c in history[-20:]]
        avg_range = mean([item for item in ranges if item > 0])
        if avg_range > 0 and quote.range_today >= cfg.range_multiplier * avg_range:
            return True

    return False


def has_participation(
    quote: Quote,
    history: list[HistoryCandle],
    cfg: Config,
) -> bool:
    if not history:
        return False

    avg_volume = mean([c.volume for c in history[-20:] if c.volume > 0])
    if avg_volume > 0 and quote.volume >= cfg.volume_multiplier * avg_volume:
        return True

    if quote.trades is not None and avg_volume > 0:
        return quote.trades >= cfg.volume_multiplier * avg_volume

    return False


def has_graphical_structure(
    quote: Quote,
    history: list[HistoryCandle],
) -> bool:
    if len(history) < 20:
        return False

    closes = [c.close for c in history[-20:]]
    highs = [c.high for c in history[-20:]]
    lows = [c.low for c in history[-20:]]
    max_high = max(highs[:-1])
    min_low = min(lows[:-1])

    return (
        quote.last > max_high
        or quote.last < min_low
        or closes[-1] < min_low
        or closes[-1] > max_high
    )


def score_candidate(
    quote: Quote,
    history: list[HistoryCandle],
    cfg: Config,
) -> tuple[float, str]:
    score = 0.0
    reasons: list[str] = []

    if price_movement(quote, cfg):
        score += 3.0
        reasons.append("movimento relevante")

    if history:
        prev_close = history[-1].close
        if prev_close > 0:
            gap = (quote.open - prev_close) / prev_close * 100
            if abs(gap) >= cfg.gap_pct:
                score += 2.0
                reasons.append("gap relevante")

        ranges = [c.high - c.low for c in history[-20:]]
        avg_range = mean([item for item in ranges if item > 0])
        if avg_range > 0 and quote.range_today >= cfg.range_multiplier * avg_range:
            score += 2.0
            reasons.append("expansao de range")

        avg_volume = mean([c.volume for c in history[-20:] if c.volume > 0])
        if avg_volume > 0 and quote.volume >= cfg.volume_multiplier * avg_volume:
            score += 2.0
            reasons.append("volume acima da media")

    return score, "; ".join(reasons) or "movimento relevante"


def screen_universe(
    client: BrapiClient,
    cfg: Config,
) -> list[Candidate]:
    tickers = client.list_eligible_tickers()
    log.info("Universo elegivel: %s", ", ".join(tickers))

    candidates: list[Candidate] = []

    for ticker in tickers:
        try:
            quote = client.get_quote(ticker)
            history = client.get_history(ticker, days=60)
        except Exception as exc:
            log.warning("%s: erro ao buscar dados: %s", ticker, exc)
            continue

        movement = has_relevant_movement(quote, history, cfg)
        strong_price_move = price_movement(quote, cfg)
        participation = has_participation(quote, history, cfg)
        structure = has_graphical_structure(quote, history)

        log.info(
            "%s: preco=%.2f variacao=%.2f%% volume=%s movimento=%s participacao=%s estrutura=%s",
            ticker,
            quote.last,
            quote.change_pct,
            quote.volume,
            movement,
            participation,
            structure,
        )

        if not movement:
            log.info("%s: excluido — sem movimento relevante", ticker)
            continue

        if strong_price_move:
            score, reason = score_candidate(quote, history, cfg)
            reason = f"movimento de preco >= {cfg.price_change_pct:.2f}%" + (f"; {reason}" if reason else "")
            candidates.append(
                Candidate(
                    ticker=ticker,
                    quote=quote,
                    history=history,
                    score=max(score, 3.0),
                    reason=reason,
                )
            )
            log.info("%s: aprovado — %s", ticker, reason)
            continue

        if not participation:
            log.info("%s: excluido — participacao abaixo do criterio", ticker)
            continue

        if not structure:
            log.info("%s: excluido — sem estrutura grafica", ticker)
            continue

        score, reason = score_candidate(quote, history, cfg)
        candidates.append(
            Candidate(
                ticker=ticker,
                quote=quote,
                history=history,
                score=score,
                reason=reason,
            )
        )
        log.info("%s: aprovado — %s", ticker, reason)

    candidates.sort(key=lambda candidate: candidate.score, reverse=True)
    selected = candidates[: cfg.max_candidates_per_cycle]

    if selected:
        log.info(
            "Candidatos selecionados: %s",
            ", ".join(f"{candidate.ticker}(score={candidate.score})" for candidate in selected),
        )
    else:
        log.info("Nenhum candidato nesta varredura.")

    return selected
