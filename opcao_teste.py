"""Teste (so leitura): qual opcao (vencimento mensal e delta) teria rendido mais nos mesmos sinais, com Black-Scholes e a mesma regra de stop/alvo.
Estimativa para ordenar escolhas; nao e preco real (IV constante, sem sorriso, custo de 3% por lado)."""
from datetime import date, datetime, timedelta
from math import log, sqrt
from statistics import NormalDist, pstdev
from zoneinfo import ZoneInfo
import config, positions, outcomes

_N = NormalDist().cdf
TZ = ZoneInfo(config.TZ)


def terceira_sexta(ano, mes):
    d = date(ano, mes, 1)
    d += timedelta(days=(4 - d.weekday()) % 7 + 14)
    while d.weekday() >= 5 or str(d) in positions.FERIADOS:
        d -= timedelta(days=1)
    return d


def vencimentos(dia, n=2, min_dias=10):
    """Proximos vencimentos mensais com pelo menos min_dias corridos a partir de 'dia'."""
    out, y, m = [], dia.year, dia.month
    while len(out) < n:
        e = terceira_sexta(y, m)
        if (e - dia).days >= min_dias:
            out.append(e)
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def sigma_hist(daily, dia):
    ant = [d["close"] for d in daily if datetime.fromtimestamp(d["date"], TZ).date() < dia][-21:]
    if len(ant) < 10:
        return 0.45
    r = [log(b / a) for a, b in zip(ant[:-1], ant[1:])]
    return min(max(pstdev(r) * sqrt(252) * 1.15, 0.25), 1.2)


def strike_por_delta(call, S, T, v, delta):
    lo, hi = S * 0.5, S * 1.5
    for _ in range(60):
        K = (lo + hi) / 2
        d1 = (log(S / K) + (config.RISK_FREE + v * v / 2) * T) / (v * sqrt(T))
        d = _N(d1) if call else 1 - _N(d1)       # delta em modulo
        if call:
            lo, hi = (K, hi) if d > delta else (lo, K)
        else:
            lo, hi = (lo, K) if d > delta else (K, hi)
    return K


def valor(call, S, K, dias, v):
    intr = max(S - K, 0) if call else max(K - S, 0)
    if dias <= 0:
        return intr
    return max(positions.bs(call, S, K, dias / 365.0, v), intr)


def trajeto(sig, bars, prazo):
    """Percorre candles diarios depois do sinal (mesma regra do placar). Retorna dict(tipo, i, nivel, p_i) ou None se ainda aberto."""
    short = sig["direction"] == "short"
    stop, parcial, alvo = outcomes.niveis(sig)
    p_i = None
    for i, b in enumerate(bars[:prazo], 1):
        if (short and b["high"] >= stop) or (not short and b["low"] <= stop):
            return dict(tipo="saida", i=i, nivel=stop, p_i=p_i)
        if (short and b["low"] <= alvo) or (not short and b["high"] >= alvo):
            return dict(tipo="alvo", i=i, nivel=alvo, p_i=p_i or i)
        if p_i is None and ((short and b["low"] <= parcial) or (not short and b["high"] >= parcial)):
            p_i = i
    if len(bars) >= prazo > 0:
        return dict(tipo="prazo", i=prazo, nivel=bars[prazo - 1]["close"], p_i=p_i)
    return None


def retorno(sig, dia, bars, venc, delta, prazo, sigma, custo=0.03):
    """Retorno (fracao) da opcao de delta dado, vencimento venc, saindo conforme o trajeto; metade vendida no nivel parcial se atingido. None se aberto."""
    call = sig["direction"] == "long"
    S0, dias0 = sig["price"], (venc - dia).days
    if dias0 <= 0:
        return None
    K = strike_por_delta(call, S0, dias0 / 365.0, sigma, delta)
    P0 = valor(call, S0, K, dias0, sigma)
    tr = trajeto(sig, bars, prazo)
    if tr is None or P0 <= 0.01:
        return None
    resto = lambda i: (venc - datetime.fromtimestamp(bars[i - 1]["date"], TZ).date()).days
    fim = valor(call, tr["nivel"], K, resto(tr["i"]), sigma)
    if tr["p_i"]:
        _, parcial, _ = outcomes.niveis(sig)
        fim = 0.5 * valor(call, parcial, K, resto(tr["p_i"]), sigma) + 0.5 * fim
    return fim * (1 - custo) / (P0 * (1 + custo)) - 1
