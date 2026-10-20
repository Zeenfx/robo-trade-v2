"""Cliente brapi.dev (Pro). Endpoints conforme documentacao em brapi.dev/docs."""
import time, logging
import requests
import config

log = logging.getLogger("data")

class BrapiError(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status

class BrapiPlanError(BrapiError):
    """Token sem acesso ao recurso (401/403): plano Gratuito sem opcoes, chave invalida."""

class Brapi:
    def __init__(self, token=None, base=None):
        self.base = base or config.BRAPI_BASE
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token or config.BRAPI_TOKEN}"

    def _get(self, path, params, retries=3):
        for i in range(retries):
            r = self.s.get(f"{self.base}{path}", params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", 2 * (i + 1))))
                continue
            if r.status_code in (401, 403):
                raise BrapiPlanError(f"{r.status_code} {path} {params} {r.text[:200]}", r.status_code)
            if r.status_code >= 400:
                raise BrapiError(f"{r.status_code} {path} {params} {r.text[:200]}", r.status_code)
            return r.json()
        raise BrapiError(f"429 persistente em {path}", 429)

    def history(self, symbol, range_="3mo", interval="1d"):
        """Candles ordenados do mais antigo ao mais recente. date = unix segundos."""
        j = self._get("/stocks/historical", {"symbols": symbol, "range": range_, "interval": interval, "sortOrder": "asc"})
        res = j.get("results") or []
        if not res:
            return []
        rows = res[0]["data"]["historicalDataPrice"]
        return sorted([r for r in rows if r.get("close") is not None], key=lambda r: r["date"])

    def expirations(self, underlying, include_expired=False):
        p = {"underlying": underlying}
        if include_expired: p["includeExpired"] = "true"
        return self._get("/options/expirations", p).get("expirations", [])

    def analytics(self, underlying, expiration, date=None, side=None):
        p = {"underlying": underlying, "expirationDate": expiration}
        if date: p["date"] = date
        if side: p["side"] = side
        j = self._get("/options/analytics", p)
        return j.get("date"), j.get("analytics", [])

    def chain(self, underlying, expiration, side=None):
        p = {"underlying": underlying, "expirationDate": expiration}
        if side: p["side"] = side
        j = self._get("/options/chain", p)
        return j.get("date"), j.get("series", [])
