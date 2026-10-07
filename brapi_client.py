import requests
import os
import re
from typing import Optional, Dict, Any, List
from bs4 import BeautifulSoup

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
        try:
            ticker = symbol.upper().replace('3', '').replace('4', '')
            url = f"https://statusinvest.com.br/acoes/{ticker}/opcoes"
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, 'html.parser')
            calls = []
            puts = []
            # Buscar todas as tabelas
            tables = soup.find_all('table')
            for table in tables:
                rows = table.find_all('tr')
                for row in rows:
                    cols = row.find_all('td')
                    if len(cols) >= 8:
                        try:
                            symbol_text = cols[0].text.strip()
                            if not symbol_text:
                                continue
                            strike_text = cols[1].text.strip().replace(',', '.')
                            price_text = cols[2].text.strip().replace(',', '.') if cols[2].text.strip() else '0'
                            strike = float(strike_text)
                            price = float(price_text) if price_text else 0.0
                            side = 'call' if 'C' in symbol_text.upper() or 'CALL' in symbol_text.upper() else 'put'
                            option = {
                                'symbol': symbol_text,
                                'strike': strike,
                                'price': price,
                                'delta': 0.5,
                                'side': side
                            }
                            if side == 'call':
                                calls.append(option)
                            else:
                                puts.append(option)
                        except Exception as e:
                            continue
            return OptionChainResult({'calls': calls, 'puts': puts})
        except Exception as e:
            return OptionChainResult({'calls': [], 'puts': []})

OptionChain = BrapiClient

def get_quote(symbol: str) -> Quote:
    client = BrapiClient()
    result = client.get_quote(symbol)
    return result

def get_option_chain(symbol: str) -> OptionChainResult:
    client = BrapiClient()
    result = client.get_option_chain(symbol)
    return result
