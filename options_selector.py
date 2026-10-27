"""Escolhe a opcao (call p/ long, put p/ short) com delta, liquidez e spread adequados."""
from datetime import datetime
import config
from iv_filter import pick_expiration, is_weekly_symbol, is_monthly

def select(api, signal, today):
    """Tenta cada vencimento mensal da janela, do mais proximo de 45 DTE para o mais distante; o primeiro com opcao valida vence."""
    exps = api.expirations(signal.ticker)
    cands = []
    for e in exps:
        dte = (datetime.strptime(e, "%Y-%m-%d").date() - today).days
        if config.OPT_MIN_DTE <= dte <= config.OPT_MAX_DTE and is_monthly(e):
            cands.append((e, dte))
    if not cands: return None, "sem vencimento mensal entre %d e %d DTE" % (config.OPT_MIN_DTE, config.OPT_MAX_DTE)
    first_err = ""
    for exp, dte in sorted(cands, key=lambda c: abs(c[1] - 45)):
        best, err = _select_exp(api, signal, exp, dte)
        if best: return best, ""
        first_err = first_err or err
    return None, first_err

def _select_exp(api, signal, exp, dte):
    side = "call" if signal.direction == "long" else "put"
    d_an, an = api.analytics(signal.ticker, exp, side=side)
    d_ch, ch = api.chain(signal.ticker, exp, side=side)
    book = {s["symbol"]: s for s in ch}
    best = None
    falhas = {"sem cotação": 0, "spread alto": 0, "pouca liquidez": 0}   # series no delta certo que cairam em cada filtro
    no_delta = 0
    for a in an:
        s = book.get(a["symbol"])
        if not s or a.get("delta") is None or is_weekly_symbol(a["symbol"]): continue
        dl = abs(a["delta"])
        if not (config.DELTA_MIN <= dl <= config.DELTA_MAX):
            no_delta += 1
            continue
        bid, ask = s.get("bid") or 0, s.get("ask") or 0
        ultimo = False
        if bid <= 0 or ask <= 0 or ask < bid:
            # sem compra/venda no fechamento: usa o ultimo preco negociado; volume e interesse aberto abaixo seguem valendo
            px = s.get("close") or 0
            if px <= 0:
                falhas["sem cotação"] += 1; continue
            bid = ask = px; ultimo = True
        mid = (bid + ask) / 2
        spread = (ask - bid) / mid * 100
        if spread > config.MAX_SPREAD_PCT:
            falhas["spread alto"] += 1; continue
        if (s.get("openInterest") or 0) < config.MIN_OPEN_INTEREST or (s.get("volume") or 0) < config.MIN_VOLUME:
            falhas["pouca liquidez"] += 1; continue
        cand = dict(symbol=a["symbol"], side=side, strike=a["strike"], expiration=exp, dte=dte, delta=a["delta"],
                    iv=a.get("impliedVolatility"), bid=bid, ask=ask, mid=mid, spread_pct=spread,
                    oi=s.get("openInterest"), volume=s.get("volume"), lot=max(a.get("allocationRoundLot") or 0, config.OPTION_LOT),   # lote padrao B3 = 100; a brapi pode informar 1
                    ref_date=d_ch, last_only=ultimo, spot_ref=a.get("underlyingPrice"))
        if best is None or abs(dl - 0.5) < abs(abs(best["delta"]) - 0.5): best = cand
    if best is None:
        n = sum(falhas.values())
        if n == 0:
            return None, "nenhuma série com delta %s a %s" % (str(config.DELTA_MIN).replace(".", ","), str(config.DELTA_MAX).replace(".", ","))
        motivo, qtd = max(falhas.items(), key=lambda kv: kv[1])
        return None, f"{motivo} ({qtd} de {n} séries)"
    best["max_loss_per_lot"] = best["ask"] * best["lot"]
    best["stop_premium"] = best["ask"] * (1 - config.OPTION_STOP_LOSS_PCT / 100)
    return best, ""


def diagnose(api, ticker, direction, today):
    """Diagnostico (so leitura): por vencimento mensal da janela, quantas series estao na faixa de delta, por que cada uma cai e os dados brutos da serie mais proxima de delta 0,5."""
    side = "call" if direction == "long" else "put"
    out = []
    for e in api.expirations(ticker):
        dte = (datetime.strptime(e, "%Y-%m-%d").date() - today).days
        if not (config.OPT_MIN_DTE <= dte <= config.OPT_MAX_DTE and is_monthly(e)):
            continue
        _, an = api.analytics(ticker, e, side=side)
        _, ch = api.chain(ticker, e, side=side)
        book = {x["symbol"]: x for x in ch}
        r = dict(exp=e, dte=dte, n=len(an), delta=0, sem_cot=0, spread=0, liq=0, ok=0, ex=None)
        best = None
        for a in an:
            x = book.get(a["symbol"])
            if not x or a.get("delta") is None or is_weekly_symbol(a["symbol"]):
                continue
            dl = abs(a["delta"])
            if best is None or abs(dl - 0.5) < abs(best[0] - 0.5):
                best = (dl, a["symbol"], x)
            if not (config.DELTA_MIN <= dl <= config.DELTA_MAX):
                continue
            r["delta"] += 1
            bid, ask = x.get("bid") or 0, x.get("ask") or 0
            if bid <= 0 or ask <= 0 or ask < bid:
                px = x.get("close") or 0
                if px <= 0:
                    r["sem_cot"] += 1; continue
                bid = ask = px
            mid = (bid + ask) / 2
            if (ask - bid) / mid * 100 > config.MAX_SPREAD_PCT:
                r["spread"] += 1; continue
            if (x.get("openInterest") or 0) < config.MIN_OPEN_INTEREST or (x.get("volume") or 0) < config.MIN_VOLUME:
                r["liq"] += 1; continue
            r["ok"] += 1
        if best:
            dl, sym, x = best
            r["ex"] = f"{sym} d{dl:.2f} bid {x.get('bid')} ask {x.get('ask')} ult {x.get('close')} vol {x.get('volume')} OI {x.get('openInterest')}"
        out.append(r)
    return out
