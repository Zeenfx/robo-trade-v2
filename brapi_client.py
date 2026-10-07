import requests
import os
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

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
        if not self.token:
            raise ValueError("BRAPI_TOKEN nao configurado")
        self.base_url = "https://brapi.dev/api"
    
    def _get_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({"Authorization": f"Bearer {self.token}", "User-Agent": "Mozilla/5.0"})
        return session
    
    def get_quote(self, symbol: str) -> Quote:
        session = self._get_session()
        try:
            url = f"{self.base_url}/quote/{symbol}"
            resp = session.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            result = data.get("results", [{}])[0] if isinstance(data.get("results"), list) and len(data["results"]) > 0 else data
            return Quote(result)
        finally:
            session.close()
    
    def get_option_chain(self, symbol: str) -> OptionChainResult:
        # Dados mock para desenvolvimento
        underlying_price = self.get_quote(symbol).price or 55.0
        # Strikes de -20% a +20% do preço atual
        step = underlying_price * 0.05
        strikes = [round(underlying_price * 0.8 + i * step, 2) for i in range(9)]
        calls = []
        puts = []
        for i, strike in enumerate(strikes):
            moneyness = (underlying_price - strike) / underlying_price
            # Delta varia de ~0.75 (ITM) a ~0.25 (OTM)
            call_delta = round(0.75 - i * 0.07, 2)
            put_delta = round(0.25 + i * 0.07, 2)
            call_price = max(0.5, underlying_price - strike + 2.5)
            put_price = max(0.5, strike - underlying_price + 2.5)
            calls.append({
                'symbol': f"{symbol[:4]}C{int(strike*100)}",
                'strike': strike,
                'price': round(call_price, 2),
                'delta': call_delta,
                'side': 'call'
            })
            puts.append({
                'symbol': f"{symbol[:4]}P{int(strike*100)}",
                'strike': strike,
                'price': round(put_price, 2),
                'delta': put_delta,
                'side': 'put'
            })
        logger.info(f"Mock options: {len(calls)} calls, {len(puts)} puts para {symbol} @ {underlying_price}")
        return OptionChainResult({'calls': calls, 'puts': puts})

OptionChain = BrapiClient

def get_quote(symbol: str) -> Quote:
    client = BrapiClient()
    result = client.get_quote(symbol)
    return result

def get_option_chain(symbol: str) -> OptionChainResult:
    client = BrapiClient()
    result = client.get_option_chain(symbol)
    return result
