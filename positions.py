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
    cols = [r[1] for r in c.execute("PRAGMA table_info(positions)").fetchall()]
    if "origem" not in cols:
        c.execute("ALTER TABLE positions ADD COLUMN origem TEXT DEFAULT 'robo'")


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


def open_position(ticker, qty, price, today, stop=None, spot0=None, partial=None, target=None, deadline=None, option=None, direction=None, level=None, origem="robo", iv=None):
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
            direction, spot0 = sig["direction"], sig["price"]
            target = target or sig["target"]
            sstop = sig["stop"]
            stop = stop or round(spot0 + 0.5 * (sstop - spot0), 2)
            partial = partial or round(spot0 + 0.6 * (target - spot0), 2)
        call = direction == "long"
        dup = c.execute("SELECT 1 FROM positions WHERE status='open' AND symbol=? AND qty0=? AND entry=?", (option["symbol"], qty, price)).fetchone()
        if dup:
            return f"Já cadastrada: {qty} {option['symbol']} a {fmt(price)}. Ignorei a repetição.", None
        T = (datetime.strptime(option["expiration"], "%Y-%m-%d").date() - today).days / 365.0
        if iv is None:
            try:
                iv = implied_vol(call, price, spot0, option["strike"], T)
            except Exception:
                iv = None
        deadline = deadline or str(add_pregoes(today, config.TIME_EXIT_DAYS))
        cur = c.execute("INSERT INTO positions(ticker,symbol,direction,strike,expiration,qty,qty0,entry,spot0,iv,stop,partial,target,deadline,opened,status,flags,realized,origem) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (ticker, option["symbol"], direction, option["strike"], option["expiration"], qty, qty, price, spot0, iv, stop, partial, target,
                         deadline, str(today), "open", "[]", 0.0, origem))
        pid = cur.lastrowid
    lado = "abaixo" if not call else "acima"
    dl = datetime.strptime(deadline, "%Y-%m-%d").strftime("%d/%m")
    return (f"✅ Posição registrada: {qty} {option['symbol']} a {fmt(price)} (R$ {fmt(qty * price, 0)}).\n"
            f"Vou avisar quando {ticker}:\n"
            f"• passar de {fmt(stop)} (sair)\n"
            f"• chegar a {fmt(partial)} (realizar metade)\n"
            f"• chegar ao alvo {fmt(target)}\n"
            f"• e no prazo, {dl}" + (" (10 pregões)." if origem == "robo" else " (perto do vencimento).")), pid


def close_position(ticker, qty, price):
    ticker = ticker.upper()
    with database.conn() as c:
        _ensure(c)
        rows = _rows(c, "status='open' AND (ticker=? OR symbol=?)", (ticker, ticker))
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
    blocos, tot_ap, tot_val, tot_real = [], 0.0, 0.0, 0.0
    for p in rows:
        try:
            px, ult = _last_price(api, p["ticker"], today)
        except Exception:
            px = None
        short = p["direction"] == "short"
        B = [f"{'🔴' if short else '🟢'} <b>{p['symbol']}</b> ({p['ticker']})" + (" · própria" if p.get("origem") == "propria" else ""),
             f"{p['qty']} a {fmt(p['entry'])} · aplicou R$ {fmt(p['qty'] * p['entry'], 0)}"]
        if px:
            est = est_option(p, px, today)
            res = (est - p["entry"]) * p["qty"]; pct = (est / p["entry"] - 1) * 100
            sg = "+" if res >= 0 else "−"
            tot_ap += p["qty"] * p["entry"]; tot_val += est * p["qty"]
            B += [f"Valor atual: ~R$ {fmt(est * p['qty'], 0)} (estimado)", f"Resultado: {'▲' if res >= 0 else '▼'} {sg}R$ {fmt(abs(res), 0)} ({sg}{fmt(abs(pct), 0)}%)", "", f"Ação: {fmt(px)}"]
        tot_real += p["realized"] or 0
        if p["realized"]:
            B.insert(2, f"Já realizado: {'+' if p['realized'] >= 0 else '−'}R$ {fmt(abs(p['realized']), 0)}")
        B += [f"Sair se: {'acima' if short else 'abaixo'} de {fmt(p['stop'])}", f"Metade: {fmt(p['partial'])} · Alvo: {fmt(p['target'])}",
              f"Prazo: {datetime.strptime(p['deadline'], '%Y-%m-%d').strftime('%d/%m')}"]
        blocos.append("\n".join(B))
    if len(rows) > 1 and tot_ap:
        r = tot_val - tot_ap
        sg = "+" if r >= 0 else "−"
        T = [f"<b>Carteira ({len(rows)} opções)</b>", f"Aplicado: R$ {fmt(tot_ap, 0)}", f"Valor atual: ~R$ {fmt(tot_val, 0)} (estimado)",
             f"Resultado: {'▲' if r >= 0 else '▼'} {sg}R$ {fmt(abs(r), 0)} ({sg}{fmt(abs(r / tot_ap * 100), 0)}%)"]
        if tot_real:
            T.append(f"Já realizado: {'+' if tot_real >= 0 else '−'}R$ {fmt(abs(tot_real), 0)}")
        blocos.append("\n".join(T))
    return "\n\n".join(blocos)


def contar_abertas():
    """{'short': n_puts, 'long': n_calls} das posicoes abertas."""
    out = {"short": 0, "long": 0}
    try:
        with database.conn() as c:
            _ensure(c)
            for r in _rows(c):
                out[r["direction"]] = out.get(r["direction"], 0) + 1
    except Exception as e:
        log.warning("Contagem de posicoes: %s", e)
    return out


def nivel_protegido(p, today):
    """Nivel da acao em que a opcao volta ao preco de compra (para subir a saida e proteger o lucro). None se nao der."""
    try:
        call = p["direction"] == "long"
        T = (datetime.strptime(p["expiration"], "%Y-%m-%d").date() - today).days / 365.0
        v = p["iv"] or 0.5
        lo, hi = (p["strike"] * 0.3, p["strike"] * 3)
        for _ in range(60):          # opcao sobe com a acao (call) / cai com a acao (put): busca o nivel em que vale o preco de compra
            m = (lo + hi) / 2
            val = bs(call, m, p["strike"], T, v)
            if (val > p["entry"]) == call: hi = m
            else: lo = m
        return round((lo + hi) / 2, 2)
    except Exception:
        return None


def resumo(api, today):
    """Linhas compactas da carteira para o resumo do fim do dia, com a variacao desde o resumo anterior."""
    with database.conn() as c:
        _ensure(c)
        rows = _rows(c)
    if not rows:
        return []
    L, ap, val, snap = ["Carteira:"], 0.0, 0.0, {}
    for p in rows:
        try:
            px, ult = _last_price(api, p["ticker"], today)
        except Exception:
            px = None
        if not px:
            continue
        est = est_option(p, px, today)
        pct = (est / p["entry"] - 1) * 100
        ap += p["qty"] * p["entry"]; val += est * p["qty"]
        snap[p["symbol"]] = [p["qty"], round(est * p["qty"], 2)]
        L.append(f"{'▲' if pct >= 0 else '▼'} {p['symbol']} {'+' if pct >= 0 else '−'}{fmt(abs(pct), 0)}%")
    if not ap:
        return []
    r = val - ap
    L.append(f"Total: {'▲' if r >= 0 else '▼'} {'+' if r >= 0 else '−'}R$ {fmt(abs(r), 0)} ({'+' if r >= 0 else '−'}{fmt(abs(r / ap * 100), 0)}%) estimado")
    try:    # variacao do dia: compara com o ultimo resumo anterior, so nas posicoes que ja existiam com a mesma quantidade
        datas = json.loads(database.get_meta("cart_dates") or "[]")
        antes = [d for d in datas if d < str(today)]
        if antes:
            ant = json.loads(database.get_meta("cart:" + antes[-1]) or "{}")
            comum = [s for s in snap if s in ant and ant[s][0] == snap[s][0]]
            if comum:
                dif = sum(snap[s][1] - ant[s][1] for s in comum); base = sum(ant[s][1] for s in comum)
                L.append(f"Hoje: {'▲' if dif >= 0 else '▼'} {'+' if dif >= 0 else '−'}R$ {fmt(abs(dif), 0)} ({'+' if dif >= 0 else '−'}{fmt(abs(dif / base * 100), 1)}%)")
        database.set_meta("cart:" + str(today), json.dumps(snap))
        if str(today) not in datas:
            datas.append(str(today))
        database.set_meta("cart_dates", json.dumps(datas[-30:]))
    except Exception as e:
        log.warning("Variacao do dia: %s", e)
    return L


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
            msgs.append(f"🛑 SAIR: {info}\nA ação {'subiu' if short else 'caiu'} até {fmt(p['stop'])}. Sair de {p['symbol']} ({p['qty']}). Confira o bid no Profit.")
        elif "alvo" not in flags and reach(p["target"]):
            flags += ["alvo", "parcial"]
            msgs.append(f"🎯 ALVO: {info}\nA ação chegou ao alvo {fmt(p['target'])}. Realizar o restante de {p['symbol']} ({p['qty']}).")
        elif "parcial" not in flags and reach(p["partial"]):
            flags.append("parcial")
            txt = f"🎯 REALIZAR METADE: {info}\nA ação chegou a {fmt(p['partial'])}. Vender {p['qty'] // 2} de {p['symbol']} e deixar o resto até o alvo {fmt(p['target'])}."
            prot = nivel_protegido(p, today) if est > p["entry"] else None
            if prot:
                txt += f"\n\nProteja o resto (a opção volta ao seu preço de compra em {fmt(prot)}):\n<code>ajustar {p['symbol']} sair {str(prot).replace('.', ',')}</code>"
            msgs.append(txt)
        if "prazo" not in flags and str(today) >= p["deadline"]:
            flags.append("prazo")
            msgs.append(f"⏰ PRAZO: {info}\n" + ("Passaram os 10 pregões" if p.get("origem") != "propria" else "O vencimento está perto") + f". Sair de {p['symbol']} ({p['qty']}), salvo se você tiver um motivo novo.")
        if msgs:
            with database.conn() as c:
                c.execute("UPDATE positions SET flags=? WHERE id=?", (json.dumps(flags), p["id"]))
            for m in msgs:
                send(m)


# ---------- Telegram: leitura de comandos ----------
HELP = ("Use: comprei TICKER QTD a PRECO (ex.: comprei CSAN3 500 a 0,44; opcional: sair 2,65)\n"
        "Opção sua, sem alerta: comprei USIMW700 200 a 0,55 sair 5,90 alvo 5,00\n"
        "vendi CODIGO [QTD] [a PRECO] · ajustar CODIGO alvo X sair Y · posicoes · resultados")


def sub_pregoes(d, n):
    while n > 0:
        d -= timedelta(days=1)
        if d.weekday() < 5 and str(d) not in FERIADOS:
            n -= 1
    return d


def _terceira_sexta(ano, mes):
    d = date(ano, mes, 15)
    while d.weekday() != 4:
        d += timedelta(days=1)
    while str(d) in FERIADOS:
        d -= timedelta(days=1)
        while d.weekday() >= 5: d -= timedelta(days=1)
    return str(d)


def cadastrar_propria(api, code, qty, price, today, stop=None, alvo=None, metade=None):
    """Opcao que nao veio de alerta: descobre ticker, lado, strike e vencimento pelo codigo (brapi; se a serie nao aparecer, pelo proprio codigo)."""
    code = code.upper()
    m = re.match(r"^([A-Z0-9]{4})([A-X])(\d{1,4})$", code)
    if not m or api is None:
        return f"Não reconheci o código {code}. Use o código da opção, ex.: USIMW700."
    root, letra, dig = m.groups()
    call = letra <= "L"
    mes = (ord(letra) - ord("A")) % 12 + 1
    side = "call" if call else "put"
    tickers = [t for t in config.UNIVERSE if t.startswith(root)]
    if not tickers:
        return f"{root} não está entre os 30 ativos do robô, então não consigo ler a opção. Posição não registrada."
    t = tickers[0]
    px, _u = _last_price(api, t, today)
    if not px:
        return f"Não consegui a cotação de {t} agora. Tente de novo em alguns minutos."
    exps = [e for e in api.expirations(t) if int(e[5:7]) == mes and e >= str(today)]
    mensais = [e for e in exps if 15 <= int(e[8:10]) <= 21]
    exp = sorted(mensais or exps)[0] if (mensais or exps) else None
    k = iv = None
    if exp:
        try:
            _, ch = api.chain(t, exp, side=side)
            serie = next((s for s in ch if (s.get("symbol") or "").upper() == code), None)
            k = serie.get("strike") if serie else None
            _, an = api.analytics(t, exp, side=side)
            a_ = next((x for x in an if (x.get("symbol") or "").upper() == code), None)
            iv = a_.get("impliedVolatility") if a_ else None
            if iv and iv > 3: iv /= 100.0
        except Exception as e:
            log.warning("Cadastro %s: %s", code, e)
    aviso = ""
    if not exp:
        ano = today.year if mes >= today.month else today.year + 1
        exp = _terceira_sexta(ano, mes)
    if k is None:     # serie fora da brapi: strike pelo codigo, na escala mais proxima da cotacao
        k = min((int(dig) / s for s in (1, 10, 100)), key=lambda x: abs(x - px))
        aviso = "\n⚠️ Não achei a série na brapi: usei strike " + fmt(k) + " (pelo código) e vencimento " + datetime.strptime(exp, "%Y-%m-%d").strftime("%d/%m") + ". O valor atual fica menos preciso."
    sel = stop or round(px * (1.05 if not call else 0.95), 2)
    alvo = alvo or round(px * (0.90 if not call else 1.10), 2)
    metade = metade or round(px + 0.6 * (alvo - px), 2)
    dl = str(sub_pregoes(datetime.strptime(exp, "%Y-%m-%d").date(), 2))
    option = dict(symbol=code, strike=k, expiration=exp)
    msg, pid = open_position(t, qty, price, today, stop=sel, spot0=px, partial=metade, target=alvo, deadline=dl, option=option,
                             direction="long" if call else "short", origem="propria", iv=iv or 0.5)
    return msg + (aviso if pid else "")


def ajustar(ident, stop=None, alvo=None, metade=None):
    """Troca os niveis (da acao) de uma posicao aberta, pelo codigo da opcao ou ticker. Zera os avisos ja dados dos niveis trocados."""
    ident = ident.upper()
    with database.conn() as c:
        _ensure(c)
        rows = _rows(c, "status='open' AND (ticker=? OR symbol=?)", (ident, ident))
        if not rows:
            return f"Não há posição aberta em {ident}."
        p = rows[-1]
        novo = dict(stop=stop or p["stop"], target=alvo or p["target"], partial=metade or p["partial"])
        if alvo and not metade:      # metade acompanha o alvo: 60% do caminho
            novo["partial"] = round(p["spot0"] + 0.6 * (alvo - p["spot0"]), 2)
        flags = [f for f in json.loads(p["flags"] or "[]") if not ((stop and f == "stop") or (alvo and f in ("alvo", "parcial")) or (metade and f == "parcial"))]
        c.execute("UPDATE positions SET stop=?, target=?, partial=?, flags=? WHERE id=?", (novo["stop"], novo["target"], novo["partial"], json.dumps(flags), p["id"]))
    lado = "acima de" if p["direction"] == "short" else "abaixo de"
    return (f"✅ {p['symbol']} ajustada:\n• sair se {lado} {fmt(novo['stop'])}\n• metade em {fmt(novo['partial'])}\n• alvo {fmt(novo['target'])}")


def _ticker_de(ident):
    """Aceita o ticker (GGBR4) ou o codigo da opcao (GGBRX251); para a opcao, acha o ticker no ultimo alerta que a trouxe."""
    ident = ident.upper()
    if not re.match(r"^[A-Z0-9]{4}[A-X]\d{2,4}$", ident):
        return ident
    with database.conn() as c:
        for r in c.execute("SELECT ticker, payload FROM alerts ORDER BY ts DESC LIMIT 200").fetchall():
            if ((json.loads(r["payload"]).get("option") or {}).get("symbol") or "").upper() == ident:
                return r["ticker"]
    return ident


def handle(text, today, api=None):
    t = (text or "").strip().lower().replace("ç", "c").replace("õ", "o")
    if not t:
        return None
    w = t.split()
    try:
        if w[0] in ("comprei", "compre", "comprado", "comprou"):
            m = re.match(r"\w+\s+([a-z0-9]{4,8})\s+(\d+)\s+(?:a\s+)?([\d.,]+)((?:\s+(?:stop|sair|alvo|metade)\s+[\d.,]+)*)$", t)
            if not m:
                return HELP
            nv = {k: num(v) for k, v in re.findall(r"(stop|sair|alvo|metade)\s+([\d.,]+)", m.group(4))}
            ident, qty, price = m.group(1), int(m.group(2)), num(m.group(3))
            stop = nv.get("sair") or nv.get("stop")
            if re.match(r"^[a-z0-9]{4}[a-x]\d{2,4}$", ident) and not re.match(r"^[a-z]{4}\d{1,2}$", ident) and _ticker_de(ident) == ident.upper():   # codigo de opcao sem alerta do robo
                return cadastrar_propria(api, ident, qty, price, today, stop, nv.get("alvo"), nv.get("metade"))
            msg, _ = open_position(_ticker_de(ident), qty, price, today, stop=stop, target=nv.get("alvo"), partial=nv.get("metade"))
            return msg
        if w[0] in ("ajustar", "ajuste"):
            m = re.match(r"\w+\s+([a-z0-9]{4,8})((?:\s+(?:stop|sair|alvo|metade)\s+[\d.,]+)+)$", t)
            if not m:
                return "Use: ajustar B3SAL160 alvo 17,50 sair 13,90"
            nv = {k: num(v) for k, v in re.findall(r"(stop|sair|alvo|metade)\s+([\d.,]+)", m.group(2))}
            return ajustar(_ticker_de(m.group(1)), nv.get("sair") or nv.get("stop"), nv.get("alvo"), nv.get("metade"))
        if w[0] == "vendi":
            m = re.match(r"vendi\s+([a-z0-9]{4,8})(?:\s+(\d+))?(?:\s+(?:a\s+)?([\d.,]+))?$", t)
            if not m:
                return HELP
            return close_position(_ticker_de(m.group(1)), int(m.group(2)) if m.group(2) else None, num(m.group(3)) if m.group(3) else None)
        if w[0] in ("posicoes", "posicao"):
            return listar(api, today)
        if w[0] in ("resultados", "placar"):
            import outcomes
            if api is not None:
                try: outcomes.update(api, today)
                except Exception: log.exception("Placar")
            return outcomes.relatorio()
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
