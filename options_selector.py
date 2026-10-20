"""Escolhe a opcao (call p/ long, put p/ short) com delta, liquidez e spread adequados."""
from datetime import datetime
import config
from iv_filter import pick_expiration, is_weekly_symbol

def select(api, signal, today):
    exps = api.expirations(signal.ticker)
    pick = pick_expiration(exps, today, target=45, lo=config.OPT_MIN_DTE, hi=config.OPT_MAX_DTE)
    if not pick: return None, "sem vencimento mensal entre %d e %d DTE" % (config.OPT_MIN_DTE, config.OPT_MAX_DTE)
    exp, dte = pick
    side = "call" if signal.direction == "long" else "put"
    d_an, an = api.analytics(signal.ticker, exp, side=side)
    d_ch, ch = api.chain(signal.ticker, exp, side=side)
    book = {s["symbol"]: s for s in ch}
    best, why = None, "nenhuma opcao passou nos filtros de delta/liquidez/spread"
    for a in an:
        s = book.get(a["symbol"])
        if not s or a.get("delta") is None or is_weekly_symbol(a["symbol"]): continue
        dl = abs(a["delta"])
        if not (config.DELTA_MIN <= dl <= config.DELTA_MAX): continue
        bid, ask = s.get("bid") or 0, s.get("ask") or 0
        if bid <= 0 or ask <= 0 or ask < bid: continue
        mid = (bid + ask) / 2
        spread = (ask - bid) / mid * 100
        if spread > config.MAX_SPREAD_PCT: continue
        if (s.get("openInterest") or 0) < config.MIN_OPEN_INTEREST or (s.get("volume") or 0) < config.MIN_VOLUME: continue
        cand = dict(symbol=a["symbol"], side=side, strike=a["strike"], expiration=exp, dte=dte, delta=a["delta"],
                    iv=a.get("impliedVolatility"), bid=bid, ask=ask, mid=mid, spread_pct=spread,
                    oi=s.get("openInterest"), volume=s.get("volume"), lot=a.get("allocationRoundLot", 100),
                    ref_date=d_ch)
        if best is None or abs(dl - 0.5) < abs(abs(best["delta"]) - 0.5): best = cand
    if best is None: return None, why
    best["max_loss_per_lot"] = best["ask"] * best["lot"]
    best["stop_premium"] = best["ask"] * (1 - config.OPTION_STOP_LOSS_PCT / 100)
    return best, ""
