import requests
import os
from typing import Optional, Dict, Any, List

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
        series = data.get("series", [])
        self.calls = [s for s in series if s.get("side") == "call"]
        self.puts = [s for s in series if s.get("side") == "put"]
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
        session.headers.update({"Authorization": f"Bearer {self.token}"})
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
    
    def get_company_info(self, symbol: str) -> Dict[str, Any]:
        session = self._get_session()
        try:
            url = f"{self.base_url}/company/{symbol}"
            resp = session.get(url, timeout=10)
            resp.raise_for_status()
            return resp.json()
        finally:
            session.close()
    
    def search_symbols(self, query: str) -> list:
        session = self._get_session()
        try:
            url = f"{self.base_url}/search/{query}"
            resp = session.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            return data.get("results", []) if isinstance(data.get("results"), list) else []
        finally:
            session.close()
    
    def get_option_chain(self, symbol: str) -> OptionChainResult:
        session = self._get_session()
        try:
            exp_url = f"{self.base_url}/v2/options/expirations"
            exp_resp = session.get(exp_url, params={"underlying": symbol}, timeout=10)
            exp_resp.raise_for_status()
            exp_data = exp_resp.json()
            expirations = exp_data.get("expirations", [])
            if not expirations:
                return OptionChainResult({})
            expiration_date = expirations[0]
            chain_url = f"{self.base_url}/v2/options/chain"
            chain_resp = session.get(chain_url, params={"underlying": symbol, "expirationDate": expiration_date}, timeout=10)
            chain_resp.raise_for_status()
            chain_data = chain_resp.json()
            return OptionChainResult(chain_data)
        finally:
            session.close()

OptionChain = BrapiClient

def get_quote(symbol: str) -> Quote:
    client = BrapiClient()
    result = client.get_quote(symbol)
    return result

def get_option_chain(symbol: str) -> OptionChainResult:
    client = BrapiClient()
    result = client.get_option_chain(symbol)
    return result
