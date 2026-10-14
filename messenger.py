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

def short_msg(sig, iv):
    return (f"<b>{sig.ticker}</b> — análise prioritária\nCenário: {sig.direction.upper()}\n"
            f"Timeframe principal: M15 (confirmação em D1)\n"
            f"Status: IV aprovado (Rank {fmt(iv['iv_rank'],1)}%, Percentile {fmt(iv['iv_pct'],1)}%)")

def full_msg(sig, iv, opt, opt_err):
    lines = [f"<b>{sig.ticker}</b> — análise completa (toque para copiar: <code>{sig.ticker}</code>)",
        f"Tendência D1: {sig.trend_d1}",
        f"Estrutura H1/M15: {sig.h1_note}",
        f"Gatilho M15: {sig.trigger_m15}",
        f"Região de interesse: rompimento de R$ {fmt(sig.level)} (preço atual R$ {fmt(sig.price)})",
        f"Invalidação (stop no ativo): R$ {fmt(sig.stop)}",
        f"Alvo técnico: R$ {fmt(sig.target)} (risco/retorno 1:{config.RR_TARGET:g})",
        f"Espaço técnico: {sig.space} | ATR D1: R$ {fmt(sig.atr)} | Ritmo de volume: {fmt(sig.vr,1)}x",
        f"IV atual (ATM): {fmt(iv['iv']*100,1)}% | IV Rank: {fmt(iv['iv_rank'],1)}% | IV Percentile: {fmt(iv['iv_pct'],1)}%"]
    if opt:
        lines += [f"Opção candidata: <code>{opt['symbol']}</code> — toque para copiar e colar na busca do Profit ({opt['side'].upper()} strike {fmt(opt['strike'])}, venc. {opt['expiration']}, {opt['dte']} DTE)",
            f"Delta {fmt(opt['delta'])} | IV {fmt((opt['iv'] or 0)*100,1)}% | Bid {fmt(opt['bid'])} / Ask {fmt(opt['ask'])} | Spread {fmt(opt['spread_pct'],1)}%",
            f"Liquidez: OI {opt['oi']} | volume {opt['volume']} (dados do pregão {opt['ref_date']}; confira o book ao vivo)",
            f"Risco: prêmio pago, máx. R$ {fmt(opt['max_loss_per_lot'])} por lote de {opt['lot']}",
            f"Manejo: sair se o ativo perder R$ {fmt(sig.stop)} ou se o prêmio cair {config.OPTION_STOP_LOSS_PCT}% (≈ R$ {fmt(opt['stop_premium'])}); realizar no alvo do ativo"]
    else:
        lines.append(f"Opção: nenhuma passou nos filtros ({html.escape(str(opt_err))}). Alerta apenas do ativo.")
    tpl = config.PROFIT_LINK_TEMPLATE
    if tpl and tpl.lower() != "off":
        target = opt["symbol"] if opt else sig.ticker
        lines.append(f"Profit {'opção' if opt else 'ativo'}: " + html.escape(tpl.format(ticker=target)))
    lines.append("Apoio à decisão; não é ordem nem recomendação.")
    return "\n".join(lines)
