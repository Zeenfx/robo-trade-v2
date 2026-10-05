"""Análise técnica para detectar gatilhos de CALL e PUT."""
from dataclasses import dataclass
from enum import Enum
import logging

import pandas as pd

logger = logging.getLogger(__name__)


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


def _ema(serie: pd.Series, periodo: int) -> pd.Series:
    return serie.ewm(span=periodo, adjust=False).mean()


def _rsi(serie: pd.Series, periodo: int = 14) -> pd.Series:
    delta = serie.diff()
    ganhos = delta.clip(lower=0).rolling(periodo).mean()
    perdas = (-delta.clip(upper=0)).rolling(periodo).mean()
    rs = ganhos / perdas.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


def detectar_gatilho(
    df: pd.DataFrame,
    lookback: int = 20,
    volume_mult: float = 1.5,
    ema_short: int = 20,
    ema_long: int = 50,
) -> ResultadoAnalise:
    """Retorna CALL, PUT ou AGUARDAR usando preço, tendência, momentum e volume."""
    dados = df.copy().dropna(subset=["close", "high", "low", "volume"])
    if len(dados) < max(ema_long, lookback, 20) + 2:
        raise ValueError("Dados insuficientes para análise técnica")

    close = dados["close"].astype(float)
    ema_curta = _ema(close, ema_short)
    ema_longa = _ema(close, ema_long)
    macd = _ema(close, 12) - _ema(close, 26)
    macd_hist = macd - _ema(macd, 9)
    rsi = _rsi(close)
    vol_media = dados["volume"].astype(float).rolling(20).mean()

    preco = float(close.iloc[-1])
    anterior = float(close.iloc[-2])
    variacao = ((preco / anterior) - 1) * 100 if anterior else 0.0
    media_volume = float(vol_media.iloc[-1])
    volume_ratio = float(dados["volume"].iloc[-1]) / media_volume if media_volume > 0 else 0.0

    maxima_anterior = float(dados["high"].iloc[-lookback - 1:-1].max())
    minima_anterior = float(dados["low"].iloc[-lookback - 1:-1].min())
    rompeu_maxima = preco > maxima_anterior
    perdeu_minima = preco < minima_anterior

    alta = preco > float(ema_curta.iloc[-1]) > float(ema_longa.iloc[-1])
    baixa = preco < float(ema_curta.iloc[-1]) < float(ema_longa.iloc[-1])
    macd_subindo = float(macd_hist.iloc[-1]) > 0 and float(macd_hist.iloc[-1]) > float(macd_hist.iloc[-2])
    macd_caindo = float(macd_hist.iloc[-1]) < 0 and float(macd_hist.iloc[-1]) < float(macd_hist.iloc[-2])
    rsi_atual = float(rsi.iloc[-1]) if pd.notna(rsi.iloc[-1]) else 50.0

    if rompeu_maxima and alta and macd_subindo and rsi_atual >= 50 and volume_ratio >= volume_mult:
        return ResultadoAnalise(
            SinalAtivo.CALL, preco, variacao, volume_ratio, True, False,
            "alta", "positivo",
            f"rompeu máxima de {lookback} períodos; volume {volume_ratio:.1f}x a média; tendência e momentum de alta confirmados",
        )

    if perdeu_minima and baixa and macd_caindo and rsi_atual <= 50 and volume_ratio >= volume_mult:
        return ResultadoAnalise(
            SinalAtivo.PUT, preco, variacao, volume_ratio, False, True,
            "baixa", "negativo",
            f"perdeu mínima de {lookback} períodos; volume {volume_ratio:.1f}x a média; tendência e momentum de baixa confirmados",
        )

    tendencia = "alta" if alta else "baixa" if baixa else "neutra"
    momentum = "positivo" if macd_subindo else "negativo" if macd_caindo else "neutro"
    motivos = []
    if not rompeu_maxima and not perdeu_minima:
        motivos.append("sem rompimento ou perda de suporte")
    if volume_ratio < volume_mult:
        motivos.append(f"volume abaixo do mínimo ({volume_ratio:.1f}x)")
    if tendencia == "neutra":
        motivos.append("tendência sem alinhamento")
    if momentum == "neutro":
        motivos.append("momentum sem confirmação")

    return ResultadoAnalise(
        SinalAtivo.AGUARDAR, preco, variacao, volume_ratio,
        rompeu_maxima, perdeu_minima, tendencia, momentum,
        "; ".join(motivos) or "condições incompletas",
    )
