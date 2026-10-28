"""Acompanhamento de posicoes de opcoes (apoio a decisao: nao envia ordens).
Comandos pelo Telegram: comprei TICKER QTD a PRECO [stop X] | vendi TICKER [QTD] [a PRECO] | posicoes."""
import json, logging, re
from datetime import date, datetime, timedelta
from math import log as ln, sqrt, exp
from statistics import NormalDist
import requests
import config, database
from messenger import fmt

log = logging.getLogger("pos")
_cdf = NormalDist().cdf

# feriados em que a B3 nao abre (para contar os pregoes ate a saida por prazo)
FERIADOS = {"2026-11-02", "2026-11-20", "2026-12-24", "2026-12-25", "2026-12-31", "2027-01-01"}

SCHEMA = """CREATE TABLE IF NOT EXISTS positions(id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, symbol TEXT, direction TEXT,
strike REAL, expiration TEXT, qty INTEGER, qty0 INTEGER, entry REAL, spot0 REAL, iv REAL, stop REAL, partial REAL, target REAL,
deadline TEXT, opened TEXT, status TEXT, flags TEXT, realized REAL);"""


def _ensure(c):
    c.execute(SCHEMA.replace("\n", " "))


def add_pregoes(d, n):
    """Data do n-esimo pregao depois de d (pula fins de semana e feriados da lista)."""
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5 and str(d) not in FERIADOS:
            n -= 1
    return d


def bs(call, S, K, T, v):
    T = max(T, 1 / 365.0)
    d1 = (ln(S / K) + (config.RISK_FREE + v * v / 2) * T) / (v * sqrt(T)); d2 = d1 - v * sqrt(T)
    r = config.RISK_FREE
    return S * _cdf(d1) - K * exp(-r * T) * _cdf(d2) if call else K * exp(-r * T) * _cdf(-d2) - S * _cdf(-d1)


def implied_vol(call, price, S, K, T):
    lo, hi = 0.05, 3.0
    for _ in range(60):
        m = (lo + hi) / 2
        if bs(call, S, K, T, m) > price: hi = m
        else: lo = m
    return (lo + hi) / 2


def est_option(p, spot, today):
    """Preco estimado da opcao agora (Black-Scholes com a IV implicita na compra). Nunca abaixo do valor intrinseco."""
    call = p["direction"] == "long"
    T = (datetime.strptime(p["expiration"], "%Y-%m-%d").date() - today).days / 365.0
    v = p["iv"] or 0.5
    intr = max(spot - p["strike"], 0) if call else max(p["strike"] - spot, 0)
    return max(bs(call, spot, p["strike"], T, v), intr)


def num(s):
    return float(s.replace(".", "").replace(",", ".")) if "," in s else float(s)


def _rows(c, where="status='open'", args=()):
    return [dict(r) for r in c.execute(f"SELECT * FROM positions WHERE {where} ORDER BY id", args).fetchall()]


def open_position(ticker, qty, price, today, stop=None, spot0=None, partial=None, target=None, deadline=None, option=None, direction=None, level=None):
    """Abre uma posicao. Sem dados explicitos, usa o ultimo alerta com opcao do ticker. Retorna (mensagem, id|None)."""
    ticker = ticker.upper()
    with database.conn() as c:
        _ensure(c)
        if not option:
            rows = c.execute("SELECT payload FROM alerts WHERE ticker=? ORDER BY ts DESC LIMIT 15", (ticker,)).fetchall()
            alert = next((json.loads(r["payload"]) for r in rows if json.loads(r["payload"]).get("option")), None)
            if not alert:
                return f"Não achei alerta recente de {ticker} com opção. Posição não registrada.", None
            sig, option = alert["signal"], alert["option"]
            direction, spot0, target = sig["direction"], sig["price"], sig["target"]
            sstop = sig["stop"]
            stop = stop or round(spot0 + 0.5 * (sstop - spot0), 2)
            partial = partial or round(spot0 + 0.6 * (target - spot0), 2)
        call = direction == "long"
        T = (datetime.strptime(option["expiration"], "%Y-%m-%d").date() - today).days / 365.0
        try:
            iv = implied_vol(call, price, spot0, option["strike"], T)
        except Exception:
            iv = None
        deadline = deadline or str(add_pregoes(today, config.TIME_EXIT_DAYS))
        cur = c.execute("INSERT INTO positions(ticker,symbol,direction,strike,expiration,qty,qty0,entry,spot0,iv,stop,partial,target,deadline,opened,status,flags,realized) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (ticker, option["symbol"], direction, option["strike"], option["expiration"], qty, qty, price, spot0, iv, stop, partial, target,
                         deadline, str(today), "open", "[]", 0.0))
        pid = cur.lastrowid
    lado = "abaixo" if not call else "acima"
    dl = datetime.strptime(deadline, "%Y-%m-%d").strftime("%d/%m")
    return (f"✅ Posição registrada: {qty} {option['symbol']} a {fmt(price)} (R$ {fmt(qty * price, 0)}).\n"
            f"Vou avisar quando {ticker}:\n"
            f"• chegar ao stop {fmt(stop)} (sair)\n"
            f"• chegar a {fmt(partial)} (realizar metade)\n"
            f"• chegar ao alvo {fmt(target)}\n"
            f"• e no prazo, {dl} (10 pregões)."), pid


def close_position(ticker, qty, price):
    ticker = ticker.upper()
    with database.conn() as c:
        _ensure(c)
        rows = _rows(c, "status='open' AND ticker=?", (ticker,))
        if not rows:
            return f"Não há posição aberta em {ticker}."
        p = rows[-1]
        q = min(qty or p["qty"], p["qty"])
        res = (price - p["entry"]) * q if price is not None else 0.0
        left = p["qty"] - q
        c.execute("UPDATE positions SET qty=?, realized=?, status=? WHERE id=?", (left, (p["realized"] or 0) + res, "open" if left > 0 else "closed", p["id"]))
        total = (p["realized"] or 0) + res
    txt = f"Registrado: vendeu {q} {p['symbol']}" + (f" a {fmt(price)} → {'+' if res >= 0 else '−'}R$ {fmt(abs(res), 0)}" if price is not None else "") + "."
    txt += f" Restam {left}." if left > 0 else (f" Posição encerrada. Resultado total: {'+' if total >= 0 else '−'}R$ {fmt(abs(total), 0)}." if price is not None else " Posição encerrada.")
    return txt


def _last_price(api, ticker, today):
    m15 = api.history(ticker, "5d", "15m")
    if not m15: return None, None
    ult = datetime.fromtimestamp(m15[-1]["date"], config_tz())
    return m15[-1]["close"], ult


def config_tz():
    from zoneinfo import ZoneInfo
    return ZoneInfo(config.TZ)


def listar(api, today):
    with database.conn() as c:
        _ensure(c)
        rows = _rows(c)
    if not rows:
        return "Nenhuma posição aberta."
    L = ["Posições abertas"]
    for p in rows:
        try:
            px, ult = _last_price(api, p["ticker"], today)
        except Exception:
            px = None
        L.append(f"• {p['symbol']} ({p['ticker']}): {p['qty']} a {fmt(p['entry'])}")
        if px:
            est = est_option(p, px, today)
            pct = (est / p["entry"] - 1) * 100
            L.append(f"   ação {fmt(px)} · opção ~{fmt(est)} ({'+' if pct >= 0 else '−'}{fmt(abs(pct), 0)}%, {'+' if est >= p['entry'] else '−'}R$ {fmt(abs(est - p['entry']) * p['qty'], 0)}) estimado")
        L.append(f"   stop {fmt(p['stop'])} · metade {fmt(p['partial'])} · alvo {fmt(p['target'])} · prazo {datetime.strptime(p['deadline'], '%Y-%m-%d').strftime('%d/%m')}")
    return "\n".join(L)


def check(api, now_dt):
    """Avisa (uma vez cada) stop, saida parcial, alvo e prazo. Roda a cada poucos minutos no pregao."""
    today = now_dt.date()
    with database.conn() as c:
        _ensure(c)
        rows = _rows(c)
    from messenger import send
    for p in rows:
        flags = json.loads(p["flags"] or "[]")
        try:
            px, ult = _last_price(api, p["ticker"], today)
        except Exception as e:
            log.warning("Posicao %s: preco indisponivel (%s)", p["ticker"], e)
            continue
        if px is None or ult.date() != today:
            continue
        short = p["direction"] == "short"
        hit = (lambda lvl: px >= lvl) if short else (lambda lvl: px <= lvl)
        reach = (lambda lvl: px <= lvl) if short else (lambda lvl: px >= lvl)
        est = est_option(p, px, today)
        pct = (est / p["entry"] - 1) * 100
        info = f"{p['ticker']} {fmt(px)} · opção ~{fmt(est)} estimado ({'+' if pct >= 0 else '−'}{fmt(abs(pct), 0)}%)"
        msgs = []
        if "stop" not in flags and hit(p["stop"]):
            flags.append("stop")
            msgs.append(f"🛑 STOP: {info}\nA ação tocou o stop {fmt(p['stop'])}. Sair de {p['symbol']} ({p['qty']}). Confira o bid no Profit.")
        elif "alvo" not in flags and reach(p["target"]):
            flags += ["alvo", "parcial"]
            msgs.append(f"🎯 ALVO: {info}\nA ação chegou ao alvo {fmt(p['target'])}. Realizar o restante de {p['symbol']} ({p['qty']}).")
        elif "parcial" not in flags and reach(p["partial"]):
            flags.append("parcial")
            msgs.append(f"🎯 REALIZAR METADE: {info}\nA ação chegou a {fmt(p['partial'])}. Vender {p['qty'] // 2} de {p['symbol']} e deixar o resto até o alvo {fmt(p['target'])}.")
        if "prazo" not in flags and str(today) >= p["deadline"]:
            flags.append("prazo")
            msgs.append(f"⏰ PRAZO: {info}\nPassaram os 10 pregões. Sair de {p['symbol']} ({p['qty']}), salvo se você tiver um motivo novo.")
        if msgs:
            with database.conn() as c:
                c.execute("UPDATE positions SET flags=? WHERE id=?", (json.dumps(flags), p["id"]))
            for m in msgs:
                send(m)


# ---------- Telegram: leitura de comandos ----------
HELP = ("Use: comprei TICKER QTD a PRECO (ex.: comprei CSAN3 500 a 0,44; opcional: stop 2,65)\n"
        "vendi TICKER [QTD] [a PRECO] · posicoes")


def handle(text, today, api=None):
    t = (text or "").strip().lower().replace("ç", "c").replace("õ", "o")
    if not t:
        return None
    w = t.split()
    try:
        if w[0] == "comprei":
            m = re.match(r"comprei\s+([a-z0-9]{4,6})\s+(\d+)\s+(?:a\s+)?([\d.,]+)(?:\s+stop\s+([\d.,]+))?$", t)
            if not m:
                return HELP
            msg, _ = open_position(m.group(1), int(m.group(2)), num(m.group(3)), today, stop=num(m.group(4)) if m.group(4) else None)
            return msg
        if w[0] == "vendi":
            m = re.match(r"vendi\s+([a-z0-9]{4,6})(?:\s+(\d+))?(?:\s+(?:a\s+)?([\d.,]+))?$", t)
            if not m:
                return HELP
            return close_position(m.group(1), int(m.group(2)) if m.group(2) else None, num(m.group(3)) if m.group(3) else None)
        if w[0] in ("posicoes", "posicao"):
            return listar(api, today)
    except Exception as e:
        log.exception("Comando falhou: %s", text)
        return f"Não consegui processar ({type(e).__name__}). {HELP}"
    return None


def poll(api, today):
    """Le mensagens novas do Telegram (so do seu chat) e responde aos comandos."""
    if config.DRY_RUN or not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        return
    from messenger import send
    off = database.get_meta("tg_offset")
    params = {"timeout": 0, "allowed_updates": json.dumps(["message"])}
    if off: params["offset"] = int(off)
    r = requests.get(f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getUpdates", params=params, timeout=20)
    if r.status_code != 200:
        log.warning("getUpdates %s %s", r.status_code, r.text[:120])
        return
    ups = r.json().get("result", [])
    if not ups:
        if not off: database.set_meta("tg_offset", "0")   # marca a 1a leitura: mensagens futuras serao processadas
        return
    database.set_meta("tg_offset", str(ups[-1]["update_id"] + 1))
    if not off:   # 1a leitura: descarta mensagens antigas
        return
    for u in ups:
        m = u.get("message") or {}
        if str((m.get("chat") or {}).get("id")) != str(config.TELEGRAM_CHAT_ID):
            continue
        reply = handle(m.get("text"), today, api)
        if reply:
            send(reply)
