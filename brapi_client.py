"""Cliente simples para a API Brapi."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import requests

from config import BRAPI_TOKEN

logger = logging.getLogger(__name__)

BASE_URL = "https://brapi.dev/api"


@dataclass
class QuoteResult:
    symbol: str
    price: float | None
    change_percent: float | None


@dataclass
class OptionChain:
    underlying: str
    calls: list[dict[str, Any]]
    puts: list[dict[str, Any]]


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {BRAPI_TOKEN}"} if BRAPI_TOKEN else {}


def get_quote(symbol: str) -> QuoteResult | None:
    """Retorna cotação de um ativo ou opção."""
    try:
        url = f"{BASE_URL}/quote/{symbol}"
        resp = requests.get(url, headers=_headers(), timeout=10)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        if not results:
            logger.warning("Brapi: sem resultados para %s", symbol)
            return None
        r = results[0]
        price = r.get("regularMarketPrice") or r.get("previousClose")
        change = r.get("regularMarketChangePercent")
        return QuoteResult(symbol=symbol, price=price, change_percent=change)
    except Exception as e:
        logger.warning("Brapi: erro ao buscar cotação de %s: %s", symbol, e)
        return None


def get_option_chain(underlying: str) -> OptionChain | None:
    """Retorna cadeia de opções de um ativo."""
    try:
        url = f"{BASE_URL}/quote/{underlying}?options=true"
        resp = requests.get(url, headers=_headers(), timeout=10)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        if not results:
            logger.warning("Brapi: sem resultados para cadeia de %s", underlying)
            return None
        r = results[0]
        options = r.get("options", {})
        calls = options.get("calls", []) if isinstance(options, dict) else []
        puts = options.get("puts", []) if isinstance(options, dict) else []
        return OptionChain(underlying=underlying, calls=calls, puts=puts)
    except Exception as e:
        logger.warning("Brapi: erro ao buscar cadeia de opções de %s: %s", underlying, e)
        return None
