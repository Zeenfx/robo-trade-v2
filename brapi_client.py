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
        self._session = self._get_session()
    
    def _get_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({"Authorization": f"Bearer {self.token}", "User-Agent": "Mozilla/5.0"})
        return session
    
    def get_quote(self, symbol: str) -> Quote:
        try:
            url = f"{self.base_url}/quote/{symbol}"
            resp = self._session.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            result = data.get("results", [{}])[0] if isinstance(data.get("results"), list) and len(data["results"]) > 0 else data
            return Quote(result)
        except Exception as e:
            logger.error(f"Erro get_quote {symbol}: {e}")
            return Quote({})
    
    def get_option_chain(self, symbol: str, target_dte: int = 30) -> OptionChainResult:
        """
        Busca cadeia de opções real da Brapi API (requer plano Pro).
        
        Args:
            symbol: Símbolo do ativo (ex: PETR4, VALE3)
            target_dte: Dias até o vencimento alvo (default: 30)
        
        Returns:
            OptionChainResult com listas de calls e puts
        """
        try:
            ticker = symbol.upper().replace('3', '').replace('4', '')
            
            # 1. Buscar vencimentos disponíveis
            exp_url = f"{self.base_url}/v2/options/expirations?underlying={ticker}"
            logger.info(f"Buscando vencimentos: {exp_url}")
            exp_resp = self._session.get(exp_url, timeout=10)
            exp_resp.raise_for_status()
            exp_data = exp_resp.json()
            
            expirations = exp_data.get("expirations", [])
            if not expirations:
                logger.warning(f"Sem vencimentos para {ticker}")
                return OptionChainResult({'calls': [], 'puts': []})
            
            # 2. Selecionar vencimento mais próximo do target_dte
            target_date = datetime.now() + timedelta(days=target_dte)
            expiration_date = None
            for exp in sorted(expirations):
                exp_dt = datetime.strptime(exp, "%Y-%m-%d")
                if exp_dt >= target_date:
                    expiration_date = exp
                    break
            
            if not expiration_date:
                expiration_date = expirations[-1]  # Usa último disponível
            
            logger.info(f"Vencimento selecionado: {expiration_date}")
            
            # 3. Buscar cadeia de opções
            chain_url = f"{self.base_url}/v2/options/chain?underlying={ticker}&expirationDate={expiration_date}"
            logger.info(f"Buscando chain: {chain_url}")
            chain_resp = self._session.get(chain_url, timeout=10)
            chain_resp.raise_for_status()
            chain_data = chain_resp.json()
            
            # 4. Parsear séries
            series = chain_data.get("series", [])
            logger.info(f"Series encontradas: {len(series)}")
            
            calls = []
            puts = []
            
            for opt in series:
                side = opt.get("side", "").lower()
                option = {
                    'symbol': opt.get('symbol', ''),
                    'strike': opt.get('strike', 0),
                    'price': opt.get('close', 0) or opt.get('bid', 0) or opt.get('ask', 0) or opt.get('last', 0),
                    'delta': opt.get('delta', 0.5),
                    'gamma': opt.get('gamma', 0),
                    'theta': opt.get('theta', 0),
                    'vega': opt.get('vega', 0),
                    'iv': opt.get('iv', 0),
                    'volume': opt.get('volume', 0),
                    'open_interest': opt.get('openInterest', 0),
                    'side': side
                }
                
                if side == 'call':
                    calls.append(option)
                elif side == 'put':
                    puts.append(option)
            
            logger.info(f"Total: {len(calls)} calls, {len(puts)} puts para {symbol}")
            return OptionChainResult({'calls': calls, 'puts': puts})
            
        except Exception as e:
            logger.error(f"Erro get_option_chain {symbol}: {e}")
            return OptionChainResult({'calls': [], 'puts': []})

OptionChain = BrapiClient

def get_quote(symbol: str) -> Quote:
    client = BrapiClient()
    return client.get_quote(symbol)

def get_option_chain(symbol: str, target_dte: int = 30) -> OptionChainResult:
    client = BrapiClient()
    return client.get_option_chain(symbol, target_dte)
