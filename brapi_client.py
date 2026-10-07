import requests
import os
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta

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
        session = self._get_session()
        try:
            ticker = symbol.upper().replace('3', '').replace('4', '')
            target_date = datetime.now() + timedelta(days=30)
            exp_url = f"{self.base_url}/v2/options/expirations?underlying={ticker}"
            exp_resp = session.get(exp_url, timeout=10)
            exp_resp.raise_for_status()
            exp_data = exp_resp.json()
            expirations = exp_data.get("expirations", [])
            if not expirations:
                return OptionChainResult({'calls': [], 'puts': []})
            expiration_date = None
            for exp in sorted(expirations):
                if exp >= target_date.strftime("%Y-%m-%d"):
                    expiration_date = exp
                    break
            if not expiration_date:
                expiration_date = expirations[-1]
            chain_url = f"{self.base_url}/v2/options/chain?underlying={ticker}&expirationDate={expiration_date}"
            chain_resp = session.get(chain_url, timeout=10)
            chain_resp.raise_for_status()
            chain_data = chain_resp.json()
            series = chain_data.get("series", [])
            calls = []
            puts = []
            for opt in series:
                option = {
                    'symbol': opt.get('symbol', ''),
                    'strike': opt.get('strike', 0),
                    'price': opt.get('close', 0) or opt.get('bid', 0) or opt.get('ask', 0),
                    'delta': 0.5,
                    'side': opt.get('side', 'call')
                }
                if option['side'] == 'call':
                    calls.append(option)
                else:
                    puts.append(option)
            return OptionChainResult({'calls': calls, 'puts': puts})
        except Exception as e:
            return OptionChainResult({'calls': [], 'puts': []})
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
