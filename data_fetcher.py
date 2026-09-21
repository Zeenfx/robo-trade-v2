"""Robô···Trade — busca de dados de acoes, opcoes e historico via Brapi."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import requests

from config import Config


log = logging.getLogger("robo_trade")


@dataclass
class Quote:
    ticker: str
    last: float
    open: float
    high: float
    low: float
    close_prev: float
    volume: float
    trades: int | None
    change_pct: float
    range_today: float


@dataclass
class HistoryCandle:
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class OptionChain:
    ticker: str
    underlying: str
    expiry: date
    options: list[dict[str, Any]]
    iv_rank: float | None
    iv_percentile: float | None


class BrapiClient:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.session = requests.Session()
        if cfg.brapi_token:
            self.session.headers.update({"Authorization": f"Bearer {cfg.brapi_token}"})

    def _get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = self.session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def list_eligible_tickers(self) -> list[str]:
        """Retorna lista de tickers com opcoes listadas.
        
        Como a Brapi nao tem endpoint publico de 'todos os ativos com opcoes',
        usamos uma lista base e filtramos por disponibilidade de dados.
        Em producao, isso pode vir de um cache ou endpoint proprietario.
        """
        base = [
            "PETR4",
            "VALE3",
            "ITUB4",
            "BBDC4",
            "ABEV3",
            "B3SA3",
            "WEGE3",
            "RENT3",
            "LREN3",
            "MGLU3",
        ]
        eligible = []
        for ticker in base:
            try:
                quote = self.get_quote(ticker)
                if quote.last > 0:
                    eligible.append(ticker)
            except Exception as exc:
                log.warning("%s indisponivel: %s", ticker, exc)
        return eligible

    def get_quote(self, ticker: str) -> Quote:
        payload = self._get(f"{self.cfg.brapi_base_url}/quote/{ticker}")
        results = payload.get("results") or []
        if not results:
            raise ValueError(f"Cotacao nao encontrada para {ticker}")

        q = results[0]
        last = float(q.get("regularMarketPrice") or 0)
        open_p = float(q.get("regularMarketOpen") or 0)
        high = float(q.get("regularMarketDayHigh") or 0)
        low = float(q.get("regularMarketDayLow") or 0)
        close_prev = float(q.get("regularMarketPreviousClose") or 0)
        volume = float(q.get("regularMarketVolume") or 0)
        trades = q.get("regularMarketNumTrades")
        change_pct = float(q.get("regularMarketChangePercent") or 0)

        return Quote(
            ticker=ticker,
            last=last,
            open=open_p,
            high=high,
            low=low,
            close_prev=close_prev,
            volume=volume,
            trades=trades,
            change_pct=change_pct,
            range_today=max(0, high - low),
        )

    def get_history(self, ticker: str, days: int = 60) -> list[HistoryCandle]:
        payload = self._get(
            f"{self.cfg.brapi_base_url}/v2/stocks/historical",
            params={
                "symbols": ticker,
                "range": "3mo",
                "interval": "1d",
            },
        )
        results = payload.get("results") or []
        if not results:
            return []

        first = results[0]
        hist = first.get("historicalDataPrice") or []
        candles = []
        for item in hist[-days:]:
            d = datetime.strptime(item["date"], "%Y-%m-%d").date()
            candles.append(
                HistoryCandle(
                    date=d,
                    open=float(item.get("open", 0)),
                    high=float(item.get("high", 0)),
                    low=float(item.get("low", 0)),
                    close=float(item.get("close", 0)),
                    volume=float(item.get("volume", 0)),
                )
            )
        return candles

    def get_option_chain(self, ticker: str) -> OptionChain | None:
        """Retorna cadeia de opcoes EOD e IV Rank/Percentile.
        
        A Brapi Pro oferece endpoints de opcoes; aqui simulamos a estrutura.
        Em producao, usar endpoint real: /options/{ticker} ou similar.
        """
        try:
            payload = self._get(
                f"{self.cfg.brapi_base_url}/options/{ticker}",
                params={"type": "both"},
            )
        except Exception as exc:
            log.warning("Opcoes nao disponiveis para %s: %s", ticker, exc)
            return None

        results = payload.get("results") or []
        if not results:
            return None

        first = results[0]
        options = first.get("options") or []
        underlying = first.get("underlyingSymbol", ticker)
        expiry_str = first.get("expirationDate")
        iv_rank = first.get("ivRank")
        iv_percentile = first.get("ivPercentile")

        expiry = None
        if expiry_str:
            try:
                expiry = datetime.strptime(expiry_str, "%Y-%m-%d").date()
            except Exception:
                expiry = None

        return OptionChain(
            ticker=ticker,
            underlying=underlying,
            expiry=expiry,
            options=options if isinstance(options, list) else [],
            iv_rank=float(iv_rank) if iv_rank is not None else None,
            iv_percentile=float(iv_percentile) if iv_percentile is not None else None,
        )
