"""Orquestracao (EOD as 08:30; no Railway a 1a subida faz o backfill sozinha). Uso: python main.py tick (cron) | serve (sempre ligado) | pre | intraday | eod | backfill [dias] | status | telegram-test"""
import sys, logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import config, database, messenger, screener, analyzer, options_selector, iv_filter
from data_fetcher import Brapi, BrapiError, BrapiPlanError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")
TZ = ZoneInfo(config.TZ)

def now(): return datetime.now(TZ)

def plan_alert(e):
    """Erro de plano/chave da brapi: avisa uma vez por dia no Telegram e interrompe."""
    log.error("brapi recusou o acesso (%s). Verifique o plano Pro e o BRAPI_TOKEN.", e)
    today = str(now().date())
    if not database.done("planwarn", today):
        database.mark_done("planwarn", today)
        messenger.send("⚠️ Radar de Opções: a brapi recusou o acesso às opções (plano Pro ou token). Nenhum alerta será gerado até corrigir.")

def job_eod():
    """Coleta IV ATM do ultimo pregao, calcula Rank/Percentile e monta a watchlist. Retorna ativos com sucesso."""
    api, ok = Brapi(), 0
    trade_date = None
    for t in config.UNIVERSE:
        try:
            r = iv_filter.collect_iv(api, t)
            if not r: log.warning("%s: sem IV", t); continue
            trade_date = r[0]
            iv, rank, pct, approved, note = iv_filter.evaluate(t)
            database.save_iv_status(trade_date, t, iv, rank, pct, approved, note)
            ok += 1
        except BrapiPlanError as e:
            plan_alert(e); break
        except BrapiError as e:
            log.error("%s: %s", t, e)
    log.info("EOD concluido: %d/%d ativos; data %s", ok, len(config.UNIVERSE), trade_date)
    return ok

def job_pre():
    """Pre-abertura: avisa a watchlist do dia (uma mensagem curta)."""
    d, wl = screener.watchlist()
    if not wl:
        log.info("Watchlist vazia"); return
    lines = [f"Radar de Opções — observação de hoje (IV de {d})"]
    for r in sorted(wl.values(), key=lambda r: r["iv_rank"]):
        lines.append(f"• {r['ticker']}: IV {r['iv']*100:.1f}% | Rank {r['iv_rank']:.0f} | Pct {r['iv_pct']:.0f}")
    lines.append("Alerta só sai se houver rompimento confirmado.")
    messenger.send("\n".join(lines))

def job_intraday():
    d, wl = screener.watchlist()
    if not wl: return
    api, t0 = Brapi(), now()
    sigs = []
    for t in wl:
        try:
            daily = api.history(t, "3mo", "1d")
            m15 = api.history(t, "5d", "15m")
            if not m15 or datetime.fromtimestamp(m15[-1]["date"], TZ).date() != t0.date():
                continue  # sem pregao hoje (feriado) ou sem dados
            s = analyzer.analyze(t, daily, m15, t0)
            if s and not database.already_alerted(str(t0.date()), t, s.direction):
                sigs.append(s)
        except BrapiError as e:
            log.error("%s: %s", t, e)
    for s in screener.rank_signals(sigs):
        iv = wl[s.ticker]
        database.log_signal(t0.isoformat(), s.ticker, s.direction, s.dict())
        try:
            opt, err = options_selector.select(api, s, t0.date())
        except BrapiError as e:
            opt, err = None, str(e)
        messenger.send(messenger.short_msg(s, iv))
        messenger.send(messenger.full_msg(s, iv, opt, err))
        database.save_alert(str(t0.date()), s.ticker, s.direction, t0.isoformat(), {"signal": s.dict(), "option": opt})
    log.info("Intraday: %d na watchlist, %d sinais", len(wl), len(sigs))

def job_backfill(days=365, budget=None):
    """Reconstroi IV historica (1 chamada de analytics por ativo/dia). Retomavel.
    budget = segundos maximos por execucao (usado no modo cron)."""
    import time
    api, t0 = Brapi(), time.time()
    start = now().date() - timedelta(days=days)
    for t in config.UNIVERSE:
        d = start
        while d < now().date():
            if budget and time.time() - t0 > budget:
                log.info("Backfill: tempo da execucao esgotado; continua na proxima"); return False
            if d.weekday() < 5 and not database.has_iv(t, str(d)) and not database.was_tried(t, str(d)):
                try:
                    if not iv_filter.collect_iv(api, t, trade_date=str(d)):
                        database.mark_tried(t, str(d))      # sem dados nesse dia (feriado etc.)
                except BrapiPlanError as e:
                    plan_alert(e); return False              # nao marca: tenta de novo apos corrigir o plano
                except BrapiError as e:
                    log.debug("%s %s: %s", t, d, e)
                    if e.status in (400, 404):
                        database.mark_tried(t, str(d))
            d += timedelta(days=1)
        log.info("Backfill %s concluido", t)
    return True

def job_bootstrap():
    """Na 1a subida (ou banco vazio): reconstroi o historico de IV e calcula a watchlist."""
    if database.min_history(config.UNIVERSE) >= config.IV_MIN_HISTORY:
        return
    log.info("Historico de IV insuficiente: iniciando backfill (pode levar um bom tempo)")
    if job_backfill(365) is not True:
        return
    job_eod()
    n = database.min_history(config.UNIVERSE)
    if n >= config.IV_MIN_HISTORY:
        messenger.send("Radar de Opções: histórico de IV carregado; robô ativo.")
    else:
        log.warning("Backfill terminou, mas o historico ainda e insuficiente (%d pregoes no ativo mais curto)", n)

def job_telegram_test():
    messenger.send("Radar de Opções: teste de conexão com o Telegram OK.")

def job_tick():
    """Modo CRON (Railway): rodar a cada 5 min, seg-sex, '*/5 11-20 * * 1-5' (UTC).
    Decide pelo relogio de Brasilia o que executar nesta passada."""
    if config.ON_RAILWAY and not config.DB_PERSISTENT:
        log.error("Sem Volume no Railway: o banco seria apagado a cada execucao. Anexe um Volume ao servico.")
        sys.exit(1)
    t = now()
    if t.weekday() > 4:
        return
    today = str(t.date())
    slot = t.hour * 60 + (t.minute // 5) * 5            # minutos desde 00:00, em passos de 5
    if database.min_history(config.UNIVERSE) < config.IV_MIN_HISTORY:
        if job_backfill(365, budget=240):
            if job_eod() >= len(config.UNIVERSE) // 2:
                messenger.send("Radar de Opções: histórico de IV carregado; robô ativo.")
        return
    if 8 * 60 + 30 <= slot < 10 * 60 and not database.done("eod", today):
        if job_eod() > 0:
            database.mark_done("eod", today)
    if config.WATCHLIST_MSG and 9 * 60 + 30 <= slot < 10 * 60 and not database.done("pre", today):
        job_pre()
        database.mark_done("pre", today)
    if (9 * 60 + 45 <= slot <= 10 * 60 + 10) or (10 * 60 + 15 <= slot <= 17 * 60 + 45 and slot % 15 == 0):
        job_intraday()

def job_status():
    d, wl = screener.watchlist()
    print("Watchlist:", d, list(wl))
    if d:
        for r in database.latest_status(d):
            print(r)

def serve():
    from apscheduler.schedulers.blocking import BlockingScheduler
    s = BlockingScheduler(timezone=config.TZ)
    wk = "mon-fri"
    # EOD roda de manha (08:30): os dados do pregao anterior da brapi ja estao disponiveis desde ~21h
    s.add_job(job_eod, "cron", day_of_week=wk, hour=8, minute=30, id="eod", misfire_grace_time=3600, coalesce=True)
    if config.WATCHLIST_MSG:
        s.add_job(job_pre, "cron", day_of_week=wk, hour=9, minute=30, id="pre", misfire_grace_time=1800, coalesce=True)
    s.add_job(job_intraday, "cron", day_of_week=wk, hour=9, minute="45-55/5", id="open5a")
    s.add_job(job_intraday, "cron", day_of_week=wk, hour=10, minute="0,5,10", id="open5b")
    s.add_job(job_intraday, "cron", day_of_week=wk, hour="10-17", minute="15,30,45", id="m15a")
    s.add_job(job_intraday, "cron", day_of_week=wk, hour="11-17", minute=0, id="m15b")
    s.add_job(job_bootstrap, "cron", day_of_week=wk, hour=8, minute=0, id="bootstrap_daily", misfire_grace_time=3600, coalesce=True)
    s.add_job(job_bootstrap, "date", run_date=now() + timedelta(seconds=10), id="bootstrap")
    log.info("Agendador iniciado (%s)", config.TZ)
    s.start()

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    jobs = {"serve": serve, "pre": job_pre, "intraday": job_intraday, "eod": job_eod, "status": job_status, "tick": job_tick, "telegram-test": job_telegram_test, "bootstrap": job_bootstrap,
            "backfill": lambda: job_backfill(int(sys.argv[2]) if len(sys.argv) > 2 else 365)}
    if cmd not in jobs:
        sys.exit(f"Comando invalido: {cmd}. Use: {', '.join(jobs)}")
    jobs[cmd]()
