from dataclasses import dataclass
from enum import Enum
import pandas as pd


class SinalAtivo(Enum):
    CALL = "CALL"
    PUT = "PUT"
    AGUARDAR = "AGUARDAR"


@dataclass
class ResultadoAnalise:
    sinal: SinalAtivo
    preco_atual: float
    variacao_pct: float
    volume_ratio: float
    rompeu_maxima: bool
    perdeu_minima: bool
    tendencia: str
    momentum: str
    motivo: str


def detectar_gatilho(df, lookback=20, volume_mult=1.5, ema_short=20, ema_long=50):
    dados = df.copy().dropna(subset=["close", "high", "low", "volume"])
    if len(dados) < max(ema_long, lookback) + 2:
        raise ValueError("Dados insuficientes para análise")
    close = dados["close"].astype(float)
    ema_curta = close.ewm(span=ema_short, adjust=False).mean()
    ema_longa = close.ewm(span=ema_long, adjust=False).mean()
    macd = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    hist = macd - macd.ewm(span=9, adjust=False).mean()
    preco = float(close.iloc[-1])
    anterior = float(close.iloc[-2])
    variacao = ((preco / anterior) - 1) * 100 if anterior else 0.0
    media_volume = float(dados["volume"].astype(float).rolling(20).mean().iloc[-1])
    volume_ratio = float(dados["volume"].iloc[-1]) / media_volume if media_volume > 0 else 0.0
    maxima = float(dados["high"].iloc[-lookback-1:-1].max())
    minima = float(dados["low"].iloc[-lookback-1:-1].min())
    rompeu = preco > maxima
    perdeu = preco < minima
    alta = preco > float(ema_curta.iloc[-1]) > float(ema_longa.iloc[-1])
    baixa = preco < float(ema_curta.iloc[-1]) < float(ema_longa.iloc[-1])
    macd_up = float(hist.iloc[-1]) > 0 and float(hist.iloc[-1]) > float(hist.iloc[-2])
    macd_down = float(hist.iloc[-1]) < 0 and float(hist.iloc[-1]) < float(hist.iloc[-2])
    if rompeu and alta and macd_up and volume_ratio >= volume_mult:
        return ResultadoAnalise(SinalAtivo.CALL, preco, variacao, volume_ratio, True, False, "alta", "positivo", f"rompeu máxima de {lookback} períodos com volume {volume_ratio:.1f}x a média")
    if perdeu and baixa and macd_down and volume_ratio >= volume_mult:
        return ResultadoAnalise(SinalAtivo.PUT, preco, variacao, volume_ratio, False, True, "baixa", "negativo", f"perdeu mínima de {lookback} períodos com volume {volume_ratio:.1f}x a média")
    tendencia = "alta" if alta else "baixa" if baixa else "neutra"
    momentum = "positivo" if macd_up else "negativo" if macd_down else "neutro"
    motivo = "condições incompletas: " + ("volume insuficiente" if volume_ratio < volume_mult else "sem rompimento confirmado")
    return ResultadoAnalise(SinalAtivo.AGUARDAR, preco, variacao, volume_ratio, rompeu, perdeu, tendencia, momentum, motivo)
