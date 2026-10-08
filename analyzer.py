"""
Analyzer - Análise gráfica D1/H1/M15
"""
import logging
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime
import config

logger = logging.getLogger(__name__)


class Analyzer:
    """Análise gráfica de candidatos"""
    
    def __init__(self):
        pass
    
    def calculate_ma(self, prices: List[float], period: int = 20) -> Optional[float]:
        """Calcula média móvel simples"""
        if len(prices) < period:
            return None
        return sum(prices[-period:]) / period
    
    def identify_trend(self, historical: List[Dict[str, Any]]) -> str:
        """Identifica tendência (alta/baixa/lateral)"""
        if len(historical) < config.MM_PERIOD + 5:
            return "indefinido"
        
        prices = [float(d.get("close") or 0) for d in historical]
        
        # MM20 atual e anterior
        ma_current = self.calculate_ma(prices, config.MM_PERIOD)
        ma_prev = self.calculate_ma(prices[:-5], config.MM_PERIOD)
        
        if not ma_current or not ma_prev:
            return "indefinido"
        
        current_price = prices[-1]
        
        # Tendência de alta
        if current_price > ma_current and ma_current > ma_prev:
            return "alta"
        
        # Tendência de baixa
        if current_price < ma_current and ma_current < ma_prev:
            return "baixa"
        
        # Lateral
        if abs(current_price - ma_current) / ma_current < 0.03:  # ±3%
            return "lateral"
        
        return "indefinido"
    
    def detect_breakout(self, historical: List[Dict[str, Any]], period: int = 5) -> Optional[str]:
        """Detecta rompimento de consolidação"""
        if len(historical) < period + 5:
            return None
        
        # Máxima e mínima da consolidação
        consolidation = historical[-period-5:-5]
        highs = [float(d.get("high") or 0) for d in consolidation]
        lows = [float(d.get("low") or 0) for d in consolidation]
        
        max_high = max(highs)
        min_low = min(lows)
        
        # Candle atual
        current = historical[-1]
        current_high = float(current.get("high") or 0)
        current_low = float(current.get("low") or 0)
        current_close = float(current.get("close") or 0)
        
        # Rompimento de máxima
        if current_close > max_high:
            return "rompimento_alta"
        
        # Rompimento de mínima
        if current_close < min_low:
            return "rompimento_baixa"
        
        return None
    
    def detect_pullback(self, historical: List[Dict[str, Any]]) -> Optional[str]:
        """Detecta pullback em região de rompimento"""
        # Implementação simplificada
        # Verifica se preço retornou à região de rompimento após expansão
        
        if len(historical) < 10:
            return None
        
        prices = [float(d.get("close") or 0) for d in historical]
        
        # Identifica expansão recente
        if len(prices) < 5:
            return None
        
        recent_high = max(prices[-5:])
        recent_low = min(prices[-5:])
        
        # Pullback: preço voltou pra média após expansão
        avg_price = sum(prices[-10:-5]) / 5
        current_price = prices[-1]
        
        if abs(current_price - avg_price) / avg_price < 0.02:  # Retornou à média
            return "pullback"
        
        return None
    
    def detect_rejection(self, historical: List[Dict[str, Any]]) -> Optional[str]:
        """Detecta rejeição em região de oferta/demanda"""
        if len(historical) < 3:
            return None
        
        current = historical[-1]
        open_p = float(current.get("open") or 0)
        close_p = float(current.get("close") or 0)
        high_p = float(current.get("high") or 0)
        low_p = float(current.get("low") or 0)
        
        body = abs(close_p - open_p)
        upper_wick = high_p - max(open_p, close_p)
        lower_wick = min(open_p, close_p) - low_p
        
        # Pavio longo (≥ 2x corpo)
        if body > 0:
            if upper_wick >= body * config.MIN_PAVIO_REJEICAO:
                return "rejeicao_topo"
            if lower_wick >= body * config.MIN_PAVIO_REJEICAO:
                return "rejeicao_fundo"
        
        return None
    
    def identify_support_resistance(self, historical: List[Dict[str, Any]]) -> Tuple[Optional[float], Optional[float]]:
        """Identifica suporte e resistência próximos"""
        if len(historical) < 20:
            return None, None
        
        prices = [float(d.get("close") or 0) for d in historical]
        highs = [float(d.get("high") or 0) for d in historical]
        lows = [float(d.get("low") or 0) for d in historical]
        
        current_price = prices[-1]
        
        # Suporte: mínima recente
        support = min(lows[-10:])
        
        # Resistência: máxima recente
        resistance = max(highs[-10:])
        
        return support, resistance
    
    def calculate_stop_target(self, quote: Dict[str, Any], trend: str, support: float, resistance: float) -> Tuple[Optional[float], Optional[float]]:
        """Calcula stop e alvo técnico"""
        current_price = float(quote.get("regularMarketPrice") or 0)
        
        if not current_price:
            return None, None
        
        if trend == "alta":
            # Stop abaixo do suporte
            stop = support * (1 - config.MAX_STOP_PCT / 100) if support else current_price * 0.97
            # Alvo: resistência ou projeção
            target = resistance if resistance else current_price * 1.06
        
        elif trend == "baixa":
            # Stop acima da resistência
            stop = resistance * (1 + config.MAX_STOP_PCT / 100) if resistance else current_price * 1.03
            # Alvo: suporte ou projeção
            target = support if support else current_price * 0.94
        
        else:
            return None, None
        
        return stop, target
    
    def analyze_candidate(self, candidate: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Análise completa de um candidato"""
        try:
            symbol = candidate["symbol"]
            quote = candidate["quote"]
            historical = candidate["historical"]
            
            # Tendência D1
            trend = self.identify_trend(historical)
            
            # Padrões
            breakout = self.detect_breakout(historical)
            pullback = self.detect_pullback(historical)
            rejection = self.detect_rejection(historical)
            
            # Suporte e resistência
            support, resistance = self.identify_support_resistance(historical)
            
            # Stop e alvo
            stop, target = self.calculate_stop_target(quote, trend, support, resistance)
            
            # Região de interesse
            if trend == "alta":
                region = f"Suporte: R$ {support:.2f}" if support else "N/A"
            elif trend == "baixa":
                region = f"Resistência: R$ {resistance:.2f}" if resistance else "N/A"
            else:
                region = "Lateral - sem direção clara"
            
            # Espaço técnico
            if stop and target and quote.get("regularMarketPrice"):
                risk = abs(float(quote["regularMarketPrice"]) - stop)
                reward = abs(target - float(quote["regularMarketPrice"]))
                risk_reward = reward / risk if risk > 0 else 0
                space = "amplo" if risk_reward >= config.MIN_RISCO_RETORNO else "curto"
            else:
                space = "indefinido"
            
            result = {
                "symbol": symbol,
                "trend": trend,
                "breakout": breakout,
                "pullback": pullback,
                "rejection": rejection,
                "support": support,
                "resistance": resistance,
                "region": region,
                "stop": stop,
                "target": target,
                "space": space,
                "timeframe": "D1",  # Simplificado - futuramente H1 e M15 também
            }
            
            logger.info(f"{symbol}: análise concluída (tendência={trend}, espaço={space})")
            return result
            
        except Exception as e:
            logger.error(f"Erro na análise {candidate['symbol']}: {e}")
            return None
