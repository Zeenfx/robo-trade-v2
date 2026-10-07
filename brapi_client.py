import httpx
import os
from typing import Optional, Dict, Any

class BrapiClient:
    def __init__(self, token: Optional[str] = None):
        self.token = token or os.getenv("BRAPI_TOKEN")
        if not self.token:
            raise ValueError("BRAPI_TOKEN não configurado. Defina a variável de ambiente BRAPI_TOKEN.")
        self.base_url = "https://brapi.dev/api"
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers={"Authorization": f"token {self.token}"},
                timeout=10.0
            )
        return self._client

    async def close(self):
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def get_quote(self, symbol: str) -> Dict[str, Any]:
        client = await self._get_client()
        url = f"{self.base_url}/quote/{symbol}"
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()
        return data.get("results", [{}])[0] if isinstance(data.get("results"), list) and len(data["results"]) > 0 else data

    async def get_company_info(self, symbol: str) -> Dict[str, Any]:
        client = await self._get_client()
        url = f"{self.base_url}/company/{symbol}"
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()

    async def search_symbols(self, query: str) -> list:
        client = await self._get_client()
        url = f"{self.base_url}/search/{query}"
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()
        return data.get("results", []) if isinstance(data.get("results"), list) else []
