import requests
import os
from typing import Optional, Dict, Any, List

class OptionChainResult:
    """Classe para representar o resultado de uma cadeia de opções."""
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
            raise ValueError("BRAPI_TOKEN não configurado. Defina a variável de ambiente BRAPI_TOKEN.")
        self.base_url = "https://brapi.dev/api"
        self._session = requests.Session()
        self._session.headers.update({"Authorization": f"token {self.token}"})

    def close(self):
        self._session.close()

    def get_quote(self, symbol: str) -> Dict[str, Any]:
        url = f"{self.base_url}/quote/{symbol}"
        resp = self._session.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data.get("results", [{}])[0] if isinstance(data.get("results"), list) and len(data["results"]) > 0 else data

    def get_company_info(self, symbol: str) -> Dict[str, Any]:
        url = f"{self.base_url}/company/{symbol}"
        resp = self._session.get(url, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def search_symbols(self, query: str) -> list:
        url = f"{self.base_url}/search/{query}"
        resp = self._session.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data.get("results", []) if isinstance(data.get("results"), list) else []

    def get_option_chain(self, symbol: str) -> OptionChainResult:
        exp_url = f"{self.base_url}/v2/options/expirations"
        exp_resp = self._session.get(exp_url, params={"underlying": symbol}, timeout=10)
        exp_resp.raise_for_status()
        exp_data = exp_resp.json()
        expirations = exp_data.get("expirations", [])
        if not expirations:
            return OptionChainResult({})
        expiration_date = expirations[0]
        chain_url = f"{self.base_url}/v2/options/chain"
        chain_resp = self._session.get(chain_url, params={"underlying": symbol, "expirationDate": expiration_date}, timeout=10)
        chain_resp.raise_for_status()
        chain_data = chain_resp.json()
        return OptionChainResult(chain_data)

OptionChain = BrapiClient

def get_quote(symbol: str) -> Dict[str, Any]:
    client = BrapiClient()
    result = client.get_quote(symbol)
    client.close()
    return result

def get_option_chain(symbol: str) -> OptionChainResult:
    client = BrapiClient()
    result = client.get_option_chain(symbol)
    client.close()
    return result