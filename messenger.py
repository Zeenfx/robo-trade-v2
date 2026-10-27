"""Telegram."""
import html, logging, requests
import config

log = logging.getLogger("tg")

MAX_LEN = 3800   # limite do Telegram e 4096; folga para o HTML

def send(text):
    """Envia; textos longos sao divididos em partes (por linha) para o Telegram nao recusar."""
    if len(text) <= MAX_LEN:
        return _send_one(text)
    parts, cur = [], ""
    for line in text.split("\n"):
        if cur and len(cur) + len(line) + 1 > MAX_LEN:
            parts.append(cur); cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    ok = True
    for p in parts:
        ok = _send_one(p) and ok
    return ok

def _send_one(text):
    if config.DRY_RUN or not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        log.info("[DRY_RUN] %s", text)
        return False
    r = requests.post(f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                      json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=20)
    if r.status_code == 400 and "parse" in r.text.lower():
        # HTML invalido (ex.: sinal '<' no texto): reenvia como texto puro para a mensagem nao se perder
        log.warning("Telegram recusou o HTML; reenviando como texto puro: %s", r.text[:120])
        import re
        plain = re.sub(r"<[^>]+>", "", text)
        r = requests.post(f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                          json={"chat_id": config.TELEGRAM_CHAT_ID, "text": html.unescape(plain), "disable_web_page_preview": True}, timeout=20)
    if r.status_code != 200:
        dica = " (token do Telegram invalido ou incompleto: confira a variavel TELEGRAM_TOKEN)" if r.status_code in (401, 404) else ""
        log.error("Telegram %s %s%s", r.status_code, r.text[:200], dica)
        return False
    return True

def fmt(x, n=2): return f"{x:,.{n}f}".replace(",", "X").replace(".", ",").replace("X", ".")

def _dm(iso):
    """2026-11-19 -> 19/11"""
    try:
        return f"{iso[8:10]}/{iso[5:7]}"
    except Exception:
        return str(iso)

def _empate(sig, opt):
    """Empate da opcao (strike +/- preco) e quanto o alvo do ativo anda. Retorna (empate_pct, alvo_pct, alvo_cobre_empate), em % na direcao da operacao."""
    long_ = sig.direction == "long"
    be = opt["strike"] + opt["ask"] if long_ else opt["strike"] - opt["ask"]
    need = (be / sig.price - 1) * 100 if long_ else (1 - be / sig.price) * 100
    alvo = (sig.target / sig.price - 1) * 100 if long_ else (1 - sig.target / sig.price) * 100
    return need, alvo, alvo >= need

def estimar_preco(sig, opt):
    """Opcao so com ultimo negocio: reestima o preco com a cotacao atual da acao (Black-Scholes, IV da brapi ou implicita no ultimo negocio).
    Retorna o preco estimado ou None se nao der para estimar."""
    from math import log, sqrt, exp
    from statistics import NormalDist
    cdf = NormalDist().cdf
    call = sig.direction == "long"
    K, T, r, last = opt["strike"], max(opt.get("dte") or 0, 1) / 365.0, config.RISK_FREE, opt["ask"]
    def bs(S, v):
        d1 = (log(S / K) + (r + v * v / 2) * T) / (v * sqrt(T)); d2 = d1 - v * sqrt(T)
        return S * cdf(d1) - K * exp(-r * T) * cdf(d2) if call else K * exp(-r * T) * cdf(-d2) - S * cdf(-d1)
    try:
        v = opt.get("iv")
        if v and v > 3: v = v / 100.0
        if not v or not (0.05 <= v <= 3):
            S0 = opt.get("spot_ref")
            if not S0: return None
            lo, hi = 0.05, 3.0
            for _ in range(60):
                mid = (lo + hi) / 2
                if bs(S0, mid) > last: hi = mid
                else: lo = mid
            v = (lo + hi) / 2
        est = bs(sig.price, v)
        intr = max(sig.price - K, 0) if call else max(K - sig.price, 0)
        est = max(est, intr)
        return round(est, 2) if est > 0 else None
    except Exception:
        return None

def _iv_word(rank, pct):
    import iv_filter
    return iv_filter.iv_tag(rank, pct).split(" ", 1)[1]    # barata | média | cara

def alert_msg(sig, iv, opt, opt_err, today=None, ibov=None):
    """Mensagem unica e enxuta para celular: direcao (cor), opcao, alvo/stop e veredito (BOM / NEUTRO / RUIM)."""
    long_ = sig.direction == "long"
    verbo = "Rompeu" if long_ else "Perdeu"
    ivw = _iv_word(iv["iv_rank"], iv["iv_pct"])
    L = [f"{'🟢' if long_ else '🔴'} <b>{'CALL' if long_ else 'PUT'} · {sig.ticker}</b>",
         f"{verbo} {fmt(sig.level)} · preço {fmt(sig.price)}" + (f" (das {sig.asof})" if getattr(sig, "asof", None) else "")]
    ext = abs(sig.price - sig.level) / sig.level * 100 if sig.level else 0
    if ext > config.LATE_PCT:
        L.append(f"⚠️ Já andou {fmt(ext,1)}% além do nível")
    if ibov is not None and abs(ibov) >= config.IBOV_ALERT_PCT:
        L.append(f"⚠️ Dia atípico: Ibovespa {fmt(ibov,1)}%. Preço da opção pode estar defasado")
    L.append("")
    ultimo_px = None
    if opt and opt.get("last_only"):
        est = estimar_preco(sig, opt)
        if est:
            ultimo_px = opt["ask"]
            opt = dict(opt, ask=est, bid=est, max_loss_per_lot=est * opt.get("lot", config.OPTION_LOT))
    if opt:
        L += [f"Opção: <code>{opt['symbol']}</code> (toque para copiar)",
              f"Strike {fmt(opt['strike'])} · vence {_dm(opt['expiration'])}",
              f"Preço ~{fmt(opt['ask'])} (R$ {fmt(opt['max_loss_per_lot'],0)} por lote)"]
        if ultimo_px:
            L[-1] = f"Preço estimado ~{fmt(opt['ask'])} (R$ {fmt(opt['max_loss_per_lot'],0)} por lote)"
            L.append(f"⚠️ Estimativa: último negócio foi {fmt(ultimo_px)}, sem compra/venda. Confira o ask no Profit")
        elif opt.get("last_only"):
            L.append("⚠️ Último negócio, sem compra/venda. Confira no Profit")
        L.append("")
    L += [f"Alvo: {fmt(sig.target)}", f"Stop: {fmt(sig.stop)}"]
    if opt:
        if ultimo_px:
            L.append(f"Sair se a ação {'voltar a' if not long_ else 'cair a'} {fmt(sig.stop)} (stop da ação)")
        else:
            L.append(f"Sair se a opção cair a {fmt(opt['stop_premium'])}")
        need, alvo, covers = _empate(sig, opt)
        sinal = "+" if long_ else "-"
        bom_iv = ivw != "cara"
        if covers and bom_iv:
            tag = "✅ BOM"
        elif not covers and not bom_iv:
            tag = "❌ RUIM"
        else:
            tag = "⚠️ NEUTRO"
        alvo_txt = f"alvo {sinal}{fmt(abs(alvo),0)}%" if covers else f"alvo só {sinal}{fmt(abs(alvo),0)}%"
        L += ["", f"<b>{tag}</b>", f"Empate {sinal}{fmt(abs(need),1)}%, {alvo_txt}. IV {ivw}."]
    else:
        L += ["", "<b>⚠️ SEM OPÇÃO</b>", f"{html.escape(str(opt_err)).capitalize() if opt_err else 'Nenhuma opção líquida'}. IV {ivw}."]
    return "\n".join(L)
