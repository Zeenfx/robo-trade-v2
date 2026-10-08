"""
IV Filter - Filtro de volatilidade implícita
"""
import logging
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta
import config
from data_fetcher import get_client

logger = logging.getLogger(__name__)


class IVFilter:
    """Filtro de IV Rank e IV Percentile"""
    
    def __init__(self):
        self.client = get_client()
    
    async def calculate_iv_rank(self, iv_history: List[float], current_iv: float) -> Optional[float]:
        """Calcula IV Rank"""
        if not iv_history or len(iv_history) < config.IV_WINDOW:
            return None
        
        iv_min = min(iv_history)
        iv_max = max(iv_history)
        
        if iv_max == iv_min:
            return 50.0
        
        iv_rank = (current_iv - iv_min) / (iv_max - iv_min) * 100
        return iv_rank
    
    async def calculate_iv_percentile(self, iv_history: List[float], current_iv: float) -> Optional[float]:
        """Calcula IV Percentile"""
        if not iv_history or len(iv_history) < config.IV_WINDOW:
            return None
        
        count_below = sum(1 for iv in iv_history if iv < current_iv)
        iv_percentile = count_below / len(iv_history) * 100
        return iv_percentile
    
    async def get_iv_history(self, symbol: str) -> Optional[List[float]]:
        """Busca histórico de IV (últimos 252 pregões)"""
        try:
            # Busca cadeia de opções mais recente
            expirations = await self.client.get_options_expirations(symbol)
            if not expirations:
                return None
            
            # Pega vencimento mais próximo (máximo 60 dias)
            today = datetime.now().date()
            valid_expirations = []
            for exp in expirations:
                exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
                dte = (exp_date - today).days
                if config.MIN_DTE <= dte <= config.MAX_DTE:
                    valid_expirations.append(exp)
            
            if not valid_expirations:
                return None
            
            # Pega o primeiro vencimento válido
            expiration = valid_expirations[0]
            
            # Busca analytics (IV)
            analytics = await self.client.get_options_analytics(symbol, expiration)
            if not analytics:
                return None
            
            # Extrai IV histórica (simplificado - futuramente buscar histórico completo)
            # Por enquanto, usa IV atual como referência
            current_iv = float(analytics.get("iv") or 0)
            
            # Simula histórico (futuramente: buscar histórico real de IV)
            iv_history = [current_iv * (0.8 + 0.4 * (i / config.IV_WINDOW)) for i in range(config.IV_WINDOW)]
            
            return iv_history
            
        except Exception as e:
            logger.error(f"Erro ao buscar IV histórico {symbol}: {e}")
            return None
    
    async def check_iv(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Verifica IV Rank e IV Percentile"""
        try:
            # Busca histórico de IV
            iv_history = await self.get_iv_history(symbol)
            if not iv_history:
                return None
            
            current_iv = iv_history[-1] if iv_history else 0
            
            # Calcula métricas
            iv_rank = await self.calculate_iv_rank(iv_history, current_iv)
            iv_percentile = await self.calculate_iv_percentile(iv_history, current_iv)
            
            if iv_rank is None or iv_percentile is None:
                return None
            
            # Verifica se é favorável
            iv_favorable = iv_rank <= config.MAX_IV_RANK and iv_percentile <= config.MAX_IV_PERCENTILE
            
            result = {
                "symbol": symbol,
                "iv_current": current_iv,
                "iv_rank": iv_rank,
                "iv_percentile": iv_percentile,
                "iv_favorable": iv_favorable,
            }
            
            if iv_favorable:
                logger.info(f"{symbol}: IV favorável (Rank={iv_rank:.1f}%, Percentile={iv_percentile:.1f}%)")
            else:
                logger.debug(f"{symbol}: IV não favorável (Rank={iv_rank:.1f}%, Percentile={iv_percentile:.1f}%)")
            
            return result
            
        except Exception as e:
            logger.error(f"Erro no filtro IV {symbol}: {e}")
            return None
