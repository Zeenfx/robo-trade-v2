"""Telegram."""
import html, logging, requests
import config

log = logging.getLogger("tg")

def send(text):
    if config.DRY_RUN or not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        log.info("[DRY_RUN] %s", text)
        return False
    r = requests.post(f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                      json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=20)
    if r.status_code != 200:
        log.error("Telegram %s %s", r.status_code, r.text[:200])
        return False
    return True

def fmt(x, n=2): return f"{x:,.{n}f}".replace(",", "X").replace(".", ",").replace("X", ".")

def _dm(iso):
    """2026-11-19 -> 19/11"""
    try:
        return f"{iso[8:10]}/{iso[5:7]}"
    except Exception:
        return str(iso)

def alert_msg(sig, iv, opt, opt_err):
    """Uma unica mensagem por alerta: ativo + gatilho, opcao, alvo/stop, risco e IV."""
    long_ = sig.direction == "long"
    kind = "CALL" if long_ else "PUT"
    verbo = "Rompeu" if long_ else "Perdeu"
    L = [f"{'🟢' if long_ else '🔴'} <b>COMPRA {kind} · {sig.ticker}</b>",
         f"{verbo} R$ {fmt(sig.level)} · agora {fmt(sig.price)} · volume {fmt(sig.vr,1)}x", ""]
    if opt:
        L += [f"📌 <code>{opt['symbol']}</code>  (toque para copiar)",
              f"Strike {fmt(opt['strike'])} · vence {_dm(opt['expiration'])} ({opt['dte']} dias) · delta {fmt(abs(opt['delta']))}",
              f"Bid {fmt(opt['bid'])} / Ask {fmt(opt['ask'])} · spread {fmt(opt['spread_pct'],0)}% · OI {fmt(opt['oi'],0)}", ""]
    else:
        L += [f"Sem opção líquida: {html.escape(str(opt_err))}", ""]
    L.append(f"🎯 Alvo {fmt(sig.target)}   🛑 Stop {fmt(sig.stop)}")
    if opt:
        L.append(f"Risco máx. R$ {fmt(opt['max_loss_per_lot'])} por lote · sair se prêmio cair {config.OPTION_STOP_LOSS_PCT}% ({fmt(opt['stop_premium'])})")
    L += ["", f"📊 IV {fmt(iv['iv']*100,0)}% · Rank {fmt(iv['iv_rank'],0)} · Pct {fmt(iv['iv_pct'],0)} ✅"]
    tpl = config.PROFIT_LINK_TEMPLATE
    if tpl and tpl.lower() != "off":
        url = tpl.format(ticker=opt["symbol"] if opt else sig.ticker)
        L.append(f'<a href="{html.escape(url, quote=True)}">Abrir no Profit</a>')
    return "\n".join(L)
