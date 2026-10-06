"""Cliente simples para a API Brapi."""

from __future__ import annotations

import json
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
        logger.debug("Brapi quote raw (%s): %s", symbol, json.dumps(data, ensure_ascii=False)[:500])
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
    """
    Retorna cadeia de opções de um ativo.

    Tenta:
      1) /market/option/{ticker}
      2) /market/option/{ticker}.SA
      3) /quote/{ticker}?options=true
      4) /quote/{ticker}.SA?options=true
    """
    candidates = [
        f"{BASE_URL}/market/option/{underlying}",
        f"{BASE_URL}/market/option/{underlying}.SA",
        f"{BASE_URL}/quote/{underlying}?options=true",
        f"{BASE_URL}/quote/{underlying}.SA?options=true",
    ]

    for url in candidates:
        try:
            resp = requests.get(url, headers=_headers(), timeout=10)
            resp.raise_for_status()
            data = resp.json()
            logger.debug("Brapi option raw (%s): %s", url, json.dumps(data, ensure_ascii=False)[:800])

            results = data.get("results", [])
            if not results:
                logger.debug("Brapi: sem results em %s", url)
                continue

            r = results[0]
            # Pode vir direto calls/puts ou dentro de options
            calls = r.get("calls") or r.get("options", {}).get("calls") or []
            puts = r.get("puts") or r.get("options", {}).get("puts") or []

            if calls or puts:
                logger.info("Brapi: %d calls e %d puts para %s (via %s)", len(calls), len(puts), underlying, url)
                return OptionChain(underlying=underlying, calls=calls, puts=puts)

            logger.debug("Brapi: calls/puts vazios em %s", url)
        except Exception as e:
            logger.debug("Brapi: falha em %s: %s", url, e)

    logger.warning("Brapi: não conseguiu cadeia de opções para %s em nenhum endpoint", underlying)
    return None
