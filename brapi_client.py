import requests
import os
from typing import Optional, Dict, Any, List

class OptionChainResult:
    """Classe para representar o resultado de uma cadeia de opções."""
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
        """Retorna a cadeia de opções para o símbolo."""
        # Brapi usa /options/{SYMBOL} para cadeia de opções
        url = f"{self.base_url}/options/{symbol}"
        resp = self._session.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return OptionChainResult(data)

# Aliases para compatibilidade com screener.py
OptionChain = BrapiClient

def get_quote(symbol: str) -> Dict[str, Any]:
    """Função standalone para get_quote."""
    client = BrapiClient()
    result = client.get_quote(symbol)
    client.close()
    return result

def get_option_chain(symbol: str) -> OptionChainResult:
    """Função standalone para get_option_chain."""
    client = BrapiClient()
    result = client.get_option_chain(symbol)
    client.close()
    return result
