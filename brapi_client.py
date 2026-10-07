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
        session = self._get_session()
        try:
            ticker = symbol.upper().replace('3', '').replace('4', '')
            # PETR4 é free no sandbox
            if ticker != 'PETR':
                logger.warning(f"Opcoes so free para PETR no sandbox. Usando PETR.")
                ticker = 'PETR'
            target_date = datetime.now() + timedelta(days=30)
            exp_url = f"{self.base_url}/v2/options/expirations?underlying={ticker}"
            logger.info(f"Exp URL: {exp_url}")
            exp_resp = session.get(exp_url, timeout=10)
            logger.info(f"Exp status: {exp_resp.status_code}")
            exp_resp.raise_for_status()
            exp_data = exp_resp.json()
            logger.info(f"Exp response: {exp_data}")
            expirations = exp_data.get("expirations", [])
            if not expirations:
                logger.warning(f"Sem vencimentos para {ticker}")
                return OptionChainResult({'calls': [], 'puts': []})
            expiration_date = None
            for exp in sorted(expirations):
                if exp >= target_date.strftime("%Y-%m-%d"):
                    expiration_date = exp
                    break
            if not expiration_date:
                expiration_date = expirations[-1]
            logger.info(f"Vencimento selecionado: {expiration_date}")
            chain_url = f"{self.base_url}/v2/options/chain?underlying={ticker}&expirationDate={expiration_date}"
            logger.info(f"Chain URL: {chain_url}")
            chain_resp = session.get(chain_url, timeout=10)
            logger.info(f"Chain status: {chain_resp.status_code}")
            chain_resp.raise_for_status()
            chain_data = chain_resp.json()
            logger.info(f"Chain response keys: {chain_data.keys()}")
            series = chain_data.get("series", [])
            logger.info(f"Series count: {len(series)}")
            if series:
                logger.info(f"Primeira serie: {series[0]}")
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
            logger.info(f"Total: {len(calls)} calls, {len(puts)} puts")
            return OptionChainResult({'calls': calls, 'puts': puts})
        except Exception as e:
            logger.error(f"Erro: {e}")
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
