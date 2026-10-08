"""
Screener - Triagem ampla de candidatos
"""
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
import config
from data_fetcher import get_client

logger = logging.getLogger(__name__)


class Screener:
    """Triagem de ativos candidatos"""
    
    def __init__(self):
        self.client = get_client()
    
    async def get_universe(self) -> List[str]:
        """Retorna lista de ativos elegíveis (com opções e liquidez)"""
        # Por enquanto, lista fixa dos principais ativos com opções
        # Futuramente: busca dinâmica da Brapi + filtro de liquidez
        
        ativos_liquidos = [
            "PETR4", "VALE3", "ITUB4", "BBDC4", "ABEV3", "B3SA3",
            "WEGE3", "RENT3", "LREN3", "SUZB3", "MGLU3", "CMIG4",
            "CSAN3", "ASAI3", "BBAS3", "RAIL3", "ELET3", "EMBR3",
            "HAPV3", "RADL3", "TAEE11", "SANB11", "TRPL4", "CPFE3",
            "EQTL3", "SBSP3", "CSMG3", "VIVT3", "TIMS3", "OIBR3"
        ]
        
        # Futuramente: filtrar por volume médio, preço, etc.
        return ativos_liquidos
    
    async def check_movement(self, quote: Dict[str, Any], historical: List[Dict[str, Any]]) -> bool:
        """Verifica se há movimento relevante de preço"""
        if not quote or not historical:
            return False
        
        # A) Variação intradiária ≥ 2.5%
        change_pct = float(quote.get("changePercent") or 0)
        if abs(change_pct) >= config.MIN_VARIACAO_INTRADIA:
            logger.debug(f"Movimento: variação {change_pct:.2f}%")
            return True
        
        # B) Gap ≥ 1.5%
        if historical and len(historical) >= 2:
            prev_close = float(historical[-2].get("close") or 0)
            open_price = float(quote.get("regularMarketOpen") or quote.get("regularMarketPrice") or 0)
            if prev_close > 0:
                gap = (open_price - prev_close) / prev_close * 100
                if abs(gap) >= config.MIN_GAP:
                    logger.debug(f"Movimento: gap {gap:.2f}%")
                    return True
        
        # C) Expansão de range ≥ 1.8x média últimos 20 dias
        if historical and len(historical) >= 20:
            last_20 = historical[-20:]
            ranges = [float(d.get("high") or 0) - float(d.get("low") or 0) for d in last_20]
            avg_range = sum(ranges[:-1]) / len(ranges[:-1]) if ranges[:-1] else 0
            current_range = float(quote.get("regularMarketDayHigh") or 0) - float(quote.get("regularMarketDayLow") or 0)
            
            if avg_range > 0 and current_range >= avg_range * config.MIN_EXPANSAO_RANGE:
                logger.debug(f"Movimento: expansão de range {current_range/avg_range:.2f}x")
                return True
        
        return False
    
    async def check_volume(self, quote: Dict[str, Any], historical: List[Dict[str, Any]]) -> bool:
        """Verifica se há participação acima do padrão"""
        if not quote:
            return False
        
        # A) Volume financeiro ≥ 1.5x média recente
        current_volume = float(quote.get("volume") or 0)
        price = float(quote.get("regularMarketPrice") or 0)
        current_financial = current_volume * price
        
        if historical and len(historical) >= 20:
            last_20 = historical[-20:-1]  # Exclui hoje
            avg_volume = sum(float(d.get("volume") or 0) * float(d.get("close") or 0) for d in last_20) / len(last_20)
            
            if avg_volume > 0 and current_financial >= avg_volume * config.MIN_VOLUME_FINANCEIRO:
                logger.debug(f"Volume: financeiro {current_financial/avg_volume:.2f}x média")
                return True
        
        # B) Volume relativo ≥ 1.5
        vr = float(quote.get("volumeRatio") or 0)
        if vr >= config.MIN_VOLUME_RELATIVO:
            logger.debug(f"Volume: VR {vr:.2f}")
            return True
        
        return False
    
    async def check_structure(self, symbol: str, historical: List[Dict[str, Any]]) -> bool:
        """Verifica se há estrutura gráfica importante"""
        # Implementação simplificada - futuramente: análise técnica completa
        # Por enquanto, retorna True se houver movimento e volume
        # A análise gráfica detalhada fica no analyzer.py
        
        return True  # Assume que há estrutura se passou pelos outros filtros
    
    async def screen_candidate(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Triagem completa de um ativo"""
        try:
            quote = await self.client.get_quote(symbol)
            if not quote or not quote.get("regularMarketPrice"):
                return None
            
            historical = await self.client.get_historical(symbol, period="1d")
            if not historical:
                return None
            
            # Verifica todos os critérios
            movement = await self.check_movement(quote, historical)
            volume = await self.check_volume(quote, historical)
            structure = await self.check_structure(symbol, historical)
            
            if movement and volume and structure:
                logger.info(f"{symbol}: candidato aprovado (movimento={movement}, volume={volume}, estrutura={structure})")
                return {
                    "symbol": symbol,
                    "quote": quote,
                    "historical": historical,
                    "movement": movement,
                    "volume": volume,
                    "structure": structure,
                }
            
            return None
            
        except Exception as e:
            logger.error(f"Erro na triagem {symbol}: {e}")
            return None
    
    async def run_screening(self) -> List[Dict[str, Any]]:
        """Executa triagem em todo o universo"""
        universe = await self.get_universe()
        candidates = []
        
        for symbol in universe:
            candidate = await self.screen_candidate(symbol)
            if candidate:
                candidates.append(candidate)
        
        # Ordena por intensidade do movimento (maior variação primeiro)
        candidates.sort(key=lambda c: abs(float(c["quote"].get("changePercent") or 0)), reverse=True)
        
        # Limita ao máximo de candidatos por ciclo
        return candidates[:config.MAX_CANDIDATOS_POR_CICLO]
