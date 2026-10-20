"""IV ATM por ativo, IV Rank e IV Percentile (calculados sobre historico proprio)."""
import logging
from datetime import date as dt_date, datetime
import config, database

log = logging.getLogger("iv")

def iv_rank(series, current):
    lo, hi = min(series), max(series)
    return 0.0 if hi == lo else (current - lo) / (hi - lo) * 100

def iv_percentile(series, current):
    return sum(1 for x in series if x < current) / len(series) * 100

def iv_favoravel(rank, pct):
    return rank is not None and pct is not None and rank <= config.IV_RANK_MAX and pct <= config.IV_PERCENTILE_MAX

def is_monthly(exp):
    """Vencimento mensal da B3: 3a sexta (ou o dia util anterior, se feriado) => dia 15 a 21.
    Os semanais (W1..W5) tem poucas series e pouca liquidez; a IV deles distorce."""
    return 15 <= datetime.strptime(exp, "%Y-%m-%d").day <= 21

def pick_expiration(expirations, today, target=None, lo=None, hi=None, prefer_monthly=True):
    """Vencimento com DTE mais proximo do alvo, dentro de [lo, hi], dando preferencia aos mensais."""
    target, lo, hi = target or config.IV_TARGET_DTE, lo or config.IV_MIN_DTE, hi or config.IV_MAX_DTE
    cands = []
    for e in expirations:
        dte = (datetime.strptime(e, "%Y-%m-%d").date() - today).days
        if lo <= dte <= hi:
            cands.append((e, dte))
    if prefer_monthly and any(is_monthly(e) for e, _ in cands):
        cands = [c for c in cands if is_monthly(c[0])]
    return min(cands, key=lambda c: abs(c[1] - target)) if cands else None

ATM_BAND = 0.07      # so series com strike a ate 7% do preco do ativo
ATM_MIN_SERIES = 2   # menos que isso = dado nao confiavel (melhor sem IV do que IV errada)

def atm_iv(analytics):
    """Media da IV da call e da put mais proximas do dinheiro. None se houver poucas series perto do ATM."""
    rows = [a for a in analytics if a.get("impliedVolatility") and a.get("underlyingPrice") and a.get("strike")]
    if not rows: return None
    spot = rows[0]["underlyingPrice"]
    rows = [a for a in rows if abs(a["strike"] / spot - 1) <= ATM_BAND]
    if len(rows) < ATM_MIN_SERIES: return None
    out = []
    for side in ("call", "put"):
        s = [a for a in rows if a["side"] == side]
        if s:
            out.append(min(s, key=lambda a: abs(a["strike"] - spot))["impliedVolatility"])
    return sum(out) / len(out) if out else None

def evaluate(ticker, window=None):
    """Retorna (iv, rank, pct, approved, note) com base no historico salvo."""
    series = database.iv_series(ticker, window or config.IV_WINDOW)
    if len(series) < config.IV_MIN_HISTORY:
        return (series[-1] if series else None), None, None, False, f"historico insuficiente ({len(series)}/{config.IV_MIN_HISTORY})"
    cur = series[-1]
    r, p = iv_rank(series, cur), iv_percentile(series, cur)
    return cur, r, p, iv_favoravel(r, p), ""

def collect_iv(api, ticker, trade_date=None, today=None):
    """Le analytics do pregao (trade_date=None -> ultimo disponivel) e salva a IV ATM."""
    today = today or (datetime.strptime(trade_date, "%Y-%m-%d").date() if trade_date else dt_date.today())
    exps = api.expirations(ticker, include_expired=trade_date is not None)
    pick = pick_expiration(exps, today)
    loud = trade_date is None            # no backfill (muitas datas) so loga em debug
    if not pick:
        if loud: log.warning("%s: nenhum vencimento entre %d e %d DTE (disponiveis: %s)", ticker, config.IV_MIN_DTE, config.IV_MAX_DTE, ", ".join(exps[:6]) or "nenhum")
        return None
    d, rows = api.analytics(ticker, pick[0], date=trade_date)
    iv = atm_iv(rows)
    if iv is None or not d:
        if loud: log.warning("%s: analytics sem IV ATM (vencimento %s, %d series, %d com IV)", ticker, pick[0], len(rows), sum(1 for a in rows if a.get("impliedVolatility")))
        return None
    database.save_iv(ticker, d, iv, pick[0])
    return d, iv
