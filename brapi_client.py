import os
import logging
import requests
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)
TESTE_MODE = os.getenv("TESTE_MODE", "true").lower() == "true"


class Quote:
    def __init__(self, data: Dict[str, Any]):
        self._data = data
        self.price = data.get("regularMarketPrice") or data.get("price") or data.get("lastPrice")
        self.symbol = data.get("symbol", "")

    def __bool__(self):
        return bool(self._data)


class OptionChainResult:
    def __init__(self, data: Dict[str, Any]):
        self._data = data
        self.calls = data.get("calls", [])
        self.puts = data.get("puts", [])

    def __bool__(self):
        return bool(self._data)


class BrapiClient:
    def __init__(self, token: Optional[str] = None):
        self.token = token or os.getenv("BRAPI_TOKEN")
        self.base_url = "https://brapi.dev/api"
        self.session = requests.Session()
        if self.token:
            self.session.headers.update({"Authorization": f"Bearer {self.token}"})
        self.session.headers.update({"User-Agent": "robo-trade-v2/1.0"})

    def get_quote(self, symbol: str) -> Quote:
        try:
            response = self.session.get(f"{self.base_url}/quote/{symbol}", timeout=15)
            response.raise_for_status()
            data = response.json()
            result = data.get("results", [{}])[0] if data.get("results") else data
            return Quote(result)
        except Exception as exc:
            logger.error("Erro get_quote %s: %s", symbol, exc)
            return Quote({})

    def _mock_option_chain(self, symbol: str) -> OptionChainResult:
        quote = self.get_quote(symbol)
        underlying = float(quote.price or 50.0)
        step = max(round(underlying * 0.025, 2), 0.05)
        calls, puts = [], []
        for index in range(-4, 5):
            strike = round(underlying + index * step, 2)
            call_delta = round(max(0.15, min(0.85, 0.52 - index * 0.08)), 2)
            put_delta = round(-max(0.15, min(0.85, 0.48 + index * 0.08)), 2)
            calls.append({"symbol": f"{symbol[:4]}C{int(strike * 100):04d}", "strike": strike, "price": round(max(0.3, underlying * 0.035 - index * step * 0.25), 2), "delta": call_delta, "volume": 1000 + (4 - abs(index)) * 500, "side": "call"})
            puts.append({"symbol": f"{symbol[:4]}P{int(strike * 100):04d}", "strike": strike, "price": round(max(0.3, underlying * 0.035 + index * step * 0.25), 2), "delta": put_delta, "volume": 1000 + (4 - abs(index)) * 500, "side": "put"})
        logger.info("TESTE: %d calls e %d puts simuladas para %s", len(calls), len(puts), symbol)
        return OptionChainResult({"calls": calls, "puts": puts})

    def get_option_chain(self, symbol: str) -> OptionChainResult:
        if TESTE_MODE:
            return self._mock_option_chain(symbol)
        try:
            ticker = symbol.upper()
            expirations = self.session.get(f"{self.base_url}/v2/options/expirations", params={"underlying": ticker}, timeout=15)
            expirations.raise_for_status()
            dates = expirations.json().get("expirations", [])
            if not dates:
                return OptionChainResult({"calls": [], "puts": []})
            chain = self.session.get(f"{self.base_url}/v2/options/chain", params={"underlying": ticker, "expirationDate": dates[0]}, timeout=15)
            chain.raise_for_status()
            series = chain.json().get("series", [])
            calls = [item for item in series if item.get("side", "").lower() == "call"]
            puts = [item for item in series if item.get("side", "").lower() == "put"]
            return OptionChainResult({"calls": calls, "puts": puts})
        except Exception as exc:
            logger.error("Erro get_option_chain %s: %s", symbol, exc)
            return OptionChainResult({"calls": [], "puts": []})


def get_quote(symbol: str) -> Quote:
    return BrapiClient().get_quote(symbol)


def get_option_chain(symbol: str) -> OptionChainResult:
    return BrapiClient().get_option_chain(symbol)
