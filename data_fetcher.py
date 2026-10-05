"""Busca de dados de preço, IV e cadeia de opções via Brapi."""
import logging
from datetime import datetime, timedelta
from typing import List, Optional

import pandas as pd
import requests

from config import BRAPI_API_KEY, BRAPI_BASE_URL

logger = logging.getLogger(__name__)


def _get_headers() -> dict:
    """Retorna headers com autenticação Brapi."""
    headers = {"Authorization": f"Bearer {BRAPI_API_KEY}"}
    return headers


def get_price_data(ativo: str, timeframe: int = 5) -> Optional[pd.DataFrame]:
    """
    Obtém dados de preço do ativo via Brapi.
    
    Retorna DataFrame com colunas: ['open', 'high', 'low', 'close', 'volume']
    """
    # Brapi: dados diários (para intraday, usar outro endpoint se disponível)
    url = f"{BRAPI_BASE_URL}/stocks/{ativo}.SA"
    params = {"period": "3mo"}  # últimos 3 meses
    
    try:
        response = requests.get(url, headers=_get_headers(), params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if "results" not in data or not data["results"]:
            logger.warning(f"Sem dados de preço para {ativo}")
            return None
        
        df = pd.DataFrame(data["results"])
        
        # Renomear colunas
        df = df.rename(columns={
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "volume": "volume",
            "date": "date",
        })
        
        df = df[["open", "high", "low", "close", "volume"]]
        df = df.dropna()
        df = df.reset_index(drop=True)
        
        return df
    except Exception as e:
        logger.error(f"Erro ao obter preço de {ativo}: {e}")
        return None


def get_iv_history(ativo: str, days: int = 252) -> List[float]:
    """
    Obtém histórico de IV (volatilidade implícita) via Brapi.
    
    Retorna lista de IV dos últimos `days` dias.
    """
    url = f"{BRAPI_BASE_URL}/options/analytics/history"
    params = {
        "underlying": ativo,
        "days": days,
    }
    
    try:
        response = requests.get(url, headers=_get_headers(), params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if "results" not in data or not data["results"]:
            logger.warning(f"Sem dados de IV para {ativo}")
            return []
        
        # Extrair IV de cada dia
        iv_history = []
        for item in data["results"]:
            iv = item.get("impliedVolatility")
            if iv is not None:
                iv_history.append(iv * 100)  # converter para %
        
        return iv_history
    except Exception as e:
        logger.error(f"Erro ao obter IV de {ativo}: {e}")
        return []


def get_opcoes_cadeia(ativo: str) -> List[dict]:
    """
    Obtém cadeia de opções do ativo via Brapi.
    
    Retorna lista de dicts com:
    - ticker: símbolo da opção
    - tipo: "call" ou "put"
    - strike: preço de exercício
    - vencimento: data de expiração
    - delta: delta da opção
    - preco: prêmio da opção
    - volume_dia: volume negociado no dia
    - open_interest: open interest
    """
    # Passo 1: obter vencimentos disponíveis
    url_exp = f"{BRAPI_BASE_URL}/options/expirations"
    params_exp = {"underlying": ativo}
    
    try:
        response = requests.get(url_exp, headers=_get_headers(), params=params_exp, timeout=10)
        response.raise_for_status()
        data_exp = response.json()
        
        if "results" not in data_exp or not data_exp["results"]:
            logger.warning(f"Sem vencimentos de opções para {ativo}")
            return []
        
        vencimentos = data_exp["results"]
        
        # Selecionar vencimento mais próximo (entre 14-42 dias)
        hoje = datetime.now().date()
        vencimento_selecionado = None
        
        for venc in vencimentos:
            if isinstance(venc, str):
                venc_date = datetime.strptime(venc, "%Y-%m-%d").date()
            else:
                venc_date = venc
            
            dias = (venc_date - hoje).days
            if 14 <= dias <= 42:
                vencimento_selecionado = venc
                break
        
        if not vencimento_selecionado:
            # Pegar o primeiro vencimento disponível
            vencimento_selecionado = vencimentos[0] if vencimentos else None
        
        if not vencimento_selecionado:
            return []
        
        # Passo 2: obter cadeia de opções para o vencimento selecionado
        url_chain = f"{BRAPI_BASE_URL}/options/chain"
        params_chain = {
            "underlying": ativo,
            "expirationDate": vencimento_selecionado if isinstance(vencimento_selecionado, str) else vencimento_selecionado.strftime("%Y-%m-%d"),
        }
        
        response = requests.get(url_chain, headers=_get_headers(), params=params_chain, timeout=10)
        response.raise_for_status()
        data_chain = response.json()
        
        if "results" not in data_chain or not data_chain["results"]:
            logger.warning(f"Sem opções para {ativo} no vencimento {vencimento_selecionado}")
            return []
        
        opcoes = []
        
        for op in data_chain["results"]:
            ticker = op.get("symbol", "")
            side = op.get("side", "").lower()  # "call" ou "put"
            strike = op.get("strike", 0)
            delta = op.get("delta", 0)
            preco = op.get("lastPrice", op.get("close", 0))
            volume = op.get("volume", 0)
            open_interest = op.get("openInterest", op.get("open_interest", 0))
            
            opcoes.append({
                "ticker": ticker,
                "tipo": side,
                "strike": strike,
                "vencimento": vencimento_selecionado if isinstance(vencimento_selecionado, str) else vencimento_selecionado.strftime("%Y-%m-%d"),
                "delta": abs(delta) if delta else 0.45,  # Brapi pode não ter delta
                "preco": preco,
                "volume_dia": volume,
                "open_interest": open_interest,
            })
        
        logger.info(f"Obtidas {len(opcoes)} opções para {ativo}")
        return opcoes
    
    except Exception as e:
        logger.error(f"Erro ao obter opções de {ativo}: {e}")
        return []
