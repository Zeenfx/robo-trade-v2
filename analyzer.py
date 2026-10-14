"""Analise D1 + M15: rompimento de maxima/minima com confirmacao, stop (ATR) e alvo."""
from dataclasses import dataclass, asdict
from datetime import datetime
import config

def atr(daily, period=None):
    period = period or config.ATR_PERIOD
    trs = []
    for prev, cur in zip(daily[:-1], daily[1:]):
        trs.append(max(cur["high"] - cur["low"], abs(cur["high"] - prev["close"]), abs(cur["low"] - prev["close"])))
    trs = trs[-period:]
    return sum(trs) / len(trs) if trs else None

def session_fraction(now):
    h = now.hour + now.minute / 60
    return min(max((h - config.SESSION_START_H) / (config.SESSION_END_H - config.SESSION_START_H), 0.05), 1.0)

@dataclass
class Signal:
    ticker: str
    direction: str        # long | short
    price: float
    level: float          # nivel rompido (D1)
    stop: float
    target: float
    atr: float
    vr: float             # ritmo de volume vs media 20d
    trend_d1: str
    h1_note: str
    trigger_m15: str
    space: str            # curto | medio | amplo
    def dict(self): return asdict(self)

def trend_d1(daily):
    c = [d["close"] for d in daily[-20:]]
    if len(c) < 20: return "indefinida"
    sma = sum(c) / len(c)
    if c[-1] > sma * 1.01: return "alta (acima da media de 20d)"
    if c[-1] < sma * 0.99: return "baixa (abaixo da media de 20d)"
    return "lateral"

def analyze(ticker, daily, m15, now):
    """daily: candles D1 (inclui ou nao o dia atual), m15: candles de 15min. Retorna Signal ou None."""
    today = now.date()
    d_today = [d for d in daily if datetime.fromtimestamp(d["date"], now.tzinfo).date() == today]
    hist = [d for d in daily if datetime.fromtimestamp(d["date"], now.tzinfo).date() < today]
    m_today = [m for m in m15 if datetime.fromtimestamp(m["date"], now.tzinfo).date() == today]
    if len(hist) < config.BREAKOUT_LOOKBACK + 1 or len(m_today) < config.M15_CONFIRM_CLOSES:
        return None
    window = hist[-config.BREAKOUT_LOOKBACK:]
    hi, lo = max(d["high"] for d in window), min(d["low"] for d in window)
    a = atr(hist)
    if not a: return None
    price = m_today[-1]["close"]
    last = m_today[-config.M15_CONFIRM_CLOSES:]
    if all(m["close"] > hi for m in last): direction, level = "long", hi
    elif all(m["close"] < lo for m in last): direction, level = "short", lo
    else: return None
    # volume: acumulado do dia vs media de 20d, ajustado pela fracao da sessao
    vol_today = sum(m.get("volume") or 0 for m in m_today)
    avg_vol = sum(d["volume"] for d in window if d.get("volume")) / max(1, sum(1 for d in window if d.get("volume")))
    vr = vol_today / (avg_vol * session_fraction(now)) if avg_vol else 0
    if vr < config.VR_MIN: return None
    sgn = 1 if direction == "long" else -1
    stop = level - sgn * config.STOP_ATR_BUFFER * a
    risk = abs(price - stop)
    if risk <= 0 or risk > config.MAX_STOP_ATR * a: return None
    target = price + sgn * config.RR_TARGET * risk
    # espaco tecnico: distancia ate a proxima barreira (extremo oposto de 60d) em ATRs
    far = [d["high"] for d in hist[-60:]] if direction == "long" else [d["low"] for d in hist[-60:]]
    barrier = max(far) if direction == "long" else min(far)
    room = (barrier - price) * sgn / a if (barrier - price) * sgn > 0 else 0
    space = "amplo" if room >= 4 or room == 0 else "medio" if room >= 2 else "curto"
    h1 = [m for m in m_today]
    h1_note = f"{len(h1)} candles M15 no dia; preco {'acima' if direction=='long' else 'abaixo'} do nivel desde o rompimento"
    trig = f"{config.M15_CONFIRM_CLOSES} fechamentos M15 {'acima' if direction=='long' else 'abaixo'} de {level:.2f}".replace(".", ",")
    return Signal(ticker, direction, price, level, stop, target, a, vr, trend_d1(hist), h1_note, trig, space)
