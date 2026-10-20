"""Avalia sinais registrados em segredo: o preco bateu o alvo ou o stop primeiro? Resultado em R (1R = distancia ate o stop)."""
from datetime import datetime

MAX_DIAS = 15

def outcome(sig, date, daily, tz):
    """Retorna (status, R). status: alvo | stop | aberto | sem_dados. Candle do proprio dia conta; se alvo e stop no mesmo candle, assume stop."""
    entry, stop, target, long_ = sig["price"], sig["stop"], sig["target"], sig["direction"] == "long"
    risk = abs(entry - stop)
    if risk <= 0:
        return "sem_dados", 0.0
    bars = [d for d in daily if datetime.fromtimestamp(d["date"], tz).strftime("%Y-%m-%d") >= date][:MAX_DIAS]
    if not bars:
        return "sem_dados", 0.0
    for b in bars:
        hit_stop = b["low"] <= stop if long_ else b["high"] >= stop
        hit_tgt = b["high"] >= target if long_ else b["low"] <= target
        if hit_stop:
            return "stop", -1.0
        if hit_tgt:
            return "alvo", abs(target - entry) / risk
    last = bars[-1]["close"]
    return "aberto", ((last - entry) if long_ else (entry - last)) / risk

def summary(results):
    lines = [f"Shadow IV: {len(results)} sinais registrados", "Regra: n | alvo/stop/abertos | R medio"]
    for key, name in (("rule_a", "A (25/30)"), ("rule_c", "C (Rank<=60)"), ("rule_b", "B (sem filtro)")):
        sel = [o for r, o in results if r[key] and o[0] != "sem_dados"]
        if not sel:
            lines.append(f"{name}: 0")
            continue
        n = len(sel)
        al, st, ab = (sum(1 for o in sel if o[0] == k) for k in ("alvo", "stop", "aberto"))
        lines.append(f"{name}: {n} | {al}/{st}/{ab} | {sum(o[1] for o in sel) / n:+.2f}R")
    lines.append("R medio: alvo=+2R, stop=-1R, abertos a mercado. Amostra pequena nao prova nada.")
    return "\n".join(lines)
