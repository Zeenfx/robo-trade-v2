"""Relatorios curtos de mercado: ativos perto de romper e contagem da IV."""
from datetime import datetime
import config, iv_filter
from messenger import fmt

NEAR_PCT = 1.0     # "perto de romper" = a menos de 1% do nivel de 20 dias
MAX_LINES = 6

def near_breakout(ticker, daily, today=None, tz=None):
    """Retorna (direcao, nivel, ultimo_fechamento, distancia_%) se o ativo esta a menos de NEAR_PCT do nivel de 20 dias; senao None.
    Usa os ultimos BREAKOUT_LOOKBACK candles diarios; se 'today' for dado, ignora o candle de hoje (como o analisador)."""
    rows = daily
    if today is not None:
        rows = [d for d in daily if datetime.fromtimestamp(d["date"], tz).date() < today]
    if len(rows) < config.BREAKOUT_LOOKBACK:
        return None
    window = rows[-config.BREAKOUT_LOOKBACK:]
    hi, lo = max(d["high"] for d in window), min(d["low"] for d in window)
    close = window[-1]["close"]
    if close <= 0:
        return None
    if close <= hi and (hi - close) / close * 100 <= NEAR_PCT:
        return ("up", hi, close, (hi - close) / close * 100)
    if close >= lo and (close - lo) / close * 100 <= NEAR_PCT:
        return ("down", lo, close, (close - lo) / close * 100)
    return None

def near_lines(items, notas=None):
    """items: lista de (ticker, (direcao, nivel, fechamento, dist)). Mais perto primeiro.
    notas: {ticker: texto curto} acrescentado ao fim da linha (ex.: 'balanço 03/11')."""
    notas = notas or {}
    out = []
    for t, (d, lvl, close, dist) in sorted(items, key=lambda x: x[1][3])[:MAX_LINES]:
        extra = f" · ⚠️ {notas[t]}" if notas.get(t) else ""
        out.append(f"{'▲' if d == 'up' else '▼'} {t} · {'máx' if d == 'up' else 'mín'} {fmt(lvl)} · fechou {fmt(close)}{extra}")
    return out

def iv_counts(status_rows):
    """Conta ativos por etiqueta de IV e lista ate 2 dos que estao de fato com etiqueta 'barata' (menor Rank primeiro)."""
    cont = {"cara": 0, "média": 0, "barata": 0}
    rows = [r for r in status_rows if r.get("iv_rank") is not None]
    for r in rows:
        cont[iv_filter.iv_tag(r["iv_rank"], r["iv_pct"]).split(" ", 1)[1]] += 1
    baratos = [r["ticker"] for r in sorted(rows, key=lambda r: r["iv_rank"])
               if iv_filter.iv_tag(r["iv_rank"], r["iv_pct"]).endswith("barata")][:2]
    return cont, baratos
