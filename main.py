import json
"""Orquestracao (EOD as 08:30; no Railway a 1a subida faz o backfill sozinha). Uso: python main.py tick (cron) | serve (sempre ligado) | pre | intraday | eod | backfill [dias] | status | telegram-test"""
import os, sys, logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import config, database, messenger, screener, analyzer, options_selector, iv_filter
from data_fetcher import Brapi, BrapiError, BrapiPlanError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")
TZ = ZoneInfo(config.TZ)

IV_METHOD = "monthly-v2"   # vencimentos mensais + ATM com >= 2 series; mude para forcar a refazer o historico

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
    if trade_date:
        st = database.latest_status(trade_date)
        apr = [f"{r['ticker']} (rank {r['iv_rank']:.0f}, pct {r['iv_pct']:.0f})" for r in st if r["approved"]]
        sem = [r["ticker"] for r in st if r["iv_rank"] is None]
        log.info("Watchlist para amanha: %s", ", ".join(apr) or "nenhum ativo com IV favoravel")
        if sem: log.info("Sem historico suficiente: %s", ", ".join(sem))
        log.info("Historico de IV (pregoes): %s", ", ".join(f"{t}={database.count_iv(t)}" for t in config.UNIVERSE))
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
    sigs, sem_dados, analisados, erros, exemplo = [], [], 0, 0, ""
    for t in wl:
        try:
            daily = api.history(t, "3mo", "1d")
            m15 = api.history(t, "5d", "15m")
            ult = datetime.fromtimestamp(m15[-1]["date"], TZ) if m15 else None
            if not ult or ult.date() != t0.date():
                sem_dados.append(t)
                if not exemplo:
                    exemplo = f"{t}: ultimo candle 15m = {ult.strftime('%d/%m %H:%M') if ult else 'nenhum'}"
                continue  # sem pregao hoje (feriado) ou sem dados
            analisados += 1
            s = analyzer.analyze(t, daily, m15, t0)
            if s and not database.already_alerted(str(t0.date()), t, s.direction):
                sigs.append(s)
        except BrapiError as e:
            erros += 1
            log.error("%s: %s", t, e)
    for s in screener.rank_signals(sigs):
        iv = wl[s.ticker]
        database.log_signal(t0.isoformat(), s.ticker, s.direction, s.dict())
        try:
            opt, err = options_selector.select(api, s, t0.date())
        except BrapiError as e:
            opt, err = None, str(e)
        messenger.send(messenger.alert_msg(s, iv, opt, err, t0.date()))
        database.save_alert(str(t0.date()), s.ticker, s.direction, t0.isoformat(), {"signal": s.dict(), "option": opt})
        try:   # registra quais regras de IV (A/B/C) teriam aprovado este sinal, para comparar resultados depois
            database.save_shadow(str(t0.date()), s.ticker, s.direction, t0.isoformat(), iv["iv"], iv["iv_rank"], iv["iv_pct"],
                                 iv_filter.iv_favoravel(iv["iv_rank"], iv["iv_pct"]), True, iv["iv_rank"] <= config.SHADOW_C_RANK_MAX, s.dict())
        except Exception as e:
            log.warning("Registro das regras falhou: %s", e)
    log.info("Intraday: %d na watchlist, %d analisados, %d sem dado de hoje, %d erros, %d sinais%s",
             len(wl), analisados, len(sem_dados), erros, len(sigs), f" | {exemplo}" if exemplo else "")
    if config.SHADOW and config.IV_GATE:   # com a regra A ativa, procura tambem sinais fora da watchlist
        try:
            job_shadow(api, t0, wl)
        except Exception as e:   # o registro paralelo nunca pode atrapalhar o alerta real
            log.warning("Shadow falhou: %s", e)

def job_shadow(api, t0, wl):
    """Roda a mesma deteccao de rompimento nos ativos fora da watchlist e grava (sem alertar) o que as regras B e C teriam enviado."""
    d = database.latest_status_date()
    if not d:
        return
    n = 0
    for r in database.latest_status(d):
        t = r["ticker"]
        if r["iv_rank"] is None:
            continue
        try:
            daily = api.history(t, "3mo", "1d")
            m15 = api.history(t, "5d", "15m")
            if not m15 or datetime.fromtimestamp(m15[-1]["date"], TZ).date() != t0.date():
                continue
            s = analyzer.analyze(t, daily, m15, t0)
        except BrapiError as e:
            log.debug("shadow %s: %s", t, e); continue
        if not s:
            continue
        a = iv_filter.iv_favoravel(r["iv_rank"], r["iv_pct"])
        c = r["iv_rank"] <= config.SHADOW_C_RANK_MAX
        database.save_shadow(str(t0.date()), t, s.direction, t0.isoformat(), r["iv"], r["iv_rank"], r["iv_pct"], a, True, c, s.dict())
        n += 1
    log.info("Shadow: %d sinais registrados (sem envio)", n)

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
        log.info("Backfill %s concluido: %d pregoes de IV salvos", t, database.count_iv(t))
    return True

def job_bootstrap():
    """Na 1a subida (ou banco vazio): reconstroi o historico de IV e calcula a watchlist."""
    if database.ensure_iv_method(IV_METHOD):
        log.warning("Metodo de IV atualizado (%s): historico antigo descartado; sera refeito", IV_METHOD)
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
    if database.ensure_iv_method(IV_METHOD):
        log.warning("Metodo de IV atualizado (%s): historico antigo descartado; sera refeito", IV_METHOD)
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

def job_debug_iv():
    """Diagnostico: manda no Telegram a IV/Rank/Percentile do robo (e os dados brutos) de 1 ou mais ativos.
    env DEBUG_IV=SUZB3 ou DEBUG_IV=SUZB3,PETR4,VALE3 (tambem: python main.py debug-iv PETR4)."""
    raw = os.getenv("DEBUG_IV", "").strip() or (sys.argv[2] if len(sys.argv) > 2 else "")
    tickers = [x.strip().upper() for x in raw.split(",") if x.strip()]
    if not tickers:
        return
    api = Brapi()
    for t in tickers:
        try:
            exps = api.expirations(t)
            pick = iv_filter.pick_expiration(exps, now().date())
            lines = [f"Diagnostico IV {t}", f"Vencimento usado: {pick[0] + f' ({pick[1]} DTE)' if pick else 'nenhum'}"]
            if pick:
                d, rows = api.analytics(t, pick[0])
                spot = next((r.get("underlyingPrice") for r in rows if r.get("underlyingPrice")), None)
                lines.append(f"Dado de {d} | ativo R$ {spot} | {len(rows)} series")
                if spot:
                    near = sorted([r for r in rows if r.get("strike")], key=lambda r: abs(r["strike"] - spot))[:4]
                    for r in near:
                        lines.append(f"{r['symbol']} {r['side']} K={r['strike']} IV={r.get('impliedVolatility')} OI={r.get('openInterest')} conf={r.get('confidence')}")
                v = iv_filter.atm_iv(rows)
                lines.append(f"IV ATM do dia: {v * 100:.2f}%" if v else "IV ATM do dia: indisponivel")
            iv, rank, pct, ok, note = iv_filter.evaluate(t)
            if rank is None:
                lines.append(f"Rank/Percentile do robo: {note or 'sem dados'}")
            else:
                lines.append(f"ROBO -> IV {iv * 100:.2f}% | Rank {rank:.1f} | Percentile {pct:.1f} | {'FAVORAVEL' if ok else 'nao favoravel'}")
            lines.append(f"Pregoes de IV salvos: {database.count_iv(t)}")
            messenger.send("\n".join(lines))
        except BrapiError as e:
            messenger.send(f"Diagnostico IV {t} falhou: {e}")

def job_backtest_iv():
    """Conta em quantos pregoes cada regra Rank/Percentil teria aprovado cada ativo (historico salvo). Manda resumo no Telegram."""
    tot_days, tot = 0, {r: 0 for r in iv_filter.RULES}
    rows = []
    for t in config.UNIVERSE:
        s = database.iv_series(t, 10000)
        days, hits = iv_filter.backtest_series(s)
        if not days:
            continue
        tot_days += days
        for r in tot:
            tot[r] += hits[r]
        rows.append((t, days, hits))
    if not rows:
        messenger.send("Backtest IV: historico insuficiente ainda.")
        return
    fmt = lambda r: f"{r[0]}/{r[1]}"
    lines = [f"Backtest IV ({len(rows)} ativos, {tot_days} ativo-dias)", "Regra Rank/Pct: % dos dias aprovados"]
    for r in iv_filter.RULES:
        lines.append(f"{fmt(r)}{' (atual)' if r == (25, 30) else ''}: {tot[r] / tot_days * 100:.1f}% ({tot[r]} dias)")
    lines.append("Por ativo (atual 25/30 | 40/40):")
    for t, days, h in sorted(rows, key=lambda x: -x[2][(25, 30)]):
        lines.append(f"{t}: {h[(25, 30)]}/{days} | {h[(40, 40)]}/{days}")
    messenger.send("\n".join(lines))

def job_shadow_report():
    """Resultado (alvo x stop no ativo, em R) dos sinais registrados, por regra A/B/C. Manda resumo no Telegram."""
    import shadow
    rows = database.shadow_rows()
    if not rows:
        messenger.send("Shadow IV: ainda sem sinais registrados.")
        return
    api, res = Brapi(), []
    for r in rows:
        try:
            daily = api.history(r["ticker"], "6mo", "1d")
        except BrapiError:
            continue
        res.append((r, shadow.outcome(json.loads(r["payload"]), r["date"], daily, TZ)))
    messenger.send(shadow.summary(res))

def job_check_iv():
    """Manda no Telegram a IV/Rank/Percentil do ativo nas datas pedidas e o veredito das regras A, B e C. Roda uma vez por valor de CHECK_IV."""
    spec = config.CHECK_IV.strip()
    if not spec or ":" not in spec or database.get_meta("check:" + spec):
        return
    t, ds = spec.split(":", 1)
    t = t.strip().upper()
    lines = [f"Conferencia IV {t} (regra A = Rank até 25 e Pct até 30; C = Rank até 60; B = sem filtro)"]
    for d in [x.strip() for x in ds.split(",") if x.strip()]:
        r = iv_filter.evaluate_on(t, d)
        if r is None:
            lines.append(f"{d}: sem IV salva")
        elif "rank" not in r:
            lines.append(f"{d}: IV {r['iv']*100:.1f}% | historico insuficiente ({r['n']} pregoes)")
        else:
            lines.append(f"{d}: IV {r['iv']*100:.1f}% | Rank {r['rank']:.0f} | Pct {r['pct']:.0f} | A {'sim' if r['A'] else 'nao'} | C {'sim' if r['C'] else 'nao'} | B sim")
    if messenger.send("\n".join(lines)):
        database.set_meta("check:" + spec, "1")

def _once(name, fn):
    """Roda fn uma unica vez por valor da variavel de ambiente 'name' (evita repetir diagnosticos a cada reinicio)."""
    def run():
        key = f"once:{name}:{os.getenv(name, '')}"
        if database.get_meta(key):
            log.info("%s ja executado para este valor; ignorando", name)
            return
        database.set_meta(key, "1")
        fn()
    return run

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
    if config.WATCHLIST_MSG and config.IV_GATE:   # com a regra B a watchlist tem todos os ativos: mensagem das 09:30 seria so ruido
        s.add_job(job_pre, "cron", day_of_week=wk, hour=9, minute=30, id="pre", misfire_grace_time=1800, coalesce=True)
    s.add_job(job_intraday, "cron", day_of_week=wk, hour=9, minute="45-55/5", id="open5a")
    s.add_job(job_intraday, "cron", day_of_week=wk, hour=10, minute="0,5,10", id="open5b")
    s.add_job(job_intraday, "cron", day_of_week=wk, hour="10-17", minute="15,30,45", id="m15a")
    s.add_job(job_intraday, "cron", day_of_week=wk, hour="11-17", minute=0, id="m15b")
    s.add_job(job_bootstrap, "cron", day_of_week=wk, hour=8, minute=0, id="bootstrap_daily", misfire_grace_time=3600, coalesce=True)
    s.add_job(job_bootstrap, "date", run_date=now() + timedelta(seconds=10), id="bootstrap")
    # na subida, recalcula a watchlist com a regra vigente (assim a mudanca vale no mesmo dia, sem esperar o EOD das 08:30)
    s.add_job(job_eod, "date", run_date=now() + timedelta(seconds=90), id="eod_boot")
    if config.CHECK_IV:
        s.add_job(job_check_iv, "date", run_date=now() + timedelta(seconds=60), id="check_iv")
    if os.getenv("BACKTEST_IV"):
        s.add_job(_once("BACKTEST_IV", job_backtest_iv), "date", run_date=now() + timedelta(seconds=40), id="backtest_iv")
    if os.getenv("DEBUG_IV"):
        s.add_job(_once("DEBUG_IV", job_debug_iv), "date", run_date=now() + timedelta(seconds=20), id="debug_iv")
    log.info("Agendador iniciado (%s)", config.TZ)
    s.start()

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    jobs = {"serve": serve, "pre": job_pre, "intraday": job_intraday, "eod": job_eod, "status": job_status, "debug-iv": job_debug_iv, "backtest-iv": job_backtest_iv, "check-iv": job_check_iv, "shadow-report": job_shadow_report, "tick": job_tick, "telegram-test": job_telegram_test, "bootstrap": job_bootstrap,
            "backfill": lambda: job_backfill(int(sys.argv[2]) if len(sys.argv) > 2 else 365)}
    if cmd not in jobs:
        sys.exit(f"Comando invalido: {cmd}. Use: {', '.join(jobs)}")
    jobs[cmd]()
