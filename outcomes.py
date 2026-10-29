"""Placar dos alertas: para cada alerta enviado (com opcao), confere nos candles diarios se a acao bateu a saida, o alvo ou o prazo.
So le dados e conta; nao opera. Regra conservadora: se saida e alvo caem no mesmo dia, conta como saida."""
import json, logging
from datetime import datetime
import config, database
from messenger import fmt

log = logging.getLogger("out")

SCHEMA = """CREATE TABLE IF NOT EXISTS outcomes(date TEXT, ticker TEXT, direction TEXT, entry REAL, stop REAL, partial REAL, target REAL,
result TEXT, days INTEGER, move_pct REAL, partial_hit INTEGER, iv_word TEXT, PRIMARY KEY(date,ticker,direction));"""


def niveis(sig):
    """Mesmos niveis do alerta e do acompanhamento de posicao: saida na metade do caminho, parcial em 60% do caminho ate o alvo."""
    p = sig["price"]
    return round(p + 0.5 * (sig["stop"] - p), 2), round(p + 0.6 * (sig["target"] - p), 2), sig["target"]


def avaliar(sig, bars, prazo=None):
    """bars: candles diarios DEPOIS do dia do alerta (mais antigo primeiro). Retorna (resultado, dias, move_pct, parcial) ou None se ainda aberto."""
    prazo = prazo or config.TIME_EXIT_DAYS
    short = sig["direction"] == "short"
    stop, parcial, alvo = niveis(sig)
    p = sig["price"]
    ph = 0
    for i, b in enumerate(bars[:prazo], 1):
        if (short and b["high"] >= stop) or (not short and b["low"] <= stop):
            return "saida", i, ((stop / p - 1) * (-1 if short else 1)) * 100, ph
        if (short and b["low"] <= alvo) or (not short and b["high"] >= alvo):
            return "alvo", i, ((alvo / p - 1) * (-1 if short else 1)) * 100, 1
        if (short and b["low"] <= parcial) or (not short and b["high"] >= parcial):
            ph = 1
    if len(bars) >= prazo:
        c = bars[prazo - 1]["close"]
        return "prazo", prazo, ((c / p - 1) * (-1 if short else 1)) * 100, ph
    return None


def update(api, today):
    """Resolve os alertas pendentes. Retorna quantos foram resolvidos agora."""
    from messenger import _iv_word
    n = 0
    with database.conn() as c:
        c.execute(SCHEMA.replace("\n", " "))
        rows = c.execute("SELECT date, ticker, direction, payload FROM alerts WHERE date < ? ORDER BY date", (str(today),)).fetchall()
        feitos = {(r["date"], r["ticker"], r["direction"]) for r in c.execute("SELECT date,ticker,direction FROM outcomes").fetchall()}
    for r in rows:
        key = (r["date"], r["ticker"], r["direction"])
        p = json.loads(r["payload"])
        if key in feitos or not p.get("option"):
            continue
        try:
            daily = api.history(r["ticker"], "3mo", "1d")
        except Exception as e:
            log.warning("Placar %s: %s", r["ticker"], e)
            continue
        bars = [b for b in daily if datetime.fromtimestamp(b["date"], _tz()).date().isoformat() > r["date"]]
        res = avaliar(p["signal"], bars)
        if not res:
            continue
        stop, parcial, alvo = niveis(p["signal"])
        ivd = p.get("iv")
        ivw = _iv_word(ivd["iv_rank"], ivd["iv_pct"]) if ivd and ivd.get("iv_rank") is not None else "?"
        with database.conn() as c:
            c.execute("INSERT OR REPLACE INTO outcomes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                      (r["date"], r["ticker"], r["direction"], p["signal"]["price"], stop, parcial, alvo, res[0], res[1], round(res[2], 2), res[3], ivw))
        n += 1
    return n


def _tz():
    from zoneinfo import ZoneInfo
    return ZoneInfo(config.TZ)


def relatorio(today=None):
    with database.conn() as c:
        c.execute(SCHEMA.replace("\n", " "))
        rows = [dict(x) for x in c.execute("SELECT * FROM outcomes ORDER BY date").fetchall()]
        abertos = []
        for r in c.execute("SELECT date, ticker, direction, payload FROM alerts ORDER BY date").fetchall():
            if json.loads(r["payload"]).get("option") and not any(o["date"] == r["date"] and o["ticker"] == r["ticker"] and o["direction"] == r["direction"] for o in rows):
                abertos.append(f"{r['ticker']} {'▲' if r['direction'] == 'long' else '▼'} ({r['date'][8:10]}/{r['date'][5:7]})")
    L = []
    if rows:
        a = sum(1 for r in rows if r["result"] == "alvo"); s = sum(1 for r in rows if r["result"] == "saida"); pz = sum(1 for r in rows if r["result"] == "prazo")
        ph = sum(r["partial_hit"] for r in rows)
        L += [f"Placar dos alertas ({len(rows)} resolvidos)", f"🎯 Alvo: {a} · 🛑 Saída: {s} · ⏰ Prazo: {pz}", f"Chegaram à metade do caminho: {ph} de {len(rows)}"]
        for nome, d in (("Puts", "short"), ("Calls", "long")):
            x = [r for r in rows if r["direction"] == d]
            if x:
                L.append(f"{nome}: alvo em {sum(1 for r in x if r['result'] == 'alvo')} de {len(x)}")
        for w in ("barata", "média", "cara"):
            x = [r for r in rows if r["iv_word"] == w]
            if x:
                L.append(f"IV {w}: alvo em {sum(1 for r in x if r['result'] == 'alvo')} de {len(x)}")
    else:
        L.append("Placar dos alertas: ainda sem alertas resolvidos (cada um leva até 10 pregões).")
    if abertos:
        L.append("Em andamento: " + ", ".join(abertos))
    return "\n".join(L)
