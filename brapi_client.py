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
OPTIONS_V2_URL = f"{BASE_URL}/v2/options"


def _log_json(label: str, data: Any, max_len: int = 2000) -> None:
    try:
        txt = json.dumps(data, ensure_ascii=False)
        if len(txt) > max_len:
            txt = txt[:max_len] + "... (truncado)"
    except Exception:
        txt = str(data)[:max_len]
    logger.info("Brapi %s: %s", label, txt)


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
        _log_json(f"quote/{symbol}", data)
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


def _get_json(url: str) -> dict[str, Any] | None:
    """Faz uma requisição à Brapi e devolve JSON de objeto."""
    try:
        resp = requests.get(url, headers=_headers(), timeout=15)
        logger.info("Brapi: tentando %s (status=%s)", url, resp.status_code)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            logger.warning("Brapi: resposta inesperada em %s", url)
            return None
        return data
    except requests.RequestException as e:
        logger.warning("Brapi: falha em %s: %s", url, e)
        return None


def _extract_expirations(data: dict[str, Any]) -> list[str]:
    """Extrai datas de vencimento, tolerando os formatos usuais da API."""
    raw = data.get("expirations") or data.get("results") or data.get("data") or []
    if isinstance(raw, dict):
        raw = raw.get("expirations") or raw.get("items") or []

    expirations: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        value = item if isinstance(item, str) else (
            item.get("expiration") or item.get("expirationDate") or item.get("date")
        )
        if value:
            expirations.append(str(value)[:10])
    return sorted(set(expirations))


def _extract_option_lists(data: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Extrai calls e puts da resposta da cadeia de opções."""
    payload = data.get("chain") or data.get("results") or data.get("data") or data

    if isinstance(payload, list):
        items = payload
        calls = [item for item in items if isinstance(item, dict) and str(
            item.get("type") or item.get("optionType") or item.get("side") or ""
        ).lower() in {"call", "c"}]
        puts = [item for item in items if isinstance(item, dict) and str(
            item.get("type") or item.get("optionType") or item.get("side") or ""
        ).lower() in {"put", "p"}]
        return calls, puts

    if not isinstance(payload, dict):
        return [], []

    calls = payload.get("calls") or []
    puts = payload.get("puts") or []
    if calls or puts:
        return (
            [item for item in calls if isinstance(item, dict)],
            [item for item in puts if isinstance(item, dict)],
        )

    items = payload.get("options") or payload.get("items") or payload.get("series") or []
    if not isinstance(items, list):
        return [], []

    calls: list[dict[str, Any]] = []
    puts: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        option_type = str(
            item.get("type") or item.get("optionType") or item.get("side") or ""
        ).lower()
        symbol = str(item.get("symbol") or "").upper()

        if option_type in {"call", "c"} or (
            not option_type and len(symbol) >= 5 and "A" <= symbol[4] <= "L"
        ):
            calls.append(item)
        elif option_type in {"put", "p"} or (
            not option_type and len(symbol) >= 5 and "M" <= symbol[4] <= "X"
        ):
            puts.append(item)

    return calls, puts


def get_option_chain(underlying: str) -> OptionChain | None:
    """Retorna a cadeia de opções do vencimento mais próximo."""
    symbol = underlying.upper().replace(".SA", "")

    expirations_url = f"{OPTIONS_V2_URL}/expirations?underlying={symbol}"
    expirations_data = _get_json(expirations_url)
    if not expirations_data:
        logger.warning("Brapi: não encontrou vencimentos para %s", symbol)
        return None

    _log_json(f"expirations/{symbol}", expirations_data, max_len=2500)
    expirations = _extract_expirations(expirations_data)
    if not expirations:
        logger.warning("Brapi: resposta sem vencimentos para %s", symbol)
        return None

    expiration = expirations[0]
    chain_url = f"{OPTIONS_V2_URL}/chain?underlying={symbol}&expiration={expiration}"
    chain_data = _get_json(chain_url)
    if not chain_data:
        logger.warning(
            "Brapi: não conseguiu a cadeia de %s para vencimento %s",
            symbol,
            expiration,
        )
        return None

    _log_json(f"chain/{symbol}/{expiration}", chain_data, max_len=2500)
    calls, puts = _extract_option_lists(chain_data)

    if not calls and not puts:
        logger.warning(
            "Brapi: cadeia vazia para %s no vencimento %s",
            symbol,
            expiration,
        )
        return None

    logger.info(
        "Brapi: %d calls e %d puts para %s, vencimento %s",
        len(calls),
        len(puts),
        symbol,
        expiration,
    )
    return OptionChain(underlying=symbol, calls=calls, puts=puts)
