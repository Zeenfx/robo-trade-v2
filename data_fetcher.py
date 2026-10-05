import logging
from datetime import datetime
import pandas as pd
import requests
from config import BRAPI_API_KEY, BRAPI_BASE_URL

logger = logging.getLogger(__name__)


def _headers():
    return {"Authorization": f"Bearer {BRAPI_API_KEY}"} if BRAPI_API_KEY else {}


def get_price_data(ativo, timeframe=5):
    url = f"{BRAPI_BASE_URL}/stocks/{ativo}.SA"
    try:
        resposta = requests.get(url, headers=_headers(), params={"period": "3mo"}, timeout=20)
        resposta.raise_for_status()
        payload = resposta.json()
        resultados = payload.get("results", [])
        if not resultados:
            return None
        if isinstance(resultados, list) and isinstance(resultados[0], dict) and "historicalDataPrice" in resultados[0]:
            resultados = resultados[0]["historicalDataPrice"]
        df = pd.DataFrame(resultados)
        mapa = {"open": "open", "high": "high", "low": "low", "close": "close", "volume": "volume"}
        if not set(mapa).issubset(df.columns):
            logger.warning("Formato de preço inesperado para %s: %s", ativo, list(df.columns))
            return None
        return df[list(mapa)].dropna().reset_index(drop=True)
    except Exception as exc:
        logger.error("Erro ao obter preços de %s: %s", ativo, exc)
        return None


def get_iv_history(ativo, days=252):
    url = f"{BRAPI_BASE_URL}/options/analytics/history"
    try:
        resposta = requests.get(url, headers=_headers(), params={"underlying": ativo, "days": days}, timeout=20)
        resposta.raise_for_status()
        resultados = resposta.json().get("results", [])
        valores = []
        for item in resultados:
            valor = item.get("impliedVolatility", item.get("iv"))
            if valor is not None:
                valor = float(valor)
                valores.append(valor * 100 if valor <= 3 else valor)
        return valores[-days:]
    except Exception as exc:
        logger.error("Erro ao obter IV de %s: %s", ativo, exc)
        return []


def get_opcoes_cadeia(ativo):
    url = f"{BRAPI_BASE_URL}/options/chain"
    try:
        resposta = requests.get(url, headers=_headers(), params={"underlying": ativo}, timeout=20)
        resposta.raise_for_status()
        resultados = resposta.json().get("results", [])
        opcoes = []
        for item in resultados:
            lado = str(item.get("side", item.get("type", ""))).lower()
            if lado not in ("call", "put"):
                continue
            vencimento = item.get("expirationDate", item.get("expiration"))
            if not vencimento:
                continue
            opcoes.append({"ticker": item.get("symbol", item.get("ticker", "")), "tipo": lado, "strike": float(item.get("strike", 0)), "vencimento": vencimento, "delta": float(item.get("delta", 0)), "preco": float(item.get("lastPrice", item.get("close", 0)) or 0), "volume_dia": int(item.get("volume", 0) or 0), "open_interest": int(item.get("openInterest", 0) or 0)})
        logger.info("Cadeia %s: %d opções", ativo, len(opcoes))
        return opcoes
    except Exception as exc:
        logger.error("Erro ao obter cadeia de %s: %s", ativo, exc)
        return []
