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


def _sim_opcao(p, r):
    """Resultado estimado da opcao (% sobre o preco do alerta) se entrasse no alerta e saisse no nivel do resultado. None se faltar dado."""
    try:
        import positions
        o, sig = p["option"], p["signal"]
        call = sig["direction"] == "long"
        entry = o["ask"]; K = o["strike"]
        d0 = datetime.strptime(r["date"], "%Y-%m-%d").date()
        exp = datetime.strptime(o["expiration"], "%Y-%m-%d").date()
        spot0 = o.get("spot_ref") or sig["price"]
        iv = positions.implied_vol(call, entry, spot0, K, max((exp - d0).days, 1) / 365.0)
        exit_date = positions.add_pregoes(d0, r["days"])
        S = sig["price"] * (1 + r["move_pct"] / 100.0 * (-1 if not call else 1))
        q = dict(direction=sig["direction"], strike=K, expiration=o["expiration"], iv=iv)
        val = positions.est_option(q, S, exit_date)
        return (val / entry - 1) * 100, (val - entry) * 100
    except Exception as e:
        log.warning("Placar simulacao: %s", e)
        return None


def detalhe():
    """Lista cada alerta resolvido (resultado, quanto a acao andou, opcao estimada) e a soma estimada por 100 opcoes."""
    L, tot, n, favor = [], 0.0, 0, 0
    with database.conn() as c:
        c.execute(SCHEMA.replace("\n", " "))
        rows = [dict(x) for x in c.execute("SELECT * FROM outcomes ORDER BY date").fetchall()]
        pay = {(r["date"], r["ticker"], r["direction"]): json.loads(r["payload"]) for r in c.execute("SELECT date,ticker,direction,payload FROM alerts").fetchall()}
    nome = {"alvo": "🎯 alvo", "saida": "🛑 saída", "prazo": "⏰ prazo"}
    for r in rows:
        sim = _sim_opcao(pay.get((r["date"], r["ticker"], r["direction"])) or {}, r)
        favor += 1 if r["move_pct"] > 0 else 0
        txt = f"{'▲' if r['direction'] == 'long' else '▼'} {r['ticker']} {r['date'][8:10]}/{r['date'][5:7]} · {nome.get(r['result'], r['result'])} em {r['days']}d · ação {fmt(abs(r['move_pct']), 1)}% {'a favor' if r['move_pct'] >= 0 else 'contra'}"
        if sim:
            txt += f" · opção ~{'+' if sim[0] >= 0 else '−'}{fmt(abs(sim[0]), 0)}%"
            tot += sim[1]; n += 1
        L.append(txt)
    if not rows:
        return ""
    out = ["", f"A favor: {favor} de {len(rows)} (a ação andou no sentido do alerta)", "Resolvidos:"] + L
    if n:
        out.append(f"Se entrasse em todos (100 opções cada): {'▲' if tot >= 0 else '▼'} {'+' if tot >= 0 else '−'}R$ {fmt(abs(tot), 0)} (estimado)")
    return "\n".join(out)


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
    d = detalhe()
    if d:
        L.append(d)
    if abertos:
        L.append("")
        L.append("Em andamento: " + ", ".join(abertos))
    return "\n".join(L)
