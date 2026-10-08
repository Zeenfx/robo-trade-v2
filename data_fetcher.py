"""
Data Fetcher - Busca dados da Brapi
"""
import httpx
import logging
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
import config

logger = logging.getLogger(__name__)


class BrapiClient:
    """Cliente para API Brapi.dev"""
    
    def __init__(self, token: str = None):
        self.token = token or config.BRAPI_TOKEN
        self.base_url = config.BRAPI_BASE_URL
        self.headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
    
    async def get_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Busca cotação do ativo"""
        try:
            async with httpx.AsyncClient() as client:
                url = f"{self.base_url}/quote/{symbol}"
                response = await client.get(url, headers=self.headers, timeout=10)
                response.raise_for_status()
                data = response.json()
                return data.get("results", {}) if isinstance(data, dict) else {}
        except Exception as e:
            logger.error(f"Erro ao buscar cotação {symbol}: {e}")
            return None
    
    async def get_historical(self, symbol: str, period: str = "1d", start: str = None, end: str = None) -> Optional[List[Dict[str, Any]]]:
        """Busca histórico de preços"""
        try:
            async with httpx.AsyncClient() as client:
                if not start:
                    start = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
                if not end:
                    end = datetime.now().strftime("%Y-%m-%d")
                
                url = f"{self.base_url}/history/{symbol}"
                params = {"start": start, "end": end, "period": period}
                response = await client.get(url, headers=self.headers, params=params, timeout=15)
                response.raise_for_status()
                data = response.json()
                return data.get("results", {}).get("values", []) if isinstance(data, dict) else []
        except Exception as e:
            logger.error(f"Erro ao buscar histórico {symbol}: {e}")
            return []
    
    async def get_options_expirations(self, underlying: str) -> Optional[List[str]]:
        """Busca vencimentos de opções do ativo"""
        try:
            async with httpx.AsyncClient() as client:
                url = f"{self.base_url}/options/expirations"
                params = {"underlying": underlying}
                response = await client.get(url, headers=self.headers, params=params, timeout=10)
                response.raise_for_status()
                data = response.json()
                return data.get("results", {}).get("expirations", []) if isinstance(data, dict) else []
        except Exception as e:
            logger.error(f"Erro ao buscar vencimentos {underlying}: {e}")
            return []
    
    async def get_options_chain(self, underlying: str, expiration: str) -> Optional[Dict[str, Any]]:
        """Busca cadeia de opções para um vencimento"""
        try:
            async with httpx.AsyncClient() as client:
                url = f"{self.base_url}/options/chain"
                params = {"underlying": underlying, "expiration": expiration}
                response = await client.get(url, headers=self.headers, params=params, timeout=15)
                response.raise_for_status()
                data = response.json()
                return data.get("results", {}) if isinstance(data, dict) else {}
        except Exception as e:
            logger.error(f"Erro ao buscar cadeia de opções {underlying} {expiration}: {e}")
            return None
    
    async def get_options_analytics(self, underlying: str, expiration: str) -> Optional[Dict[str, Any]]:
        """Busca analytics de opções (IV, gregas)"""
        try:
            async with httpx.AsyncClient() as client:
                url = f"{self.base_url}/options/analytics"
                params = {"underlying": underlying, "expiration": expiration}
                response = await client.get(url, headers=self.headers, params=params, timeout=15)
                response.raise_for_status()
                data = response.json()
                return data.get("results", {}) if isinstance(data, dict) else {}
        except Exception as e:
            logger.error(f"Erro ao buscar analytics {underlying} {expiration}: {e}")
            return None
    
    async def get_all_stocks(self) -> Optional[List[Dict[str, Any]]]:
        """Busca lista de todos os ativos"""
        try:
            async with httpx.AsyncClient() as client:
                url = f"{self.base_url}/stocks/list"
                response = await client.get(url, headers=self.headers, timeout=15)
                response.raise_for_status()
                data = response.json()
                return data.get("results", []) if isinstance(data, dict) else []
        except Exception as e:
            logger.error(f"Erro ao buscar lista de ativos: {e}")
            return []


# Singleton
_client: Optional[BrapiClient] = None

def get_client() -> BrapiClient:
    global _client
    if _client is None:
        _client = BrapiClient()
    return _client
