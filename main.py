import json
"""Orquestracao (EOD as 08:30; no Railway a 1a subida faz o backfill sozinha). Uso: python main.py tick (cron) | serve (sempre ligado) | pre | intraday | eod | backfill [dias] | status | telegram-test"""
import os, sys, logging
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
import config, database, messenger, screener, analyzer, options_selector, iv_filter, positions, outcomes
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

def _ibov_change(api, t0):
    """Variacao do Ibovespa hoje vs fechamento anterior (%), ou None se nao der para calcular."""
    try:
        rows = api.history("^BVSP", "5d", "1d")
        if len(rows) < 2 or datetime.fromtimestamp(rows[-1]["date"], TZ).date() != t0.date():
            return None
        return (rows[-1]["close"] / rows[-2]["close"] - 1) * 100
    except Exception as e:
        log.warning("Variacao do Ibovespa indisponivel: %s", e)
        return None

def _bova_pct(api, t0):
    """Variacao do BOVA11 hoje (ultimo candle de 15 min vs fechamento anterior, em %). None se faltar dado ou se o numero parecer erro (|v| > 8%)."""
    try:
        ant = [d for d in api.history("BOVA11", "5d", "1d") if datetime.fromtimestamp(d["date"], TZ).date() < t0.date()]
        hoje = [c for c in api.history("BOVA11", "5d", "15m") if datetime.fromtimestamp(c["date"], TZ).date() == t0.date()]
        if not ant or not hoje:
            return None
        v = (hoje[-1]["close"] / ant[-1]["close"] - 1) * 100
        return v if abs(v) <= 8 else None
    except Exception as e:
        log.warning("BOVA11 indisponivel: %s", e)
        return None

def _mercado_no_sentido(bov, direction):
    """Variacao do BOVA11 no sentido do sinal (positivo = mercado a favor). None se nao houver dado."""
    if bov is None:
        return None
    return -bov if direction == "short" else bov

def job_intraday():
    d, wl = screener.watchlist()
    if not wl: return
    api, t0 = Brapi(), now()
    sigs, sem_dados, analisados, erros, exemplo, lags = [], [], 0, 0, "", []
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
            fecha = ult + timedelta(minutes=15)                  # candle de 15 min: o preco e o do fechamento dele
            lags.append(max(0, (t0 - fecha).total_seconds() / 60))
            s = analyzer.analyze(t, daily, m15, t0)
            if s and not database.already_alerted(str(t0.date()), t, s.direction):
                s.asof = fecha.strftime("%H:%M")                  # horario do ultimo preco usado
                sigs.append(s)
        except BrapiError as e:
            erros += 1
            log.error("%s: %s", t, e)
    ibov = _ibov_change(api, t0) if sigs else None
    bov = _bova_pct(api, t0) if sigs else None
    for s in screener.rank_signals(sigs):
        iv = wl[s.ticker]
        database.log_signal(t0.isoformat(), s.ticker, s.direction, s.dict())
        try:
            opt, err = options_selector.select(api, s, t0.date())
        except BrapiError as e:
            opt, err = None, str(e)
        ruim, bloq = False, False
        mv = _mercado_no_sentido(bov, s.direction)
        if opt:   # sem opcao liquida, com veredito RUIM ou com o mercado no mesmo sentido nao vira alerta: fica registrado e aparece no resumo das 18:30
            msg = messenger.alert_msg(s, iv, opt, err, t0.date(), ibov, positions.contar_abertas(), mv)
            ruim = "❌ RUIM" in msg
            if ruim:
                log.info("%s %s: veredito RUIM; fica para o resumo", s.ticker, s.direction)
            elif config.FILTRO_MERCADO and mv is not None and mv > 0:
                bloq = True
                log.info("%s %s: BOVA11 %.1f%% no mesmo sentido; fica para o resumo", s.ticker, s.direction, mv)
            else:
                messenger.send(msg)
        else:
            log.info("%s %s: sinal sem opcao (%s); fica para o resumo", s.ticker, s.direction, err)
        database.save_alert(str(t0.date()), s.ticker, s.direction, t0.isoformat(), {"signal": s.dict(), "option": opt, "reason": err, "ruim": ruim, "bloq_mercado": bloq, "mercado": mv, "iv": {"iv_rank": iv["iv_rank"], "iv_pct": iv["iv_pct"]}})
        try:   # registra quais regras de IV (A/B/C) teriam aprovado este sinal, para comparar resultados depois
            database.save_shadow(str(t0.date()), s.ticker, s.direction, t0.isoformat(), iv["iv"], iv["iv_rank"], iv["iv_pct"],
                                 iv_filter.iv_favoravel(iv["iv_rank"], iv["iv_pct"]), True, iv["iv_rank"] <= config.SHADOW_C_RANK_MAX, s.dict())
        except Exception as e:
            log.warning("Registro das regras falhou: %s", e)
    if lags:
        try:
            import statistics
            database.set_meta("lag_last", str(int(statistics.median(lags))))
        except Exception:
            pass
    try:
        database.save_stats(t0.isoformat(), str(t0.date()), len(wl), analisados, len(sem_dados), erros, len(sigs))
    except Exception as e:
        log.warning("Estatistica do ciclo nao gravada: %s", e)
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

def job_dist_iv():
    """Checagem unica: quanto o corte de 'IV cara' em Rank 50 (vs 75) muda as etiquetas. Hoje e no historico dos 30 ativos."""
    key = "dist_iv:v1"
    if database.get_meta(key):
        return
    hoje = {"barata": 0, "m50": 0, "c50": 0, "m75": 0, "c75": 0}
    d = database.latest_status_date()
    rows = database.latest_status(d) if d else []
    n_hoje = 0
    for r in rows:
        if r.get("iv_rank") is None:
            continue
        n_hoje += 1
        if iv_filter.iv_favoravel(r["iv_rank"], r["iv_pct"]):
            hoje["barata"] += 1
        elif r["iv_rank"] > 50: hoje["c50"] += 1
        else: hoje["m50"] += 1
        if not iv_filter.iv_favoravel(r["iv_rank"], r["iv_pct"]):
            hoje["c75" if r["iv_rank"] > 75 else "m75"] += 1
    tot = a50 = a75 = 0
    for t in config.UNIVERSE:
        ser = database.iv_series(t, 400)
        for i in range(config.IV_MIN_HISTORY, len(ser)):
            w = ser[max(0, i - config.IV_WINDOW + 1): i + 1]
            rk = iv_filter.iv_rank(w, ser[i])
            tot += 1; a50 += rk > 50; a75 += rk > 75
    L = ["Checagem IV: corte de 'cara' em Rank 50 (novo) vs 75 (antigo)"]
    if n_hoje:
        L.append(f"Hoje ({d[8:10]}/{d[5:7]}, {n_hoje} ativos): ✅ barata {hoje['barata']} | ⚠️ média {hoje['m50']} (antes {hoje['m75']}) | 🔥 cara {hoje['c50']} (antes {hoje['c75']})")
    if tot:
        L.append(f"Histórico ({tot} ativo-dias): Rank acima de 50 em {a50/tot*100:.0f}% | acima de 75 em {a75/tot*100:.0f}%")
    if messenger.send("\n".join(L)):
        database.set_meta(key, "1")

def job_diag_opcoes():
    """Checagem unica (so leitura): para cada sinal sem opcao de hoje, por que cada vencimento falha, com dados brutos da serie mais proxima de delta 0,5."""
    key = "diag_opc:v3"
    if database.get_meta(key):
        return
    hoje = str(now().date())
    sinais = []
    for k in range(0, 6):   # usa o dia mais recente que tenha sinais sem opcao
        dia = str(now().date() - timedelta(days=k))
        sinais = database.no_option_signals(dia)
        if sinais:
            hoje = dia
            break
    log.info("Diagnostico de opcoes: %d sinais sem opcao em %s", len(sinais), hoje)
    if not sinais:
        messenger.send(f"Diagnostico de opcoes: nenhum sinal sem opcao registrado em {hoje[8:10]}/{hoje[5:7]}.")
        return
    api = Brapi()
    messenger.send(f"Diagnostico de opcoes: iniciado ({len(sinais)} sinais de {hoje[8:10]}/{hoje[5:7]}). Resultados chegam em blocos.")
    L = ["faixa delta | sem cotacao | spread | liquidez | ok"]
    enviado = True
    for t, d, _ in sinais:
        try:
            res = options_selector.diagnose(api, t, d, now().date())
        except Exception as e:   # nunca falhar em silencio: o erro aparece na mensagem
            log.exception("Diagnostico de opcoes %s falhou", t)
            L.append(f"{t}: erro {type(e).__name__} {str(e)[:80]}")
            res = None
        if res is not None and not res:
            L.append(f"{t}: sem vencimento mensal na janela")
        for r in (res or []):
            L.append(f"{t} {'▲' if d == 'long' else '▼'} {r['exp'][8:10]}/{r['exp'][5:7]}: {r['delta']}/{r['n']} | {r['sem_cot']} | {r['spread']} | {r['liq']} | {r['ok']}")
            if r["ex"]:
                L.append("   " + r["ex"])
        if len(L) >= 12:
            enviado = messenger.send("\n".join(L)) and enviado
            L = []
        log.info("Diagnostico de opcoes: %s concluido", t)
    if L:
        enviado = messenger.send("\n".join(L)) and enviado
    if enviado:
        database.set_meta(key, "1")

def job_diag_semanais():
    """Checagem unica (so leitura): quais dos ativos tem opcoes semanais nos proximos 14 dias e quantas series passam nos filtros (delta, spread, liquidez)."""
    key = "diag_sem:v1"
    if database.get_meta(key):
        return
    hoje = now().date()
    api = Brapi()
    messenger.send("Checagem de semanais: iniciada (30 ativos). Resultado chega em blocos.")
    L, com, sem = [], [], []
    enviado = True
    for t in config.UNIVERSE:
        try:
            exps = [e for e in api.expirations(t) if 1 <= (datetime.strptime(e, "%Y-%m-%d").date() - hoje).days <= 14 and not iv_filter.is_monthly(e)]
            if not exps:
                sem.append(t); continue
            ok_c = ok_p = n = 0
            for e in sorted(exps)[:2]:
                for side in ("call", "put"):
                    _, an = api.analytics(t, e, side=side)
                    _, ch = api.chain(t, e, side=side)
                    book = {x["symbol"]: x for x in ch}
                    for a in an:
                        x = book.get(a["symbol"])
                        if not x or a.get("delta") is None or not (config.DELTA_MIN <= abs(a["delta"]) <= config.DELTA_MAX):
                            continue
                        bid, ask = x.get("bid") or 0, x.get("ask") or 0
                        if bid <= 0 or ask <= 0 or ask < bid:
                            px = x.get("close") or 0
                            if px <= 0:
                                continue
                            bid = ask = px
                        if (ask - bid) / ((ask + bid) / 2) * 100 > config.MAX_SPREAD_PCT:
                            continue
                        if (x.get("openInterest") or 0) < config.MIN_OPEN_INTEREST or (x.get("volume") or 0) < config.MIN_VOLUME:
                            continue
                        if side == "call": ok_c += 1
                        else: ok_p += 1
            com.append(f"{t}: {len(exps)} venc ({', '.join(e[8:10] + '/' + e[5:7] for e in sorted(exps)[:3])}) · series ok: {ok_c} calls, {ok_p} puts")
        except Exception as e:
            L.append(f"{t}: erro {type(e).__name__} {str(e)[:60]}")
    msg = ["Semanais (proximos 14 dias):"] + (com or ["nenhum ativo com semanal"]) + ["", "Sem semanal: " + (", ".join(sem) or "-")] + L
    enviado = messenger.send("\n".join(msg))
    if enviado:
        database.set_meta(key, "1")

def job_diag_variacao():
    """Teste (so leitura): para cada sinal de hoje, a trajetoria da acao ate a hora do alerta (abertura, nivel, 1o candle alem do nivel, preco no alerta)."""
    hoje = now().date()
    with database.conn() as c_:
        rows = c_.execute("SELECT ticker, direction, ts, payload FROM alerts WHERE date=? ORDER BY ts", (str(hoje),)).fetchall()
    if not rows:
        messenger.send("Variação até o alerta: nenhum sinal registrado hoje.")
        return
    api, L = Brapi(), []
    for r in rows:
        t, d = r["ticker"], r["direction"]
        p = json.loads(r["payload"])
        sig = p.get("signal") or {}
        try:
            daily = api.history(t, "3mo", "1d")
            m15 = [c for c in api.history(t, "5d", "15m") if datetime.fromtimestamp(c["date"], TZ).date() == hoje]
            ontem = [c for c in daily if datetime.fromtimestamp(c["date"], TZ).date() < hoje]
            if not m15 or not ontem:
                L.append(f"{t}: sem candles de hoje")
                continue
            prev = ontem[-1]["close"]
            lvl = sig.get("level")
            tsa = datetime.fromisoformat(r["ts"])
            fecha = lambda c: datetime.fromtimestamp(c["date"], TZ) + timedelta(minutes=15)
            ate = [c for c in m15 if fecha(c) <= tsa + timedelta(minutes=1)] or m15[:1]
            px_alerta = ate[-1]["close"]
            cruz = next((c for c in m15 if lvl and ((d == "short" and c["close"] < lvl) or (d == "long" and c["close"] > lvl))), None)
            pct = lambda a, b: (a / b - 1) * 100
            seta = "▲" if d == "long" else "▼"
            ln = [f"{t} {seta} · alerta {tsa.strftime('%H:%M')} · {'com opção' if p.get('option') else 'sem opção'}{' · RUIM' if p.get('ruim') else ''}",
                  f"Ontem fechou {fmt_(prev)} · abriu {fmt_(m15[0]['open'])} ({sgn_(pct(m15[0]['open'], prev))}) · no alerta {fmt_(px_alerta)} ({sgn_(pct(px_alerta, prev))} no dia)"]
            if lvl:
                if cruz:
                    hc = fecha(cruz)
                    ln.append(f"Nível {fmt_(lvl)} · 1º candle além do nível: {hc.strftime('%H:%M')} a {fmt_(cruz['close'])}")
                    mais = pct(px_alerta, cruz["close"]) * (-1 if d == "short" else 1)
                    ln.append(f"Entre aquele candle e o alerta: {(tsa - hc).total_seconds() / 60:.0f} min · a ação andou mais {sgn_(mais)} a favor")
                else:
                    ln.append(f"Nível {fmt_(lvl)} · nenhum candle fechou além do nível")
            if cruz:   # repete o teste de cada regra a cada candle, do 1o fechamento alem do nivel ate o alerta
                ln.append("Candle a candle (hora de fechamento · preço · o que bloqueava):")
                i0 = m15.index(cruz)
                for k in range(i0, len(m15)):
                    c = m15[k]
                    hk = fecha(c)
                    if hk > tsa + timedelta(minutes=1):
                        break
                    ln.append(f"{hk.strftime('%H:%M')} · {fmt_(c['close'])} · {analyzer.explain(d, daily, m15[:k + 1], hk)}")
            L.append("\n".join(ln[:30]))
        except Exception as e:
            L.append(f"{t}: erro {type(e).__name__} {str(e)[:60]}")
    msg = "Variação até a hora do alerta (hoje)\n\n" + "\n\n".join(L)
    messenger.send(msg)

def job_diag_atrasos():
    """Teste (so leitura): em todos os alertas ja enviados, quanto a acao andou entre o 1o fechamento alem do nivel e o alerta, e quando o sinal sairia com volume minimo menor."""
    with database.conn() as c_:
        rows = c_.execute("SELECT date, ticker, direction, ts, payload FROM alerts ORDER BY date, ts").fetchall()
    api, cache, L, tot = Brapi(), {}, [], []
    orig = config.VR_MIN
    try:
        for r in rows:
            t, d = r["ticker"], r["direction"]
            p = json.loads(r["payload"])
            sig = p.get("signal") or {}
            lvl = sig.get("level")
            dia = date.fromisoformat(r["date"])
            tag = f"{r['date'][8:]}/{r['date'][5:7]} {t}"
            try:
                if t not in cache:
                    cache[t] = (api.history(t, "3mo", "1d"), api.history(t, "1mo", "15m"))
                daily, all15 = cache[t]
                m15 = [c for c in all15 if datetime.fromtimestamp(c["date"], TZ).date() == dia]
                if not m15 or not lvl:
                    L.append(f"{tag}: sem candles M15")
                    continue
                tsa = datetime.fromisoformat(r["ts"])
                fecha = lambda c: datetime.fromtimestamp(c["date"], TZ) + timedelta(minutes=15)
                cruz = next((c for c in m15 if (c["close"] < lvl if d == "short" else c["close"] > lvl)), None)
                if not cruz:
                    L.append(f"{tag}: sem candle além do nível")
                    continue
                ate = [c for c in m15 if fecha(c) <= tsa + timedelta(minutes=1)] or m15[:1]
                px_a = ate[-1]["close"]
                sg = -1 if d == "short" else 1
                a = sg * (px_a / cruz["close"] - 1) * 100
                i0 = m15.index(cruz)
                def quando(vr):
                    config.VR_MIN = vr
                    for k in range(i0, len(m15)):
                        hk = fecha(m15[k])
                        if analyzer.explain(d, daily, m15[:k + 1], hk).startswith("SINAL"):
                            return hk
                    return None
                partes = []
                for vr in (1.2, 1.0):
                    hk = quando(vr)
                    partes.append(f"{messenger.fmt(vr, 1)}x→{hk.strftime('%H:%M') if hk else '-'}")
                atraso = (tsa - fecha(cruz)).total_seconds() / 60
                tot.append((a, atraso))
                seta = "▲" if d == "long" else "▼"
                L.append(f"{tag} {seta}: 1º fecho além {fecha(cruz).strftime('%H:%M')} a {fmt_(cruz['close'])} · alerta {tsa.strftime('%H:%M')} a {fmt_(px_a)} · atraso {atraso:.0f} min · já andou {sgn_(a)} · {' '.join(partes)}")
            except Exception as e:
                L.append(f"{tag}: erro {type(e).__name__} {str(e)[:50]}")
    finally:
        config.VR_MIN = orig
    cab = [f"Atraso dos alertas ({len(rows)} no total)", "Hora em que o sinal sairia com volume mínimo de 1,2x ou 1,0x (hoje 1,5x)", ""]
    if tot:
        grandes = sum(1 for a, _ in tot if a >= 1.0)
        cab.append(f"Já andou ≥1% a favor antes do alerta: {grandes} de {len(tot)}")
        cab.append(f"Mediana do que já andou: {sgn_(sorted(a for a, _ in tot)[len(tot) // 2])} · atraso mediano: {sorted(x for _, x in tot)[len(tot) // 2]:.0f} min")
        cab.append("")
    messenger.send("\n".join(cab + L))

def _replay_vr(daily, m15, t, vr, ate_dia):
    """Reexecuta analyze() a cada candle de cada dia (ate ate_dia, exclusive) com volume minimo vr. Retorna {(dia, direcao): sinal dict + hora}."""
    out, orig = {}, config.VR_MIN
    config.VR_MIN = vr
    try:
        dias = sorted({datetime.fromtimestamp(c["date"], TZ).date() for c in m15})
        for dia in dias:
            if dia >= ate_dia:
                continue
            idx = [i for i, c in enumerate(m15) if datetime.fromtimestamp(c["date"], TZ).date() == dia]
            for i in idx:
                hk = datetime.fromtimestamp(m15[i]["date"], TZ) + timedelta(minutes=15)
                s = analyzer.analyze(t, daily, m15[:i + 1], hk)
                if s and (dia, s.direction) not in out:
                    d = s.dict(); d["hora"] = hk.strftime("%H:%M")
                    out[(dia, s.direction)] = d
    finally:
        config.VR_MIN = orig
    return out

def job_diag_volume():
    """Teste (so leitura): refaz o ultimo mes nos ativos do universo com volume minimo 1,5x / 1,2x / 1,0x e compara resultado (stop/alvo/prazo na acao)."""
    api, hoje = Brapi(), now().date()
    VRS = (1.5, 1.2, 1.0)
    res = {v: {} for v in VRS}
    erros = 0
    for t in config.UNIVERSE:
        try:
            daily = api.history(t, "3mo", "1d")
            m15 = api.history(t, "1mo", "15m")
            for v in VRS:
                for (dia, dr), sg in _replay_vr(daily, m15, t, v, hoje).items():
                    bars = [b for b in daily if datetime.fromtimestamp(b["date"], TZ).date() > dia]
                    r = outcomes.avaliar(sg, bars)
                    res[v][(t, dia, dr)] = (sg, r)
        except Exception as e:
            erros += 1
            log.warning("diag_volume %s: %s", t, e)
    def resumo(itens):
        n = len(itens)
        fech = [r for _, r in itens if r]
        alvo = sum(1 for r in fech if r[0] == "alvo"); saida = sum(1 for r in fech if r[0] == "saida"); prazo = sum(1 for r in fech if r[0] == "prazo")
        med = sum(r[2] for r in fech) / len(fech) if fech else 0
        return f"{n} sinais · alvo {alvo} · saída {saida} · prazo {prazo} · abertos {n - len(fech)} · ação média {sgn_(med)} por sinal"
    L = [f"Teste de volume mínimo (último mês, {len(config.UNIVERSE) - erros} ativos; sem filtro de IV/opção/atraso)", ""]
    for v in VRS:
        L.append(f"Volume {messenger.fmt(v, 1)}x: {resumo(list(res[v].values()))}")
    for v in VRS[1:]:
        novos = [x for k, x in res[v].items() if k not in res[1.5]]
        iguais = [k for k in res[v] if k in res[1.5]]
        L += ["", f"Com {messenger.fmt(v, 1)}x:", f"  Alertas novos (dias que hoje não alertariam): {resumo(novos)}"]
        ant = [res[v][k] for k in iguais if res[v][k][0]["hora"] != res[1.5][k][0]["hora"]]
        ant15 = [res[1.5][k] for k in iguais if res[v][k][0]["hora"] != res[1.5][k][0]["hora"]]
        L.append(f"  Mesmos alertas, mas saindo antes ({len(ant)}): com {messenger.fmt(v, 1)}x {resumo(ant)}")
        L.append(f"  Esses mesmos com 1,5x: {resumo(ant15)}")
    messenger.send("\n".join(L))

def job_niveis():
    """Comando 'niveis' (so leitura): preco agora e distancia ate a maxima/minima de 20 pregoes de cada ativo; os 10 mais perto de romper (AXIA3 sempre incluida)."""
    api, hoje, rows = Brapi(), now().date(), []
    for t in config.UNIVERSE:
        try:
            daily = api.history(t, "3mo", "1d")
            hist = [d for d in daily if datetime.fromtimestamp(d["date"], TZ).date() < hoje][-config.BREAKOUT_LOOKBACK:]
            m15 = [c for c in api.history(t, "5d", "15m") if datetime.fromtimestamp(c["date"], TZ).date() == hoje]
            if not hist:
                continue
            px = m15[-1]["close"] if m15 else hist[-1]["close"]
            hi, lo = max(d["high"] for d in hist), min(d["low"] for d in hist)
            up, dn = (hi / px - 1) * 100, (px / lo - 1) * 100   # quanto falta para romper para cima / para baixo
            rows.append((min(up, dn), t, px, hi, lo, up, dn, m15[-1] if m15 else None))
        except BrapiError:
            continue
    rows.sort()
    pick = rows[:10] + [r for r in rows[10:] if r[1] == "AXIA3"]
    hora = next((datetime.fromtimestamp(r[7]["date"], TZ) + timedelta(minutes=15) for r in rows if r[7]), None)
    L = [f"Distância até o nível de 20 dias ({'preço das ' + hora.strftime('%H:%M') if hora else 'último fechamento'})", ""]
    for _, t, px, hi, lo, up, dn, _c in pick:
        lado = f"▲ falta {messenger.fmt(up, 1)}% p/ máx {fmt_(hi)}" if up <= dn else f"▼ falta {messenger.fmt(dn, 1)}% p/ mín {fmt_(lo)}"
        L.append(f"{t} · {fmt_(px)} · {lado}" + (f" (mín {fmt_(lo)} a {messenger.fmt(dn, 1)}%)" if up <= dn else f" (máx {fmt_(hi)} a {messenger.fmt(up, 1)}%)") if t == "AXIA3" else f"{t} · {fmt_(px)} · {lado}")
    messenger.send("\n".join(L))

def job_explicar(ticker="USIM5", direction="short"):
    """Comando 'usim5' (so leitura): candle a candle de hoje, o que bloqueia o sinal agora."""
    api, hoje = Brapi(), now().date()
    daily = api.history(ticker, "3mo", "1d")
    m15 = [c for c in api.history(ticker, "5d", "15m") if datetime.fromtimestamp(c["date"], TZ).date() == hoje]
    if not m15:
        messenger.send(f"{ticker}: sem candles de hoje.")
        return
    L = [f"{ticker} {'▼' if direction == 'short' else '▲'} hoje, candle a candle (hora de fechamento · preço · o que bloqueia):"]
    for k, c in enumerate(m15):
        hk = datetime.fromtimestamp(c["date"], TZ) + timedelta(minutes=15)
        L.append(f"{hk.strftime('%H:%M')} · {fmt_(c['close'])} · {analyzer.explain(direction, daily, m15[:k + 1], hk)}")
    messenger.send("\n".join(L))

def job_diag_resultados():
    """Comando 'resultados' (so leitura): alertas enviados ja resolvidos, por IV e por veredito, e o mesmo teste com stop na distancia inteira do sinal (em vez de metade)."""
    from types import SimpleNamespace
    with database.conn() as c_:
        rows = c_.execute("SELECT date, ticker, direction, payload FROM alerts ORDER BY date").fetchall()
    api, cache, itens = Brapi(), {}, []
    for r in rows:
        p = json.loads(r["payload"])
        sg = p.get("signal")
        if not p.get("option") or p.get("ruim") or not sg:
            continue
        try:
            if r["ticker"] not in cache:
                cache[r["ticker"]] = api.history(r["ticker"], "3mo", "1d")
            bars = [b for b in cache[r["ticker"]] if datetime.fromtimestamp(b["date"], TZ).date().isoformat() > r["date"]]
            atual = outcomes.avaliar(sg, bars)
            largo = outcomes.avaliar(dict(sg, stop=sg["price"] + 2 * (sg["stop"] - sg["price"])), bars)
            ivd = p.get("iv") or {}
            ivw = messenger._iv_word(ivd["iv_rank"], ivd["iv_pct"]) if ivd.get("iv_rank") is not None else "?"
            try:
                cobre = messenger._empate(SimpleNamespace(**sg), p["option"])[2]
            except Exception:
                cobre = None
            itens.append(dict(k=f"{r['date'][8:]}/{r['date'][5:7]} {r['ticker']}", iv=ivw, cobre=cobre, atual=atual, largo=largo))
        except Exception as e:
            log.warning("diag_resultados %s: %s", r["ticker"], e)
    def linha(nome, grupo, chave):
        fech = [i[chave] for i in grupo if i[chave]]
        if not fech:
            return f"{nome}: sem resolvidos"
        a = sum(1 for x in fech if x[0] == "alvo"); sd = sum(1 for x in fech if x[0] == "saida"); pz = sum(1 for x in fech if x[0] == "prazo")
        m = sum(x[2] for x in fech) / len(fech)
        d1 = sum(1 for x in fech if x[0] == "saida" and x[1] <= 2)
        return f"{nome}: {len(fech)} resolvidos · alvo {a} · saída {sd} (em até 2 dias: {d1}) · prazo {pz} · ação média {sgn_(m)}"
    L = [f"Resultados dos alertas enviados ({len(itens)} no total)", "", "Stop de hoje (metade do caminho):"]
    L.append(linha("Todos", itens, "atual"))
    for w in ("barata", "média", "cara"):
        L.append(linha(f"IV {w}", [i for i in itens if i["iv"] == w], "atual"))
    L.append(linha("Alvo cobre o empate", [i for i in itens if i["cobre"] is True], "atual"))
    L.append(linha("Alvo não cobre o empate", [i for i in itens if i["cobre"] is False], "atual"))
    L += ["", "Mesmos alertas com stop na distância inteira do sinal (o dobro de hoje):", linha("Todos", itens, "largo")]
    for w in ("barata", "média", "cara"):
        L.append(linha(f"IV {w}", [i for i in itens if i["iv"] == w], "largo"))
    messenger.send("\n".join(L))

def _replay_conf(daily, m15, t, conf, ate_dia):
    """Como _replay_vr, mas com 'conf' fechamentos M15 de confirmacao (volume minimo vigente)."""
    out, orig = {}, config.M15_CONFIRM_CLOSES
    config.M15_CONFIRM_CLOSES = conf
    try:
        for dia in sorted({datetime.fromtimestamp(c["date"], TZ).date() for c in m15}):
            if dia >= ate_dia:
                continue
            for i, c in enumerate(m15):
                if datetime.fromtimestamp(c["date"], TZ).date() != dia:
                    continue
                hk = datetime.fromtimestamp(c["date"], TZ) + timedelta(minutes=15)
                sg = analyzer.analyze(t, daily, m15[:i + 1], hk)
                if sg and (dia, sg.direction) not in out:
                    d = sg.dict(); d["hora"] = hk.strftime("%H:%M")
                    out[(dia, sg.direction)] = d
    finally:
        config.M15_CONFIRM_CLOSES = orig
    return out

def job_diag_regras():
    """Comando 'regras' (so leitura): ultimo mes nos ativos do universo com variacoes da regra de entrada; compara stop/alvo na acao."""
    api, hoje = Brapi(), now().date()
    nomes = ["Hoje (2 fechamentos)", "3 fechamentos M15", "4 fechamentos M15", "Tendência diária a favor", "Fechou ontem a <1% do nível"]
    res = {n: [] for n in nomes}
    erros = 0
    for t in config.UNIVERSE:
        try:
            daily = api.history(t, "3mo", "1d")
            m15 = api.history(t, "1mo", "15m")
            base = _replay_conf(daily, m15, t, 2, hoje)
            conj = {nomes[0]: base, nomes[1]: _replay_conf(daily, m15, t, 3, hoje), nomes[2]: _replay_conf(daily, m15, t, 4, hoje)}
            conj[nomes[3]] = {k: v for k, v in base.items() if (("alta" in v["trend_d1"]) if k[1] == "long" else ("baixa" in v["trend_d1"]))}
            def perto(k, v):
                ant = [d for d in daily if datetime.fromtimestamp(d["date"], TZ).date() < k[0]]
                if not ant:
                    return False
                c = ant[-1]["close"]
                return abs(c / v["level"] - 1) * 100 < 1.0
            conj[nomes[4]] = {k: v for k, v in base.items() if perto(k, v)}
            for n, sinais in conj.items():
                for (dia, dr), sg in sinais.items():
                    bars = [b for b in daily if datetime.fromtimestamp(b["date"], TZ).date() > dia]
                    res[n].append(outcomes.avaliar(sg, bars))
        except Exception as e:
            erros += 1
            log.warning("diag_regras %s: %s", t, e)
    L = [f"Teste de regras de entrada (último mês, {len(config.UNIVERSE) - erros} ativos; volume 1,2x; sem filtro de IV/opção)", ""]
    for n in nomes:
        itens = res[n]
        fech = [r for r in itens if r]
        a = sum(1 for r in fech if r[0] == "alvo"); sd = sum(1 for r in fech if r[0] == "saida"); pz = sum(1 for r in fech if r[0] == "prazo")
        m = sum(r[2] for r in fech) / len(fech) if fech else 0
        tx = f"{a / len(fech) * 100:.0f}%" if fech else "-"
        L.append(f"{n}: {len(itens)} sinais · alvo {a} ({tx}) · saída {sd} · prazo {pz} · abertos {len(itens) - len(fech)} · ação média {sgn_(m)}")
    L += ["", "Para empatar, o alvo precisa sair em ~20% dos sinais fechados."]
    messenger.send("\n".join(L))

def job_diag_filtros():
    """Comando 'filtros' (so leitura): no mesmo periodo dos alertas reais, compara os sinais que viraram alerta enviado com os descartados (sem opcao, RUIM, fora da watchlist ou ja alertado), e puts x calls."""
    with database.conn() as c_:
        al = c_.execute("SELECT date, ticker, direction, payload FROM alerts ORDER BY date").fetchall()
    if not al:
        messenger.send("Filtros: sem alertas registrados.")
        return
    chave = {}
    for r in al:
        p = json.loads(r["payload"])
        chave[(r["date"], r["ticker"], r["direction"])] = "enviado" if (p.get("option") and not p.get("ruim")) else "registrado, não enviado (sem opção ou RUIM)"
    inicio = date.fromisoformat(al[0]["date"])
    api, hoje = Brapi(), now().date()
    grupos, lados, erros = {}, {"put": [], "call": []}, 0
    for t in config.UNIVERSE:
        try:
            daily = api.history(t, "3mo", "1d")
            m15 = api.history(t, "1mo", "15m")
            for (dia, dr), sg in _replay_conf(daily, m15, t, config.M15_CONFIRM_CLOSES, hoje).items():
                if dia < inicio:
                    continue
                bars = [b for b in daily if datetime.fromtimestamp(b["date"], TZ).date() > dia]
                res = outcomes.avaliar(sg, bars)
                g = chave.get((str(dia), t, dr), "não virou alerta (fora da watchlist, sem opção líquida ou já alertado)")
                grupos.setdefault(g, []).append(res)
                lados["call" if dr == "long" else "put"].append(res)
        except Exception as e:
            erros += 1
            log.warning("diag_filtros %s: %s", t, e)
    def linha(nome, itens):
        fech = [r for r in itens if r]
        a = sum(1 for r in fech if r[0] == "alvo"); sd = sum(1 for r in fech if r[0] == "saida")
        m = sum(r[2] for r in fech) / len(fech) if fech else 0
        tx = f"{a / len(fech) * 100:.0f}%" if fech else "-"
        return f"{nome}: {len(itens)} sinais · alvo {a} ({tx}) · saída {sd} · abertos {len(itens) - len(fech)} · ação média {sgn_(m)}"
    L = [f"Sinais do teste desde {inicio.strftime('%d/%m')} (mesma regra de hoje, preço do sinal)", ""]
    for g in sorted(grupos):
        L.append(linha(g.capitalize(), grupos[g]))
    L += ["", linha("Puts", lados["put"]), linha("Calls", lados["call"]), "", "Para empatar, o alvo precisa sair em ~20% dos fechados na ação."]
    messenger.send("\n".join(L))

def job_diag_fatores():
    """Comando 'fatores' (so leitura): no ultimo mes, taxa de alvo por faixa de hora, volume, atraso do sinal, direcao do BOVA11, stop em ATR e lado (put/call)."""
    api, hoje = Brapi(), now().date()
    try:
        bd = api.history("BOVA11", "3mo", "1d"); bm = api.history("BOVA11", "1mo", "15m")
    except Exception:
        bd, bm = [], []
    def bova_a_favor(dia, hk, dr):
        ant = [d for d in bd if datetime.fromtimestamp(d["date"], TZ).date() < dia]
        ate = [c for c in bm if datetime.fromtimestamp(c["date"], TZ).date() == dia and datetime.fromtimestamp(c["date"], TZ) + timedelta(minutes=15) <= hk + timedelta(minutes=1)]
        if not ant or not ate:
            return None
        v = ate[-1]["close"] / ant[-1]["close"] - 1
        return (v < 0) if dr == "short" else (v > 0)
    linhas, erros = [], 0
    for t in config.UNIVERSE:
        try:
            daily = api.history(t, "3mo", "1d"); m15 = api.history(t, "1mo", "15m")
            for (dia, dr), sg in _replay_conf(daily, m15, t, config.M15_CONFIRM_CLOSES, hoje).items():
                bars = [b for b in daily if datetime.fromtimestamp(b["date"], TZ).date() > dia]
                res = outcomes.avaliar(sg, bars)
                if not res:
                    continue
                hh = int(sg["hora"][:2]) + int(sg["hora"][3:]) / 60
                atraso = abs(sg["price"] / sg["level"] - 1) * 100
                f = {}
                f["Hora do sinal"] = "10:00-11:00" if hh < 11 else "11:00-13:00" if hh < 13 else "13:00-15:00" if hh < 15 else "depois das 15:00"
                f["Volume (ritmo do dia)"] = "1,2x-1,5x" if sg["vr"] < 1.5 else "1,5x-2,5x" if sg["vr"] < 2.5 else "acima de 2,5x"
                f["Quanto já andou além do nível"] = "até 0,5%" if atraso < 0.5 else "0,5%-1%" if atraso < 1 else "1%-2%" if atraso < 2 else "acima de 2%"
                risco = abs(sg["price"] - sg["stop"]) / sg["atr"]
                f["Distância do stop do sinal"] = "até 0,8 ATR" if risco < 0.8 else "0,8-1,3 ATR" if risco < 1.3 else "acima de 1,3 ATR"
                f["Lado"] = "put" if dr == "short" else "call"
                bv = bova_a_favor(dia, datetime.strptime(f"{dia} {sg['hora']}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ), dr)
                f["BOVA11 no dia"] = "sem dado" if bv is None else ("a favor da direção" if bv else "contra a direção")
                linhas.append((f, res))
        except Exception as e:
            erros += 1
            log.warning("diag_fatores %s: %s", t, e)
    if not linhas:
        messenger.send("Fatores: sem sinais resolvidos.")
        return
    base = sum(1 for _, r in linhas if r[0] == "alvo") / len(linhas) * 100
    L = [f"O que separa rompimentos que dão certo (último mês, {len(linhas)} sinais resolvidos, alvo geral {base:.0f}%)", ""]
    for nome in ["Hora do sinal", "Volume (ritmo do dia)", "Quanto já andou além do nível", "Distância do stop do sinal", "BOVA11 no dia", "Lado"]:
        L.append(nome + ":")
        faixas = {}
        for f, r in linhas:
            faixas.setdefault(f[nome], []).append(r)
        for k in sorted(faixas):
            rs = faixas[k]
            a = sum(1 for r in rs if r[0] == "alvo")
            m = sum(r[2] for r in rs) / len(rs)
            L.append(f"  {k}: {len(rs)} · alvo {a / len(rs) * 100:.0f}% · ação média {sgn_(m)}")
    messenger.send("\n".join(L))

def job_diag_bova():
    """Comando 'bova' (so leitura): robustez do fator BOVA11 -- qualidade do dado, duas metades do mes, faixas de variacao do mercado e dias distintos."""
    api, hoje = Brapi(), now().date()
    bd = api.history("BOVA11", "3mo", "1d"); bm = api.history("BOVA11", "1mo", "15m")
    dia_de = lambda c: datetime.fromtimestamp(c["date"], TZ).date()
    suspeitos = set()
    for i in range(1, len(bm)):
        if dia_de(bm[i]) == dia_de(bm[i - 1]) and abs(bm[i]["close"] / bm[i - 1]["close"] - 1) > 0.02:
            suspeitos.add(dia_de(bm[i]))
    for d in bd:
        ult = [c for c in bm if dia_de(c) == dia_de(d)]
        if ult and abs(ult[-1]["close"] / d["close"] - 1) > 0.02:
            suspeitos.add(dia_de(d))
    def mercado(dia, hk, dr):
        ant = [d for d in bd if dia_de(d) < dia]
        ate = [c for c in bm if dia_de(c) == dia and datetime.fromtimestamp(c["date"], TZ) + timedelta(minutes=15) <= hk + timedelta(minutes=1)]
        if not ant or not ate:
            return None
        v = (ate[-1]["close"] / ant[-1]["close"] - 1) * 100
        return -v if dr == "short" else v      # positivo = mercado a favor da direcao do sinal
    linhas, erros = [], 0
    for t in config.UNIVERSE:
        try:
            daily = api.history(t, "3mo", "1d"); m15 = api.history(t, "1mo", "15m")
            for (dia, dr), sg in _replay_conf(daily, m15, t, config.M15_CONFIRM_CLOSES, hoje).items():
                bars = [b for b in daily if dia_de(b) > dia]
                res = outcomes.avaliar(sg, bars)
                if not res:
                    continue
                hk = datetime.strptime(f"{dia} {sg['hora']}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
                mv = mercado(dia, hk, dr)
                if mv is not None:
                    linhas.append((dia, mv, res))
        except Exception as e:
            erros += 1
            log.warning("diag_bova %s: %s", t, e)
    def linha(nome, itens):
        if not itens:
            return f"  {nome}: sem sinais"
        a = sum(1 for _, _, r in itens if r[0] == "alvo")
        m = sum(r[2] for _, _, r in itens) / len(itens)
        return f"  {nome}: {len(itens)} sinais em {len({d for d, _, _ in itens})} dias · alvo {a / len(itens) * 100:.0f}% · ação média {sgn_(m)}"
    L = [f"Robustez do fator BOVA11 ({len(linhas)} sinais)", "",
         "Dados do BOVA11 suspeitos (salto >2% num candle ou fecho do M15 diferente do diário): " + (", ".join(sorted(d.strftime('%d/%m') for d in suspeitos)) or "nenhum"), ""]
    ok = [x for x in linhas if x[0] not in suspeitos]
    for titulo, base in (("Todos os dias", linhas), ("Sem os dias suspeitos", ok)):
        L.append(titulo + ":")
        L.append(linha("Mercado a favor (>0)", [x for x in base if x[1] > 0]))
        L.append(linha("Mercado contra (≤0)", [x for x in base if x[1] <= 0]))
    dias = sorted({d for d, _, _ in ok})
    meio = dias[len(dias) // 2] if dias else None
    L += ["", f"Metades do mês (dias sem suspeita; corte em {meio.strftime('%d/%m') if meio else '-'}):"]
    for nome, sel in (("1ª metade", lambda d: d < meio), ("2ª metade", lambda d: d >= meio)):
        L.append(nome + ":")
        L.append(linha("a favor", [x for x in ok if sel(x[0]) and x[1] > 0]))
        L.append(linha("contra", [x for x in ok if sel(x[0]) and x[1] <= 0]))
    L += ["", "Por tamanho do movimento do mercado (a favor da direção, em %):"]
    for nome, lo, hi in (("contra forte (abaixo de −0,5)", -99, -0.5), ("contra leve (−0,5 a 0)", -0.5, 0.0000001), ("a favor leve (0 a 0,5)", 0.0000001, 0.5), ("a favor forte (acima de 0,5)", 0.5, 99)):
        L.append(linha(nome, [x for x in ok if lo < x[1] <= hi]))
    messenger.send("\n".join(L))

COMPARAR = ["PETR4", "PRIO3", "EMBR3", "SMAL11", "ITUB4", "BOVA11", "SUZB3", "WEGE3"]

def job_comparar():
    """Comando 'comparar' (so leitura): para ativos que mexeram forte hoje, o que a regra do robo teria visto e o que ele de fato fez."""
    api, hoje = Brapi(), now().date()
    with database.conn() as c_:
        al = {(r["ticker"], r["direction"]): json.loads(r["payload"]) for r in c_.execute("SELECT ticker, direction, payload FROM alerts WHERE date=?", (str(hoje),)).fetchall()}
    L = [f"Comparação de hoje ({hoje.strftime('%d/%m')}): o que a regra viu e o que o robô fez", ""]
    for t in COMPARAR:
        try:
            daily = api.history(t, "3mo", "1d")
            m15 = [c for c in api.history(t, "5d", "15m") if datetime.fromtimestamp(c["date"], TZ).date() == hoje]
            ant = [d for d in daily if datetime.fromtimestamp(d["date"], TZ).date() < hoje]
            if not m15 or len(ant) < config.BREAKOUT_LOOKBACK:
                L.append(f"{t}: sem candles de hoje")
                continue
            prev, last = ant[-1]["close"], m15[-1]["close"]
            chg = (last / prev - 1) * 100
            ext_hi, ext_lo = max(c["high"] for c in m15), min(c["low"] for c in m15)
            dr = "long" if chg >= 0 else "short"
            jan = ant[-config.BREAKOUT_LOOKBACK:]
            lvl = max(d["high"] for d in jan) if dr == "long" else min(d["low"] for d in jan)
            seta = "▲" if dr == "long" else "▼"
            ln = [f"{t} {seta} {sgn_(chg)} no dia ({fmt_(prev)} → {fmt_(last)}) · nível 20d {fmt_(lvl)} ({'já rompido' if (last > lvl if dr == 'long' else last < lvl) else 'não rompeu'})"]
            sig = _replay_conf(daily, [c for c in api.history(t, "5d", "15m")], t, config.M15_CONFIRM_CLOSES, hoje + timedelta(days=1)).get((hoje, dr))
            if sig:
                ln.append(f"  Regra: sinal às {sig['hora']} a {fmt_(sig['price'])} (já tinha andado {sgn_(((sig['price'] / prev - 1) * 100))} no dia)")
            else:
                ln.append("  Regra: nenhum sinal hoje · agora: " + analyzer.explain(dr, daily, m15, datetime.fromtimestamp(m15[-1]["date"], TZ) + timedelta(minutes=15)))
            a = al.get((t, dr)) or al.get((t, "short" if dr == "long" else "long"))
            if a:
                st = "sem opção" if not a.get("option") else "RUIM (não enviado)" if a.get("ruim") else "bloqueado pelo mercado (não enviado)" if a.get("bloq_mercado") else "enviado"
                ln.append(f"  Robô: alerta {st}")
            else:
                ln.append("  Robô: sem alerta" + ("" if t in config.UNIVERSE else " (ativo fora dos 30 do robô)"))
            L.append("\n".join(ln))
        except Exception as e:
            L.append(f"{t}: erro {type(e).__name__} {str(e)[:50]}")
    messenger.send("\n".join(L))

def job_comparar_dias(dias=None):
    """Comando 'dias' (so leitura): em cada dia (padrao: 05/11 e 06/11), nos ativos do robo, o que a regra viu, quanto a acao andou depois do sinal e o que o robo fez; e ativos com dado de 20 dias suspeito."""
    dias = dias or [date(2026, 11, 5), date(2026, 11, 6)]
    api, hoje = Brapi(), now().date()
    tick = list(dict.fromkeys(list(config.UNIVERSE) + ["SMAL11"]))
    por_dia = {d: [] for d in dias}
    suspeitos = []
    try:
        bd = api.history("BOVA11", "3mo", "1d"); bm = api.history("BOVA11", "1mo", "15m")
    except Exception:
        bd, bm = [], []
    dia_de = lambda c: datetime.fromtimestamp(c["date"], TZ).date()
    def mercado(dia, hk, dr):
        ant = [d for d in bd if dia_de(d) < dia]
        ate = [c for c in bm if dia_de(c) == dia and datetime.fromtimestamp(c["date"], TZ) + timedelta(minutes=15) <= hk + timedelta(minutes=1)]
        if not ant or not ate:
            return None
        v = (ate[-1]["close"] / ant[-1]["close"] - 1) * 100
        return -v if dr == "short" else v
    with database.conn() as c_:
        al = {(r["date"], r["ticker"], r["direction"]): json.loads(r["payload"]) for r in c_.execute("SELECT date, ticker, direction, payload FROM alerts").fetchall()}
    for t in tick:
        try:
            daily = api.history(t, "3mo", "1d"); m15all = api.history(t, "1mo", "15m")
            ult = daily[-1]["close"]
            jan = daily[-config.BREAKOUT_LOOKBACK - 1:-1]
            if jan and (max(d["high"] for d in jan) > ult * 1.15 or min(d["low"] for d in jan) < ult * 0.85):
                suspeitos.append(f"{t} (preço {fmt_(ult)} · máx 20d {fmt_(max(d['high'] for d in jan))} · mín 20d {fmt_(min(d['low'] for d in jan))})")
            sinais = _replay_conf(daily, m15all, t, config.M15_CONFIRM_CLOSES, hoje)
            for dia in dias:
                dd = [d for d in daily if dia_de(d) == dia]; pd_ = [d for d in daily if dia_de(d) < dia]
                if not dd or not pd_:
                    continue
                chg = (dd[0]["close"] / pd_[-1]["close"] - 1) * 100
                dr = "long" if chg >= 0 else "short"
                sg = sinais.get((dia, dr))
                a = al.get((str(dia), t, dr))
                if not sg and abs(chg) < 2.5 and not a:
                    continue
                if sg:
                    fav = (dd[0]["close"] / sg["price"] - 1) * 100 * (1 if dr == "long" else -1)
                    mv = mercado(dia, datetime.strptime(f"{dia} {sg['hora']}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ), dr)
                    regra = f"sinal {sg['hora']} a {fmt_(sg['price'])} · depois do sinal a ação fez {sgn_(fav)}" + ("" if mv is None else f" · mercado {'a favor' if mv > 0 else 'contra'}")
                else:
                    regra = "sem sinal pela regra"
                if a:
                    rob = "sem opção" if not a.get("option") else "RUIM (não enviado)" if a.get("ruim") else "enviado"
                else:
                    rob = "sem alerta" + ("" if sg else "")
                por_dia[dia].append((abs(chg), f"{t} {'▲' if dr == 'long' else '▼'} {sgn_(chg)} · {regra} · robô: {rob}"))
        except Exception as e:
            log.warning("comparar_dias %s: %s", t, e)
    L = []
    for dia in dias:
        L.append(f"{dia.strftime('%d/%m')} (regra atual, volume 1,2x, já em vigor nesse dia)")
        itens = sorted(por_dia[dia], reverse=True)[:14]
        L += [x for _, x in itens] or ["nada relevante"]
        L.append("")
    L.append("Dados de 20 dias suspeitos (nível a mais de 15% do preço): " + ("; ".join(suspeitos) if suspeitos else "nenhum"))
    messenger.send("\n".join(L))

def _sinais_repique(t, daily, m15, ate_dia, mov_min):
    """Regra de repique/recuo (teste): ativo que tocou o extremo de 20 dias nos ultimos 5 pregoes e, hoje, dois fechamentos M15 seguidos a mov_min% ou mais do fechamento de ontem no sentido oposto, com volume >= VR_MIN.
    Alta (call) vindo da minima; queda (put) vindo da maxima. Stop na minima/maxima do dia (+ buffer ATR), alvo RR_TARGET. Retorna {(dia, direcao): sinal dict}."""
    out = {}
    for dia in sorted({datetime.fromtimestamp(c["date"], TZ).date() for c in m15}):
        if dia >= ate_dia:
            continue
        hist = [d for d in daily if datetime.fromtimestamp(d["date"], TZ).date() < dia]
        if len(hist) < config.BREAKOUT_LOOKBACK + 1:
            continue
        jan = hist[-config.BREAKOUT_LOOKBACK:]
        hi, lo = max(d["high"] for d in jan), min(d["low"] for d in jan)
        rec = hist[-5:]
        a = analyzer.atr(hist)
        if not a:
            continue
        perto_lo = min(d["low"] for d in rec) <= lo * 1.02
        perto_hi = max(d["high"] for d in rec) >= hi * 0.98
        prev = hist[-1]["close"]
        avg = sum(d["volume"] for d in jan if d.get("volume")) / max(1, sum(1 for d in jan if d.get("volume")))
        mt = [c for c in m15 if datetime.fromtimestamp(c["date"], TZ).date() == dia]
        for k in range(1, len(mt)):
            hk = datetime.fromtimestamp(mt[k]["date"], TZ) + timedelta(minutes=15)
            vr = sum(c.get("volume") or 0 for c in mt[:k + 1]) / (avg * analyzer.session_fraction(hk)) if avg else 0
            if vr < config.VR_MIN:
                continue
            c1, c2 = mt[k - 1]["close"], mt[k]["close"]
            for dr in ("long", "short"):
                if (dia, dr) in out:
                    continue
                if dr == "long" and perto_lo and min(c1, c2) >= prev * (1 + mov_min / 100):
                    stop = min(c["low"] for c in mt[:k + 1]) - config.STOP_ATR_BUFFER * a
                elif dr == "short" and perto_hi and max(c1, c2) <= prev * (1 - mov_min / 100):
                    stop = max(c["high"] for c in mt[:k + 1]) + config.STOP_ATR_BUFFER * a
                else:
                    continue
                risco = abs(c2 - stop)
                if risco <= 0 or risco > config.MAX_STOP_ATR * a:
                    continue
                sg = 1 if dr == "long" else -1
                out[(dia, dr)] = dict(ticker=t, direction=dr, price=c2, level=lo if dr == "long" else hi, stop=stop, target=c2 + sg * config.RR_TARGET * risco, atr=a, vr=vr, hora=hk.strftime("%H:%M"))
    return out

def job_diag_repique():
    """Comando 'repique' (so leitura): ultimo mes nos ativos do universo, regra de repique/recuo com 2% e 3% de movimento; resultado e quais movimentos de 05/11 a 09/11 ela teria pegado."""
    api, hoje = Brapi(), now().date()
    res = {2.0: [], 3.0: []}
    recentes = []
    erros = 0
    for t in config.UNIVERSE:
        try:
            daily = api.history(t, "3mo", "1d"); m15 = api.history(t, "1mo", "15m")
            for mov in (2.0, 3.0):
                for (dia, dr), sg in _sinais_repique(t, daily, m15, hoje + timedelta(days=1), mov).items():
                    bars = [b for b in daily if datetime.fromtimestamp(b["date"], TZ).date() > dia]
                    r = outcomes.avaliar(sg, bars)
                    res[mov].append((dia, dr, r))
                    if mov == 2.0 and dia >= date(2026, 11, 5):
                        ult = [d for d in daily if datetime.fromtimestamp(d["date"], TZ).date() == dia]
                        fav = ((ult[0]["close"] / sg["price"] - 1) * 100 * (1 if dr == "long" else -1)) if ult else 0
                        recentes.append((dia, f"{dia.strftime('%d/%m')} {t} {'▲' if dr == 'long' else '▼'} sinal {sg['hora']} a {fmt_(sg['price'])} · até o fechamento {sgn_(fav)}"))
        except Exception as e:
            erros += 1
            log.warning("diag_repique %s: %s", t, e)
    def linha(nome, itens):
        fech = [r for _, _, r in itens if r]
        a = sum(1 for r in fech if r[0] == "alvo"); sd = sum(1 for r in fech if r[0] == "saida")
        m = sum(r[2] for r in fech) / len(fech) if fech else 0
        tx = f"{a / len(fech) * 100:.0f}%" if fech else "-"
        return f"{nome}: {len(itens)} sinais em {len({d for d, _, _ in itens})} dias · alvo {a} ({tx}) · saída {sd} · abertos {len(itens) - len(fech)} · ação média {sgn_(m)}"
    L = [f"Teste de repique/recuo (último mês, {len(config.UNIVERSE) - erros} ativos; volume {config.VR_MIN}x; sem filtro de IV/opção)", ""]
    for mov in (2.0, 3.0):
        L.append(f"Movimento mínimo de {messenger.fmt(mov, 0)}% sobre o fechamento de ontem:")
        L.append(linha("  Todos", res[mov]))
        L.append(linha("  Calls (repique)", [x for x in res[mov] if x[1] == "long"]))
        L.append(linha("  Puts (recuo)", [x for x in res[mov] if x[1] == "short"]))
    L += ["", "Sinais de 05/11 a 09/11 (regra de 2%):"] + [x for _, x in sorted(recentes)[:25]] + ["", "Para empatar, o alvo precisa sair em ~20% dos fechados. O mês teve queda forte seguida de repique, então o resultado pode ser bom demais para repetir."]
    messenger.send("\n".join(L))

def job_diag_forte():
    """Comando 'forte' (so leitura): cruza mercado contra/a favor com o quanto a acao ja andou alem do nivel, para ver se existe um grupo de sinais fortes."""
    api, hoje = Brapi(), now().date()
    bd = api.history("BOVA11", "3mo", "1d"); bm = api.history("BOVA11", "1mo", "15m")
    dia_de = lambda c: datetime.fromtimestamp(c["date"], TZ).date()
    def mercado(dia, hk, dr):
        ant = [d for d in bd if dia_de(d) < dia]
        ate = [c for c in bm if dia_de(c) == dia and datetime.fromtimestamp(c["date"], TZ) + timedelta(minutes=15) <= hk + timedelta(minutes=1)]
        if not ant or not ate:
            return None
        v = (ate[-1]["close"] / ant[-1]["close"] - 1) * 100
        return -v if dr == "short" else v
    linhas, erros = [], 0
    for t in config.UNIVERSE:
        try:
            daily = api.history(t, "3mo", "1d"); m15 = api.history(t, "1mo", "15m")
            for (dia, dr), sg in _replay_conf(daily, m15, t, config.M15_CONFIRM_CLOSES, hoje).items():
                if dia == date(2026, 10, 26):
                    continue   # dia com dado do BOVA11 suspeito
                bars = [b for b in daily if dia_de(b) > dia]
                res = outcomes.avaliar(sg, bars)
                hk = datetime.strptime(f"{dia} {sg['hora']}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
                mv = mercado(dia, hk, dr)
                if mv is None:
                    continue
                linhas.append(dict(dia=dia, t=t, dr=dr, mv=mv, atraso=abs(sg["price"] / sg["level"] - 1) * 100, res=res, hora=sg["hora"], price=sg["price"]))
        except Exception as e:
            erros += 1
            log.warning("diag_forte %s: %s", t, e)
    def linha(nome, itens):
        fech = [x["res"] for x in itens if x["res"]]
        if not itens:
            return f"  {nome}: sem sinais"
        a = sum(1 for r in fech if r[0] == "alvo")
        m = sum(r[2] for r in fech) / len(fech) if fech else 0
        tx = f"{a / len(fech) * 100:.0f}%" if fech else "-"
        return f"  {nome}: {len(itens)} sinais em {len({x['dia'] for x in itens})} dias · alvo {a} de {len(fech)} ({tx}) · ação média {sgn_(m)}"
    L = [f"Sinais fortes? (último mês, {len(linhas)} sinais com dado de mercado; sem 26/10)", ""]
    for nome, f in (("Mercado contra e até 0,5% além do nível", lambda x: x["mv"] <= 0 and x["atraso"] < 0.5),
                    ("Mercado contra e mais de 0,5% além do nível", lambda x: x["mv"] <= 0 and x["atraso"] >= 0.5),
                    ("Mercado a favor e até 0,5% além do nível", lambda x: x["mv"] > 0 and x["atraso"] < 0.5),
                    ("Mercado a favor e mais de 0,5% além do nível", lambda x: x["mv"] > 0 and x["atraso"] >= 0.5)):
        L.append(linha(nome, [x for x in linhas if f(x)]))
    forte = sorted([x for x in linhas if x["mv"] <= 0 and x["atraso"] < 0.5], key=lambda x: x["dia"])
    L += ["", "Sinais do grupo forte (mercado contra e até 0,5% além do nível):"]
    for x in forte[:20]:
        r = x["res"]
        L.append(f"  {x['dia'].strftime('%d/%m')} {x['t']} {'▲' if x['dr'] == 'long' else '▼'} {x['hora']} · " + (("🎯 alvo" if r[0] == "alvo" else "🛑 saída" if r[0] == "saida" else "⏰ prazo") + f" em {r[1]}d" if r else "aberto"))
    meio = sorted({x["dia"] for x in linhas})
    meio = meio[len(meio) // 2] if meio else None
    if meio:
        L += ["", f"Duas metades (corte em {meio.strftime('%d/%m')}), grupo forte:"]
        L.append(linha("1ª metade", [x for x in forte if x["dia"] < meio]))
        L.append(linha("2ª metade", [x for x in forte if x["dia"] >= meio]))
    messenger.send("\n".join(L))

def job_diag_opcao():
    try:
        _diag_opcao()
    except Exception as e:
        log.exception("diag_opcao falhou")
        messenger.send(f"Teste de opção falhou: {type(e).__name__}: {str(e)[:200]}")

def _diag_opcao():
    """Comando 'opcao' (so leitura): nos sinais do ultimo mes, retorno estimado da opcao por vencimento mensal (proximo x seguinte) e delta (0,3/0,5/0,7), saindo em 10 pregoes ou ate 1 dia antes do vencimento (com parcial de metade)."""
    import opcao_teste as ot
    from statistics import median
    api, hoje = Brapi(), now().date()
    bd = api.history("BOVA11", "3mo", "1d"); bm = api.history("BOVA11", "1mo", "15m")
    dia_de = lambda c: datetime.fromtimestamp(c["date"], TZ).date()
    def mercado(dia, hk, dr):
        ant = [d for d in bd if dia_de(d) < dia]
        ate = [c for c in bm if dia_de(c) == dia and datetime.fromtimestamp(c["date"], TZ) + timedelta(minutes=15) <= hk + timedelta(minutes=1)]
        if not ant or not ate:
            return None
        v = (ate[-1]["close"] / ant[-1]["close"] - 1) * 100
        return -v if dr == "short" else v
    DELTAS = (0.3, 0.5, 0.7)
    acc = {}    # (grupo, venc_idx, delta, politica) -> [retornos]
    nsin = {"todos": 0, "mercado contra": 0}
    erros = 0
    for t in config.UNIVERSE:
        try:
            daily = api.history(t, "3mo", "1d"); m15 = api.history("%s" % t, "1mo", "15m")
            for (dia, dr), sg in _replay_conf(daily, m15, t, config.M15_CONFIRM_CLOSES, hoje).items():
                if dia == date(2026, 10, 26):
                    continue
                bars = [b for b in daily if dia_de(b) > dia]
                hk = datetime.strptime(f"{dia} {sg['hora']}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
                mv = mercado(dia, hk, dr)
                grupos = ["todos"] + (["mercado contra"] if mv is not None and mv <= 0 else [])
                for g in grupos:
                    nsin[g] += 1
                sigma = ot.sigma_hist(daily, dia)
                for vi, venc in enumerate(ot.vencimentos(dia, 2)):
                    ate_venc = len([b for b in bars if dia_de(b) < venc])
                    for dl in DELTAS:
                        for pol, prazo in (("10 pregões", config.TIME_EXIT_DAYS), ("até 1 dia antes do vencimento", min(ate_venc, 40))):
                            if prazo <= 0:
                                continue
                            r = ot.retorno(sg, dia, bars, venc, dl, prazo, sigma)
                            if r is not None:
                                for g in grupos:
                                    acc.setdefault((g, vi, dl, pol), []).append(r)
        except Exception as e:
            erros += 1
            log.warning("diag_opcao %s: %s", t, e)
    L = [f"Qual opção teria rendido mais (último mês, {len(config.UNIVERSE) - erros} ativos; estimativa Black-Scholes, custo de 3% por lado, metade vendida no parcial)", ""]
    for g in ("todos", "mercado contra"):
        L.append(f"Sinais: {g} ({nsin[g]})")
        for vi, nome in ((0, "Vencimento mensal próximo"), (1, "Vencimento mensal seguinte")):
            L.append(f"  {nome}:")
            for dl in DELTAS:
                partes = []
                for pol in ("10 pregões", "até 1 dia antes do vencimento"):
                    x = acc.get((g, vi, dl, pol), [])
                    partes.append(f"{'10 pregões' if pol == '10 pregões' else 'até o vencimento'}: " + (f"{len(x)} · média {sgn_(sum(x) / len(x) * 100)} · mediana {sgn_(median(x) * 100)} · positivos {sum(1 for v in x if v > 0) / len(x) * 100:.0f}%" if x else "sem resolvidos"))
                L.append(f"    delta {messenger.fmt(dl, 1)}: " + " | ".join(partes))
        L.append("")
    L.append("Só ordena escolhas; não é preço real. Retorno sobre o prêmio, antes de corretagem.")
    messenger.send("\n".join(L))

def _sma(v, n):
    return sum(v[-n:]) / n if len(v) >= n else None

def _sinais_recuo(daily, bova):
    """Teste diario (1 ano): rompimento de 20 dias x recuo dentro da tendencia. Mesmo stop (1,5 ATR) e alvo (RR_TARGET) em todas as regras.
    Retorna {regra: [(indice_do_dia, sinal dict)]}. Entrada no fechamento do dia do sinal."""
    out = {"Rompimento 20d (diário)": [], "Recuo na tendência": [], "Recuo + BOVA11 a favor": []}
    ult = {}
    bclose = {datetime.fromtimestamp(b["date"], TZ).date(): b["close"] for b in bova}
    bdias = sorted(bclose)
    for i in range(60, len(daily) - 1):
        h = daily[:i + 1]; c = h[-1]; ant = h[:-1]
        a = analyzer.atr(h)
        cl = [d["close"] for d in h]
        s20, s50 = _sma(cl, 20), _sma(cl, 50)
        if not a or not s20 or not s50:
            continue
        dia = datetime.fromtimestamp(c["date"], TZ).date()
        bs = [bclose[d] for d in bdias if d <= dia]
        b20 = _sma(bs, 20)
        for dr in ("long", "short"):
            sg = 1 if dr == "long" else -1
            def mk():
                risco = 1.5 * a
                return dict(ticker="", direction=dr, price=c["close"], stop=c["close"] - sg * risco, target=c["close"] + sg * config.RR_TARGET * risco, atr=a)
            if dr == "long":
                romp = c["close"] > max(d["high"] for d in ant[-20:])
                tend = c["close"] > s50 and s20 > s50
                toque = min(d["low"] for d in h[-5:]) <= s20 + 0.5 * a
                fundo = max(d["high"] for d in h[-10:]) - c["close"] >= a
                virou = c["close"] > c["open"] and c["close"] > ant[-1]["close"]
                mercado = bool(bs and b20 and bs[-1] > b20)
            else:
                romp = c["close"] < min(d["low"] for d in ant[-20:])
                tend = c["close"] < s50 and s20 < s50
                toque = max(d["high"] for d in h[-5:]) >= s20 - 0.5 * a
                fundo = c["close"] - min(d["low"] for d in h[-10:]) >= a
                virou = c["close"] < c["open"] and c["close"] < ant[-1]["close"]
                mercado = bool(bs and b20 and bs[-1] < b20)
            for nome, ok in (("Rompimento 20d (diário)", romp), ("Recuo na tendência", tend and toque and fundo and virou), ("Recuo + BOVA11 a favor", tend and toque and fundo and virou and mercado)):
                if ok and i - ult.get((nome, dr), -99) >= 5:
                    ult[(nome, dr)] = i
                    out[nome].append((i, mk()))
    return out

def job_diag_recuo():
    try:
        _diag_recuo()
    except Exception as e:
        log.exception("diag_recuo falhou")
        messenger.send(f"Teste de recuo falhou: {type(e).__name__}: {str(e)[:200]}")

def _diag_recuo():
    """Comando 'recuo' (so leitura): 1 ano de candles diarios nos ativos; compara rompimento de 20 dias com recuo dentro da tendencia (e com BOVA11 a favor), mesmo stop e alvo."""
    api = Brapi()
    bova = api.history("BOVA11", "1y", "1d")
    acc, erros, ndias = {}, 0, 0
    for t in config.UNIVERSE:
        try:
            daily = api.history(t, "1y", "1d")
            ndias = max(ndias, len(daily))
            for nome, lst in _sinais_recuo(daily, bova).items():
                for i, sg in lst:
                    res = outcomes.avaliar(sg, daily[i + 1:])
                    if res:
                        acc.setdefault(nome, []).append((i, len(daily), res))
        except Exception as e:
            erros += 1
            log.warning("diag_recuo %s: %s", t, e)
    L = [f"Recuo na tendência x rompimento (diário, até 1 ano, {len(config.UNIVERSE) - erros} ativos). Mesmo stop de 1,5 ATR e alvo 1:{config.RR_TARGET:g}; só resolvidos.", ""]
    for nome in ("Rompimento 20d (diário)", "Recuo na tendência", "Recuo + BOVA11 a favor"):
        x = acc.get(nome, [])
        if not x:
            L.append(f"{nome}: sem sinais"); continue
        def lin(sub, rot):
            if not sub:
                return f"  {rot}: sem sinais"
            al = sum(1 for _, _, r in sub if r[0] == "alvo"); sa = sum(1 for _, _, r in sub if r[0] == "saida")
            mv = sum(r[2] for _, _, r in sub) / len(sub)
            return f"  {rot}: {len(sub)} · alvo {al / len(sub) * 100:.0f}% · saída {sa / len(sub) * 100:.0f}% · prazo {(len(sub) - al - sa) / len(sub) * 100:.0f}% · média na ação {mv:+.2f}%".replace(".", ",")
        L.append(nome)
        L.append(lin(x, "Total"))
        meio = max(n for _, n, _ in x) // 2 + 30
        L.append(lin([k for k in x if k[0] < meio], "1ª metade"))
        L.append(lin([k for k in x if k[0] >= meio], "2ª metade"))
        L.append(lin([k for k in x if k[0] >= k[1] - 22], "Último mês"))
        L.append("")
    L.append("Equilíbrio com esse payoff: alvo em cerca de 20%. Ação, não opção; sem custos.")
    messenger.send("\n".join(L))

def job_diag_cenarios():
    try:
        _diag_cenarios()
    except Exception as e:
        log.exception("diag_cenarios falhou")
        messenger.send(f"Teste de cenários falhou: {type(e).__name__}: {str(e)[:200]}")

def _diag_cenarios():
    """Comando 'cenarios' (so leitura): 1 ano de candles diarios; acerto de direcao em 1, 2 e 5 pregoes (sem stop/alvo) para rompimentos de 5/10/20 dias e para seguir so a tendencia do mercado, com o BOVA11 a favor ou contra."""
    api = Brapi()
    bova = api.history("BOVA11", "1y", "1d")
    bc = {datetime.fromtimestamp(b["date"], TZ).date(): b["close"] for b in bova}
    bd = sorted(bc)
    HZ = (1, 2, 5)
    acc = {}
    def add(bloco, nome, sg, c0, fut, i, n):
        for h in HZ:
            if i + h < n:
                m = (fut[i + h] / c0 - 1) * sg * 100
                a = acc.setdefault((bloco, nome, h), [0, 0, 0.0]); a[0] += 1; a[1] += m > 0; a[2] += m
    erros = 0
    for t in config.UNIVERSE:
        try:
            daily = api.history(t, "1y", "1d")
        except Exception as e:
            erros += 1; log.warning("diag_cenarios %s: %s", t, e); continue
        cl = [d["close"] for d in daily]; n = len(daily)
        ult = {}
        for i in range(60, n - 1):
            dia = datetime.fromtimestamp(daily[i]["date"], TZ).date()
            bs = [bc[d] for d in bd if d <= dia]
            b20 = _sma(bs, 20)
            if not b20 or len(bs) < 6:
                continue
            up = bs[-1] > b20
            for bloco, ini in (("Ano", 60), ("Últimos 6 meses", n - 125)):
                if i < ini:
                    continue
                for dr in ("long", "short"):
                    sg = 1 if dr == "long" else -1
                    fav = "a favor" if (up and dr == "long") or (not up and dr == "short") else "contra"
                    add(bloco, f"Só seguir o BOVA11 sem rompimento ({fav})", sg, cl[i], cl, i, n)
                    for L in (5, 10, 20):
                        ant = daily[i - L:i]
                        ok = cl[i] > max(d["high"] for d in ant) if dr == "long" else cl[i] < min(d["low"] for d in ant)
                        if ok and i - ult.get((L, dr, bloco), -99) >= 3:
                            ult[(L, dr, bloco)] = i
                            add(bloco, f"Rompimento {L}d, BOVA11 {fav}", sg, cl[i], cl, i, n)
    L = [f"Cenários (diário, {len(config.UNIVERSE) - erros} ativos): % de sinais em que a ação andou no sentido do sinal depois de 1, 2 e 5 pregões · média do movimento. Sem stop, sem custos.", "BOVA11 a favor = acima da média de 20 dias para call, abaixo para put.", ""]
    for bloco in ("Ano", "Últimos 6 meses"):
        L.append(bloco)
        nomes = sorted({k[1] for k in acc if k[0] == bloco})
        for nm in nomes:
            partes = []
            for h in HZ:
                a = acc.get((bloco, nm, h))
                partes.append(f"{h}d {a[1] / a[0] * 100:.0f}% {a[2] / a[0]:+.2f}%".replace(".", ",") if a else f"{h}d -")
            n0 = acc.get((bloco, nm, 1), [0])[0]
            L.append(f"  {nm} ({n0}): " + " · ".join(partes))
        L.append("")
    L.append("Acerto perto de 50% = sem vantagem. Compare cada rompimento com a linha de seguir só o mercado.")
    messenger.send("\n".join(L))

def job_diag_exemplos2():
    """Comando 'exemplos2': WEGE3 (put), B3SA3 (put) e VALE3 (call) em outubro, ativos das opcoes WEGEV510, B3SAW199 e VALEK706 citadas pelo usuario."""
    try:
        _diag_exemplos({"WEGE3": "short", "B3SA3": "short", "VALE3": "long"}, date(2026, 10, 1), date(2026, 10, 27), "Exemplos de outubro (saída antes de 28/10) (WEGEV510, B3SAW199, VALEK706)")
    except Exception as e:
        log.exception("diag_exemplos2 falhou")
        messenger.send(f"Teste de exemplos de outubro falhou: {type(e).__name__}: {str(e)[:200]}")

def job_diag_exemplos():
    try:
        _diag_exemplos()
    except Exception as e:
        log.exception("diag_exemplos falhou")
        messenger.send(f"Teste de exemplos falhou: {type(e).__name__}: {str(e)[:200]}")

def _diag_exemplos(ex=None, ini=None, fim=None, titulo=None):
    """Comando 'exemplos' (so leitura): nos ativos dos exemplos do usuario, o melhor movimento (fundo ate pico) desde 03/11, quanto uma call/put ATM teria rendido nele (Black-Scholes), e se a regra do robo sinalizou no caminho."""
    import opcao_teste as ot
    api, hoje = Brapi(), now().date()
    ini = ini or date(2026, 11, 3)
    fim = fim or date(2026, 12, 31)
    dia_de = lambda c: datetime.fromtimestamp(c["date"], TZ).date()
    ex = ex or {t: None for t in ["PETR4", "PRIO3", "USIM5", "UGPA3", "ITUB4", "B3SA3", "SIMH3", "ASAI3"]}
    EX = list(ex)
    bd = api.history("BOVA11", "3mo", "1d")
    bj = [d for d in bd if ini <= dia_de(d) <= fim]
    L = [(titulo or "Exemplos") + f": o que aconteceu de {ini.strftime('%d/%m')} em diante (estimativa Black-Scholes, opção ATM do vencimento mensal próximo, custo 3% por lado)"]
    if len(bj) > 1:
        cb = [d["close"] for d in bj]
        L.append(f"BOVA11: fundo {fmt_(min(cb))} em {dia_de(bj[cb.index(min(cb))]).strftime('%d/%m')}, último {fmt_(cb[-1])} ({sgn_((cb[-1] / min(cb) - 1) * 100)} do fundo)")
    L.append("")
    for t in EX:
        try:
            daily = api.history(t, "3mo", "1d"); m15 = api.history(t, "1mo", "15m")
            ant = [d for d in daily if dia_de(d) < ini]
            w = [d for d in daily if ini <= dia_de(d) <= fim]
            if len(w) < 3 or not ant:
                L.append(f"{t}: sem dados"); continue
            serie = [ant[-1]] + w
            cl = [d["close"] for d in serie]
            il = min(range(len(cl) - 1), key=lambda k: cl[k]); ih = max(range(il + 1, len(cl)), key=lambda k: cl[k]) if il < len(cl) - 1 else il
            sh = max(range(len(cl) - 1), key=lambda k: cl[k]); sl = min(range(sh + 1, len(cl)), key=lambda k: cl[k]) if sh < len(cl) - 1 else sh
            up, dn = cl[ih] / cl[il] - 1, cl[sh] / cl[sl] - 1
            long_ = up >= dn if not ex.get(t) else ex[t] == "long"
            i0, i1 = (il, ih) if long_ else (sh, sl)
            mov = (cl[i1] / cl[i0] - 1) * (1 if long_ else -1) * 100
            d0, d1 = dia_de(serie[i0]), dia_de(serie[i1])
            sig = ot.sigma_hist(daily, d0)
            venc = ot.vencimentos(d0, 1)[0]
            K = cl[i0]
            def op(S, dia_):
                return ot.valor(long_, S, K, (venc - dia_).days, sig)
            v0, v1 = op(cl[i0], d0), op(cl[i1], d1)
            ret = (v1 * 0.97) / (v0 * 1.03) - 1 if v0 > 0 else 0
            dias = " ".join(f"{dia_de(serie[k]).strftime('%d/%m' if titulo else '%d')}:{sgn_((cl[k] / cl[k - 1] - 1) * 100)}" for k in range(1, len(cl)))
            sinais = _replay_conf(daily, m15, t, config.M15_CONFIRM_CLOSES, hoje)
            dr = "long" if long_ else "short"
            sg = [(k[0], v) for k, v in sorted(sinais.items()) if k[1] == dr and ini <= k[0] <= d1]
            ini15 = min((dia_de(c) for c in m15), default=None)
            L.append(f"{t} {'call' if long_ else 'put'}: do fundo {d0.strftime('%d/%m')} a {fmt_(cl[i0])} ao pico {d1.strftime('%d/%m')} a {fmt_(cl[i1])} = {sgn_(mov)} na ação, opção ATM estimada {sgn_(ret * 100)}")
            L.append(f"  dias: {dias}")
            if sg:
                dd, v = sg[0]
                r2 = (ot.valor(long_, cl[i1], v["price"], (venc - d1).days, sig) * 0.97) / (ot.valor(long_, v["price"], v["price"], (venc - dd).days, sig) * 1.03) - 1
                L.append(f"  regra sinalizou {dd.strftime('%d/%m')} {v['hora']} a {fmt_(v['price'])}: até o pico, opção estimada {sgn_(r2 * 100)}")
            elif ini15 and ini15 > d0:
                L.append(f"  sem candles de 15 min antes de {ini15.strftime('%d/%m')}: não dá para refazer a regra do robô nesse trecho")
            else:
                L.append("  regra do robô não sinalizou nesse sentido antes do pico")
            with database.conn() as c_:
                rs = c_.execute("SELECT date, payload FROM alerts WHERE ticker = ? AND direction = ? AND date >= ? AND date <= ? ORDER BY date", (t, dr, str(ini), str(fim))).fetchall()
            if rs:
                L.append("  alertas do robô nesse sentido: " + ", ".join(f"{r['date'][8:10]}/{r['date'][5:7]} ({'enviado' if json.loads(r['payload']).get('option') and not json.loads(r['payload']).get('ruim') and not json.loads(r['payload']).get('bloq_mercado') else 'não enviado'})" for r in rs))
        except Exception as e:
            L.append(f"{t}: erro {type(e).__name__}")
            log.warning("diag_exemplos %s: %s", t, e)
    L.append("")
    L.append("Pico e fundo escolhidos olhando para trás: é o melhor caso possível, não o que dava para saber na hora.")
    messenger.send("\n".join(L))

def job_diag_saidas():
    try:
        _diag_saidas()
    except Exception as e:
        log.exception("diag_saidas falhou")
        messenger.send(f"Teste de saídas falhou: {type(e).__name__}: {str(e)[:200]}")

def _ret_saida(sg, bars, venc, call, K, vol, tp1, tp2):
    """Retorno estimado da opcao com saida por lucro: vende metade quando a opcao chega a +tp1 (gatilho no extremo favoravel do dia, otimista),
    o resto em +tp2 (ou deixa correr ate 1 dia antes do vencimento, com saida no empate depois do 1o gatilho). Stop da acao = 'Sair se' do robo. Custo 3% por lado."""
    import opcao_teste as ot
    dia_de = lambda c: datetime.fromtimestamp(c["date"], TZ).date()
    call_ = call
    stop, _, _alvo = outcomes.niveis(sg)
    p = sg["price"]
    prazo_b = [b for b in bars if dia_de(b) < venc][:40]
    if not prazo_b:
        return None
    ent_dia = sg["_dia"]
    v0 = ot.valor(call_, p, K, (venc - ent_dia).days, vol)
    if v0 <= 0:
        return None
    entry = v0 * 1.03
    resto, caixa, vendeu1 = 1.0, 0.0, False
    for b in prazo_b:
        dl = (venc - dia_de(b)).days
        adv, fav = (b["low"], b["high"]) if call_ else (b["high"], b["low"])
        if (call_ and adv <= stop) or ((not call_) and adv >= stop):
            if vendeu1:
                caixa += resto * min(ot.valor(call_, stop, K, dl, vol), entry * 0.9) * 0.97
            else:
                caixa += resto * ot.valor(call_, stop, K, dl, vol) * 0.97
            return caixa / entry - 1
        vf = ot.valor(call_, fav, K, dl, vol)
        if tp1 is not None and not vendeu1 and vf >= entry * (1 + tp1):
            caixa += 0.5 * entry * (1 + tp1) * 0.97; resto = 0.5; vendeu1 = True
        if tp2 is not None and vendeu1 and resto and vf >= entry * (1 + tp2):
            caixa += 0.5 * entry * (1 + tp2) * 0.97; resto = 0
            return caixa / entry - 1
        if vendeu1 and resto and ot.valor(call_, adv, K, dl, vol) <= entry * 0.9:
            caixa += resto * entry * 0.9 * 0.97; resto = 0      # depois do 1o lucro, o resto sai se a opcao voltar a -10% do preco de compra
            return caixa / entry - 1
    b = prazo_b[-1]
    caixa += resto * ot.valor(call_, b["close"], K, (venc - dia_de(b)).days, vol) * 0.97
    return caixa / entry - 1

def _diag_saidas():
    """Comando 'saidas' (so leitura): em 1 ano de rompimentos diarios de 20 dias, compara a saida atual do robo com saidas por lucro (vender metade quando a opcao sobe +30/+50/+100% e deixar o resto correr), delta 0,5, vencimento mensal proximo."""
    import opcao_teste as ot
    from statistics import median
    api = Brapi()
    dia_de = lambda c: datetime.fromtimestamp(c["date"], TZ).date()
    bova = api.history("BOVA11", "1y", "1d")
    bc = {dia_de(b): b["close"] for b in bova}; bd = sorted(bc)
    POL = [("Robô atual: 10 pregões, alvo 1:2", "robo"),
           ("Segurar até 1 dia antes do vencimento, sem realizar lucro", (None, None)),
           ("Metade em +30%, resto em +60%", (0.3, 0.6)),
           ("Metade em +50%, resto em +100%", (0.5, 1.0)),
           ("Metade em +100%, resto em +200%", (1.0, 2.0)),
           ("Metade em +50%, resto corre até o vencimento", (0.5, None))]
    acc, erros = {}, 0
    for t in config.UNIVERSE:
        try:
            daily = api.history(t, "1y", "1d")
        except Exception as e:
            erros += 1; log.warning("diag_saidas %s: %s", t, e); continue
        ult = {}
        last = dia_de(daily[-1])
        for i in range(60, len(daily) - 1):
            c = daily[i]; a = analyzer.atr(daily[:i + 1])
            if not a:
                continue
            dia = dia_de(c)
            for dr in ("long", "short"):
                ant = daily[i - 20:i]
                ok = c["close"] > max(d["high"] for d in ant) if dr == "long" else c["close"] < min(d["low"] for d in ant)
                if not ok or i - ult.get(dr, -99) < 5:
                    continue
                ult[dr] = i
                sgn = 1 if dr == "long" else -1
                risco = 1.5 * a
                sg = dict(direction=dr, price=c["close"], stop=c["close"] - sgn * risco, target=c["close"] + sgn * config.RR_TARGET * risco, _dia=dia)
                venc = ot.vencimentos(dia, 1)[0]
                if (venc - last).days > 1:      # ainda nao chegou perto do vencimento: aberto, fora do teste
                    continue
                bars = daily[i + 1:]
                call = dr == "long"
                vol = ot.sigma_hist(daily, dia)
                K = ot.strike_por_delta(call, c["close"], (venc - dia).days / 365.0, vol, 0.5)
                bs = [bc[d] for d in bd if d <= dia]
                b20 = _sma(bs, 20)
                grupos = ["todos"] + ([("BOVA11 a favor" if ((bs[-1] > b20) == call) else "BOVA11 contra")] if b20 else [])
                for nome, p in POL:
                    if p == "robo":
                        r = ot.retorno(sg, dia, bars, venc, 0.5, config.TIME_EXIT_DAYS, vol)
                    else:
                        r = _ret_saida(sg, bars, venc, call, K, vol, p[0], p[1])
                    if r is not None:
                        for g in grupos:
                            acc.setdefault((g, nome), []).append(r)
    L = [f"Saídas por lucro (último ano, {len(config.UNIVERSE) - erros} ativos, rompimento 20d diário, opção delta 0,5 do mensal próximo, só trades já encerrados no vencimento). Estimativa Black-Scholes, custo 3% por lado; gatilho no extremo do dia, então é otimista.", ""]
    for g in ("todos", "BOVA11 a favor", "BOVA11 contra"):
        n0 = len(acc.get((g, POL[0][0]), []))
        L.append(f"{g} ({n0} sinais)")
        for nome, _ in POL:
            x = acc.get((g, nome), [])
            if x:
                L.append(f"  {nome}: média {sgn_(sum(x) / len(x) * 100)} · mediana {sgn_(median(x) * 100)} · positivos {sum(1 for v in x if v > 0) / len(x) * 100:.0f}%")
        L.append("")
    L.append("Compara regras de saída nos mesmos sinais. Não é preço real e não prova lucro.")
    messenger.send("\n".join(L))

def job_diag_continuacao():
    try:
        _diag_continuacao()
    except Exception as e:
        log.exception("diag_continuacao falhou")
        messenger.send(f"Teste de continuação falhou: {type(e).__name__}: {str(e)[:200]}")

def _diag_continuacao():
    """Comando 'continuacao' (so leitura): 1 ano de candles diarios; depois de um dia forte (3%, 4% ou 6% ou mais) a acao continua no mesmo sentido? Separa por BOVA11 no mesmo dia (a favor/contra/neutro) e testa saidas por lucro na opcao."""
    import opcao_teste as ot
    from statistics import median
    api = Brapi()
    dia_de = lambda c: datetime.fromtimestamp(c["date"], TZ).date()
    bova = api.history("BOVA11", "1y", "1d")
    bchg = {dia_de(b): (b["close"] / a["close"] - 1) * 100 for a, b in zip(bova[:-1], bova[1:])}
    HZ = (1, 2, 5, 10)
    fw, mk, op = {}, {}, {}
    dias_ev, erros = {}, 0
    POL = [("Robô atual (10 pregões, alvo 1:2)", "robo"), ("Metade em +30%, resto em +60%", (0.3, 0.6)), ("Metade em +50%, resto em +100%", (0.5, 1.0)), ("Segurar até 1 dia antes do vencimento", (None, None))]
    for t in config.UNIVERSE:
        try:
            daily = api.history(t, "1y", "1d")
        except Exception as e:
            erros += 1; log.warning("diag_continuacao %s: %s", t, e); continue
        n = len(daily); last = dia_de(daily[-1]); ult = {}
        for i in range(30, n - 1):
            chg = (daily[i]["close"] / daily[i - 1]["close"] - 1) * 100
            dr = "long" if chg > 0 else "short"; sg_ = 1 if chg > 0 else -1
            dia = dia_de(daily[i])
            for lim in (3, 4, 6):
                if abs(chg) < lim or i - ult.get((lim, dr), -99) < 3:
                    continue
                ult[(lim, dr)] = i
                dias_ev.setdefault(lim, set()).add(dia)
                for h in HZ:
                    if i + h < n:
                        m = (daily[i + h]["close"] / daily[i]["close"] - 1) * sg_ * 100
                        a = fw.setdefault((lim, h), [0, 0, 0.0]); a[0] += 1; a[1] += m > 0; a[2] += m
                if lim != 4:
                    continue
                bm = bchg.get(dia)
                if bm is None:
                    continue
                bs = bm * sg_
                g = "BOVA11 a favor (±0,7% ou mais)" if bs >= 0.7 else "BOVA11 contra (±0,7% ou mais)" if bs <= -0.7 else "BOVA11 neutro"
                for h in (1, 2, 5):
                    if i + h < n:
                        m = (daily[i + h]["close"] / daily[i]["close"] - 1) * sg_ * 100
                        a = mk.setdefault((g, h), [0, 0, 0.0]); a[0] += 1; a[1] += m > 0; a[2] += m
                a_ = analyzer.atr(daily[:i + 1])
                if not a_:
                    continue
                call = dr == "long"
                risco = 1.5 * a_
                sg = dict(direction=dr, price=daily[i]["close"], stop=daily[i]["close"] - sg_ * risco, target=daily[i]["close"] + sg_ * config.RR_TARGET * risco, _dia=dia)
                venc = ot.vencimentos(dia, 1)[0]
                if (venc - last).days > 1:
                    continue
                vol = ot.sigma_hist(daily, dia)
                K = ot.strike_por_delta(call, sg["price"], (venc - dia).days / 365.0, vol, 0.5)
                for nome, p in POL:
                    r = ot.retorno(sg, dia, daily[i + 1:], venc, 0.5, config.TIME_EXIT_DAYS, vol) if p == "robo" else _ret_saida(sg, daily[i + 1:], venc, call, K, vol, p[0], p[1])
                    if r is not None:
                        for gg in ("todos", g):
                            op.setdefault((gg, nome), []).append(r)
    ln = lambda a: f"{a[1] / a[0] * 100:.0f}% {a[2] / a[0]:+.2f}%".replace(".", ",")
    L = [f"Continuação depois de dia forte (1 ano, {len(config.UNIVERSE) - erros} ativos). Entrada no fechamento do dia forte; % de casos em que a ação seguiu no mesmo sentido · média do movimento.", ""]
    for lim in (3, 4, 6):
        a1 = fw.get((lim, 1))
        if not a1:
            continue
        L.append(f"Dia de {lim}% ou mais ({a1[0]} casos em {len(dias_ev[lim])} dias):")
        L.append("  " + " · ".join(f"{h}d {ln(fw[(lim, h)])}" for h in HZ if (lim, h) in fw))
    L.append("")
    L.append("Dia de 4% ou mais, separado pelo BOVA11 no mesmo dia:")
    for g in ("BOVA11 a favor (±0,7% ou mais)", "BOVA11 neutro", "BOVA11 contra (±0,7% ou mais)"):
        if (g, 1) in mk:
            L.append(f"  {g} ({mk[(g, 1)][0]}): " + " · ".join(f"{h}d {ln(mk[(g, h)])}" for h in (1, 2, 5) if (g, h) in mk))
    L.append("")
    L.append("Opção delta 0,5 do mensal próximo depois de dia de 4% ou mais (estimativa, custo 3% por lado, gatilho no extremo do dia; só trades já encerrados no vencimento):")
    for gg in ("todos", "BOVA11 neutro", "BOVA11 contra (±0,7% ou mais)"):
        n0 = len(op.get((gg, POL[0][0]), []))
        if not n0:
            continue
        L.append(f"{gg} ({n0}):")
        for nome, _ in POL:
            x = op.get((gg, nome), [])
            if x:
                L.append(f"  {nome}: média {sgn_(sum(x) / len(x) * 100)} · mediana {sgn_(median(x) * 100)} · positivos {sum(1 for v in x if v > 0) / len(x) * 100:.0f}%")
    L.append("")
    L.append("Acerto perto de 50% = sem vantagem. Dias fortes se agrupam em poucos dias de mercado, então a amostra efetiva é menor que o número de casos.")
    messenger.send("\n".join(L))

def job_diag_lista():
    try:
        _diag_lista()
    except Exception as e:
        log.exception("diag_lista falhou")
        messenger.send(f"Teste da lista falhou: {type(e).__name__}: {str(e)[:200]}")

LISTA_OPCOES = [("B3SAJ159", 2.05, date(2026, 9, 3)), ("PETRJ500", 3.64, date(2026, 9, 15)), ("CSNAV665", 0.70, date(2026, 9, 15)), ("CMINV621", 0.74, date(2026, 9, 17)),
                ("ITUBK15", 3.10, date(2026, 9, 30)), ("MGLUK610", 1.57, date(2026, 9, 30)), ("ASAIK122", 1.56, date(2026, 10, 5)), ("CSANK395", 1.15, date(2026, 10, 5)),
                ("PRIOK690", 3.16, date(2026, 10, 15)), ("VALEW691", 2.42, date(2026, 10, 15)), ("USIMW700", 0.53, date(2026, 10, 15)), ("PETRK60", 3.61, date(2026, 10, 16)),
                ("B3SAL160", 1.13, date(2026, 10, 27)), ("CSNAX540", 0.94, date(2026, 10, 27))]
LISTA_ATIVOS = {"B3SA": "B3SA3", "PETR": "PETR4", "CSNA": "CSNA3", "CMIN": "CMIN3", "ITUB": "ITUB4", "MGLU": "MGLU3", "ASAI": "ASAI3", "CSAN": "CSAN3", "PRIO": "PRIO3", "VALE": "VALE3", "USIM": "USIM5"}

def _diag_lista():
    """Comando 'lista' (so leitura): para cada opcao da lista do usuario (com data e preco de compra), o que a ação fez, o valor estimado da opção no vencimento ou hoje, o melhor momento possível, e o cenário na entrada (distância da máx/mín de 20 dias, últimos 5 pregões, BOVA11)."""
    import re, opcao_teste as ot
    api, hoje = Brapi(), now().date()
    dia_de = lambda c: datetime.fromtimestamp(c["date"], TZ).date()
    bova = api.history("BOVA11", "6mo", "1d")
    cache, L, res = {}, [], []
    for cod, pago, d0 in LISTA_OPCOES:
        try:
            m = re.match(r"^([A-Z0-9]{4})([A-X])(\d+)$", cod)
            t = LISTA_ATIVOS[m.group(1)]; letra = m.group(2); dig = int(m.group(3))
            call = letra <= "L"
            mes = (ord(letra) - ord("A")) % 12 + 1
            ano = d0.year + (1 if mes < d0.month else 0)
            venc = ot.terceira_sexta(ano, mes)
            if t not in cache:
                cache[t] = api.history(t, "6mo", "1d")
            daily = cache[t]
            ate = [d for d in daily if dia_de(d) <= d0]
            if len(ate) < 21:
                L.append(f"{cod}: sem histórico suficiente"); continue
            S0 = ate[-1]["close"]
            K = min((dig / f for f in (1, 10, 100)), key=lambda k: abs(k - S0))
            vol = ot.sigma_hist(daily, d0 + timedelta(days=1))
            est0 = ot.valor(call, S0, K, (venc - d0).days, vol)
            flag = "" if pago and abs(est0 / pago - 1) <= 0.5 else " ⚠️ strike/preço não conferem com o prêmio pago"
            apos = [d for d in daily if d0 < dia_de(d) <= min(venc, hoje)]
            if not apos:
                L.append(f"{cod}: sem candles depois da compra"); continue
            pico = 0.0; dpico = None
            for b in apos:
                dl = (venc - dia_de(b)).days
                v = ot.valor(call, b["high"] if call else b["low"], K, dl, vol)
                if v > pico:
                    pico, dpico = v, dia_de(b)
            ult = apos[-1]; fim_v = ot.valor(call, ult["close"], K, (venc - dia_de(ult)).days, vol)
            fim_ret = fim_v * 0.97 / pago - 1; pico_ret = pico * 0.97 / pago - 1
            mov = (ult["close"] / S0 - 1) * (1 if call else -1) * 100
            salto = max(abs(b["close"] / a["close"] - 1) for a, b in zip(ate[-25:-1] + apos[:0], ate[-24:])) if len(ate) > 25 else 0
            salto2 = max((abs(b["close"] / a["close"] - 1) for a, b in zip(apos[:-1], apos[1:])), default=0)
            sus = " ⚠️ salto de dado suspeito na série" if max(salto, salto2) > 0.2 else ""
            jan = ate[-20:]
            h20, l20 = max(d["high"] for d in jan[:-1]), min(d["low"] for d in jan[:-1])
            dist = (S0 / h20 - 1) * 100 if call else (S0 / l20 - 1) * 100
            r5 = (S0 / ate[-6]["close"] - 1) * 100
            bb = [b for b in bova if dia_de(b) <= d0]
            b5 = (bb[-1]["close"] / bb[-6]["close"] - 1) * 100 if len(bb) > 6 else 0
            fim_txt = f"no vencimento {venc.strftime('%d/%m')}" if venc <= hoje else f"hoje (vence {venc.strftime('%d/%m')})"
            L.append(f"{cod} {'call' if call else 'put'} K {fmt_(K)} · {d0.strftime('%d/%m')} a {fmt_(pago)} · ação {fmt_(S0)} → {fmt_(ult['close'])} ({sgn_(mov)} a favor) · {fim_txt}: {sgn_(fim_ret * 100)} · melhor momento {dpico.strftime('%d/%m')}: {sgn_(pico_ret * 100)}{flag}{sus}")
            L.append(f"    na compra: ação a {sgn_(dist)} da {'máx' if call else 'mín'} de 20d · últimos 5 pregões {sgn_(r5)} · BOVA11 5 pregões {sgn_(b5)}")
            res.append((cod, call, fim_ret, pico_ret, mov, dist, r5, b5, bool(flag or sus)))
        except Exception as e:
            L.append(f"{cod}: erro {type(e).__name__}")
            log.warning("diag_lista %s: %s", cod, e)
    ok = [r for r in res if not r[8]]
    cab = ["Sua lista (14 opções, preço e data de compra informados; saída desconhecida). Valor por Black-Scholes, custo de venda 3%, 'melhor momento' = extremo do dia (olhando para trás)."]
    if ok:
        pos = sum(1 for r in ok if r[2] > 0)
        cab.append(f"Sem as marcadas com aviso ({len(ok)} opções): {pos} fecham positivas no vencimento/hoje e {sum(1 for r in ok if r[3] >= 1.0)} chegaram a dobrar em algum momento; média no fim {sgn_(sum(r[2] for r in ok) / len(ok) * 100)}.")
        cab.append(f"Calls: {sum(1 for r in ok if r[1])} · puts: {sum(1 for r in ok if not r[1])}. Entradas de call a {sum(r[5] for r in ok if r[1]) / max(1, sum(1 for r in ok if r[1])):+.1f}% da máx de 20d em média; de put a {sum(r[5] for r in ok if not r[1]) / max(1, sum(1 for r in ok if not r[1])):+.1f}% da mín de 20d.".replace(".", ","))
    cab.append("")
    messenger.send("\n".join(cab + L))

def job_diag_direcao():
    try:
        _diag_direcao()
    except Exception as e:
        log.exception("diag_direcao falhou")
        messenger.send(f"Teste de direção falhou: {type(e).__name__}: {str(e)[:200]}")

def _barreira(daily, i, dr, pct):
    """1 se o preco andou +pct a favor antes de -pct contra em ate 20 pregoes (mesmo dia nos dois = perda); 0 se perdeu; None se nenhum dos dois."""
    c = daily[i]["close"]; up, dn = c * (1 + pct), c * (1 - pct)
    for b in daily[i + 1:i + 21]:
        if dr == "long":
            if b["low"] <= dn: return 0
            if b["high"] >= up: return 1
        else:
            if b["high"] >= up: return 0
            if b["low"] <= dn: return 1
    return None

def _diag_direcao():
    """Comando 'direcao' (so leitura): 1 ano de candles diarios nos ativos; probabilidade de andar +3% (ou +5%) a favor antes de -3% (ou -5%) contra em ate 20 pregoes, para varias leituras de direcao. Sem vantagem = 50%."""
    api = Brapi()
    dia_de = lambda c: datetime.fromtimestamp(c["date"], TZ).date()
    bova = api.history("BOVA11", "1y", "1d")
    bcl = [b["close"] for b in bova]; bidx = {dia_de(b): k for k, b in enumerate(bova)}
    def breg(d):
        k = bidx.get(d)
        if k is None or k < 21: return None
        s20 = sum(bcl[k - 19:k + 1]) / 20
        dd = bcl[k] / max(b["high"] for b in bova[k - 19:k + 1]) - 1
        return dict(acima=bcl[k] > s20, r1=(bcl[k] / bcl[k - 1] - 1) * 100, dd=dd * 100, r10=(bcl[k] / bcl[k - 10] - 1) * 100)
    dados, erros = {}, 0
    for t in config.UNIVERSE:
        try:
            dados[t] = api.history(t, "1y", "1d")
        except Exception as e:
            erros += 1; log.warning("diag_direcao %s: %s", t, e)
    rs = {}
    for t, dl in dados.items():
        for i in range(10, len(dl)):
            k = bidx.get(dia_de(dl[i]))
            if k is not None and k >= 10:
                rs[(t, dia_de(dl[i]))] = (dl[i]["close"] / dl[i - 10]["close"] - 1) * 100 - (bcl[k] / bcl[k - 10] - 1) * 100
    por_dia = {}
    for (t, d), v in rs.items():
        por_dia.setdefault(d, []).append(v)
    q = {d: (sorted(v)[int(len(v) * 0.2)], sorted(v)[min(len(v) - 1, int(len(v) * 0.8))]) for d, v in por_dia.items() if len(v) >= 10}
    acc = {}
    def add(g, dia, w3, w5, half):
        a = acc.setdefault(g, dict(d=set(), n3=0, w3=0, n5=0, w5=0, h=[[0, 0], [0, 0]]))
        if w3 is not None:
            a["d"].add(dia); a["n3"] += 1; a["w3"] += w3; a["h"][half][0] += 1; a["h"][half][1] += w3
        if w5 is not None:
            a["n5"] += 1; a["w5"] += w5
    for t, dl in dados.items():
        n = len(dl)
        for i in range(60, n - 1):
            dia = dia_de(dl[i]); br = breg(dia)
            if not br: continue
            half = 0 if i < n // 2 else 1
            c = dl[i]["close"]
            mom5 = (c / dl[i - 5]["close"] - 1) * 100
            h20 = max(b["high"] for b in dl[i - 20:i]); l20 = min(b["low"] for b in dl[i - 20:i])
            for dr in ("long", "short"):
                sg = 1 if dr == "long" else -1
                w3, w5 = _barreira(dl, i, dr, 0.03), _barreira(dl, i, dr, 0.05)
                mkt = br["acima"] == (dr == "long")
                add("Todos os dias, call" if dr == "long" else "Todos os dias, put", dia, w3, w5, half)
                romp = c > h20 if dr == "long" else c < l20
                if romp: add("Rompimento de 20 dias", dia, w3, w5, half)
                perto = (c >= 0.98 * h20) if dr == "long" else (c <= 1.02 * l20)
                if perto and mom5 * sg >= 4:
                    add("Perto do extremo de 20d + movimento de 4% em 5 dias a favor", dia, w3, w5, half)
                    add("  … com BOVA11 a favor (acima/abaixo da média de 20d)" if mkt else "  … com BOVA11 contra", dia, w3, w5, half)
                if (t, dia) in rs and dia in q:
                    forte = rs[(t, dia)] >= q[dia][1] if dr == "long" else rs[(t, dia)] <= q[dia][0]
                    if forte:
                        add("Força relativa forte contra o BOVA11 (10 dias, 20% mais forte)", dia, w3, w5, half)
                        if mkt: add("  … com BOVA11 a favor", dia, w3, w5, half)
                if dr == "long" and br["dd"] <= -5 and br["r1"] >= 1:
                    add("Virada de mercado: BOVA11 5% abaixo da máx de 20d e dia de +1% (call)", dia, w3, w5, half)
    L = [f"Direção (último ano, {len(config.UNIVERSE) - erros} ativos): chance de andar +3% a favor antes de −3% contra em até 20 pregões (e com 5%). Sem vantagem = 50%. Só casos resolvidos.", ""]
    ordem = ["Todos os dias, call", "Todos os dias, put", "Rompimento de 20 dias", "Perto do extremo de 20d + movimento de 4% em 5 dias a favor", "  … com BOVA11 a favor (acima/abaixo da média de 20d)", "  … com BOVA11 contra",
             "Força relativa forte contra o BOVA11 (10 dias, 20% mais forte)", "  … com BOVA11 a favor", "Virada de mercado: BOVA11 5% abaixo da máx de 20d e dia de +1% (call)"]
    for g in ordem:
        a = acc.get(g)
        if not a or not a["n3"]:
            L.append(f"{g}: sem casos"); continue
        h = lambda x: f"{x[1] / x[0] * 100:.0f}%" if x[0] else "-"
        L.append(f"{g}: {a['n3']} casos em {len(a['d'])} dias · 3%: {a['w3'] / a['n3'] * 100:.0f}% · 5%: " + (f"{a['w5'] / a['n5'] * 100:.0f}%" if a["n5"] else "-") + f" · 1ª metade {h(a['h'][0])} · 2ª metade {h(a['h'][1])}")
    L.append("")
    L.append("Casos se agrupam em poucos dias de mercado: a amostra efetiva é bem menor. Ação, sem opção e sem custos. Diferença de poucos pontos de 50% é ruído.")
    messenger.send("\n".join(L))


def job_diag_auditoria():
    try:
        _diag_auditoria()
    except Exception as e:
        log.exception("diag_auditoria falhou")
        messenger.send(f"Auditoria falhou: {type(e).__name__}: {str(e)[:200]}")


def _diag_auditoria():
    from messenger import fmt
    """Comando 'auditoria' (so leitura): resumo real do robo - operacoes registradas, alertas enviados/bloqueados e placar dos resolvidos."""
    import json as _j
    L = ["AUDITORIA DO ROBÔ (dados reais)", ""]
    with database.conn() as c:
        try:
            pos = [dict(r) for r in c.execute("SELECT * FROM positions ORDER BY id").fetchall()]
        except Exception:
            pos = []
        al = [dict(r) for r in c.execute("SELECT date,ticker,direction,payload FROM alerts ORDER BY date").fetchall()]
        try:
            out = [dict(r) for r in c.execute("SELECT * FROM outcomes ORDER BY date").fetchall()]
        except Exception:
            out = []
    # 1) operacoes
    L.append("1) SUAS OPERAÇÕES")
    tot = 0.0; ntot = 0
    for p in pos:
        if p.get("status") == "open":
            L.append(f"• {p['symbol']} ABERTA ({p.get('qty')} a {fmt(p.get('entry') or 0, 2)})")
            continue
        r = p.get("realized") or 0.0
        tot += r; ntot += 1
        L.append(f"• {p['symbol']} ({p.get('origem') or 'robo'}) {str(p.get('opened'))[5:10]}: {'+' if r >= 0 else '−'}R$ {fmt(abs(r), 0)}")
    L.append(f"Total realizado: {'+' if tot >= 0 else '−'}R$ {fmt(abs(tot), 0)} em {ntot} operações")
    # 2) alertas
    bq = [a for a in al if _j.loads(a["payload"]).get("bloq_mercado")]
    env = [a for a in al if a not in bq]
    ns = [a for a in env if _j.loads(a["payload"]).get("enviado") is False]
    L += ["", "2) ALERTAS", f"Total gerados: {len(al)} · enviados: {len(env)} · bloqueados pelo BOVA: {len(bq)}"]
    for d, nm in (("long", "CALL"), ("short", "PUT")):
        L.append(f"{nm}: {sum(1 for a in env if a['direction'] == d)} enviados")
    # 3) placar
    bqk = {(a["date"], a["ticker"], a["direction"]) for a in bq}
    o = [r for r in out if (r["date"], r["ticker"], r["direction"]) not in bqk]
    L += ["", "3) PLACAR DOS ALERTAS RESOLVIDOS (ação, sem custo)"]
    for d, nm in (("long", "CALL"), ("short", "PUT"), (None, "TOTAL")):
        x = [r for r in o if d is None or r["direction"] == d]
        if not x: continue
        a = sum(1 for r in x if r["result"] == "alvo"); sd = sum(1 for r in x if r["result"] == "saida"); pz = sum(1 for r in x if r["result"] == "prazo")
        fav = sum(1 for r in x if r["move_pct"] > 0)
        med = sum(r["move_pct"] for r in x) / len(x)
        L.append(f"{nm}: {len(x)} · alvo {a} · saída {sd} · prazo {pz} · a favor {fav}/{len(x)} · movimento médio {'+' if med >= 0 else '−'}{fmt(abs(med), 1)}%")
    # 4) opcao estimada se seguisse todos
    try:
        import outcomes
        paym = {(a["date"], a["ticker"], a["direction"]): _j.loads(a["payload"]) for a in al}
        s = 0.0; n = 0
        for r in o:
            q = outcomes._sim_opcao(paym.get((r["date"], r["ticker"], r["direction"])) or {}, r)
            if q: s += q[1]; n += 1
        L += ["", f"4) Seguir todos os {n} alertas resolvidos com 100 opções: {'+' if s >= 0 else '−'}R$ {fmt(abs(s), 0)} (estimado, otimista: sem spread de ~10%)"]
    except Exception as e:
        L.append(f"4) simulação indisponível: {type(e).__name__}")
    L += ["", "Leitura: amostra pequena; testes de 5 anos não acharam vantagem de direção nos filtros testados."]
    txt = "\n".join(L)
    for i in range(0, len(txt), 3800):
        messenger.send(txt[i:i + 3800])



def job_diag_excursao():
    try:
        _diag_excursao()
    except Exception as e:
        log.exception("diag_excursao falhou")
        messenger.send(f"Teste de excursão falhou: {type(e).__name__}: {str(e)[:200]}")


def _diag_excursao():
    from messenger import fmt
    """Comando 'excursao' (so leitura): para cada alerta enviado, ate onde a acao andou a favor/contra em 10 pregoes. Quantos teriam lucro se saisse em +1%, +1,5%, +2%, +3% antes de -1,5% (e simetrico)."""
    import json as _j
    from datetime import datetime as _dt
    api = Brapi()
    with database.conn() as c:
        rows = [dict(r) for r in c.execute("SELECT date,ticker,direction,payload FROM alerts ORDER BY date").fetchall()]
    rows = [r for r in rows if not _j.loads(r["payload"]).get("bloq_mercado")]
    res = []
    for r in rows:
        p = _j.loads(r["payload"]); sig = p.get("signal")
        if not sig: continue
        try:
            daily = api.history(r["ticker"], "3mo", "1d")
        except Exception:
            continue
        bars = [b for b in daily if _dt.fromtimestamp(b["date"], positions.config_tz() if hasattr(positions, "config_tz") else None).date().isoformat() > r["date"]][:10]
        if len(bars) < 3: continue
        short = r["direction"] == "short"; pr = sig["price"]
        fav = [((pr / b["low"] - 1) if short else (b["high"] / pr - 1)) * 100 for b in bars]
        adv = [((b["high"] / pr - 1) if short else (1 - b["low"] / pr)) * 100 for b in bars]
        res.append((r["ticker"], r["date"][5:], r["direction"], max(fav), max(adv), fav, adv))
    if not res:
        messenger.send("Excursão: sem dados."); return
    L = ["EXCURSÃO DOS ALERTAS (10 pregões, preço da ação)", f"{len(res)} alertas com dados", ""]
    for t, d, di, mf, ma, _, _ in res:
        L.append(f"{'▲' if di == 'long' else '▼'} {t} {d[3:]}/{d[:2]} · máx a favor +{fmt(mf, 1)}% · máx contra −{fmt(ma, 1)}%")
    L += ["", "Se saísse no lucro X antes do contra Y (dia a dia; mesmo dia = perda):"]
    for X, Y in ((1, 1), (1.5, 1.5), (2, 2), (3, 2), (3, 3), (5, 3)):
        w = 0
        for _, _, _, _, _, fav, adv in res:
            for f, a in zip(fav, adv):
                if a >= Y: break
                if f >= X: w += 1; break
        L.append(f"+{X}% antes de −{Y}%: {w} de {len(res)} ({fmt(100 * w / len(res), 0)}%)")
    L += ["", "Nota: acaso puro daria Y/(X+Y): 50% em 1/1, 40% em 3/2."]
    txt = "\n".join(L)
    for i in range(0, len(txt), 3800):
        messenger.send(txt[i:i + 3800])



def job_diag_atraso():
    try:
        _diag_atraso()
    except Exception as e:
        log.exception("diag_atraso falhou")
        messenger.send(f"Teste de atraso falhou: {type(e).__name__}: {str(e)[:200]}")


def _diag_atraso():
    """Comando 'atraso' (so leitura): quanto a acao ja tinha andado (em favor do alerta) desde o fechamento anterior quando o alerta saiu, e o que aconteceu depois. Cedo <1%, medio 1-3%, tarde >3%."""
    from messenger import fmt
    import json as _j
    from datetime import datetime as _dt
    api = Brapi(); tz = positions.config_tz()
    with database.conn() as c:
        rows = [dict(r) for r in c.execute("SELECT date,ticker,direction,payload FROM alerts ORDER BY date").fetchall()]
    rows = [r for r in rows if not _j.loads(r["payload"]).get("bloq_mercado")]
    res = []
    for r in rows:
        sig = _j.loads(r["payload"]).get("signal")
        if not sig: continue
        try:
            daily = api.history(r["ticker"], "3mo", "1d")
        except Exception:
            continue
        ds = [(_dt.fromtimestamp(b["date"], tz).date().isoformat(), b) for b in daily]
        ant = [b for d, b in ds if d < r["date"]]
        dia = [b for d, b in ds if d == r["date"]]
        dep = [b for d, b in ds if d > r["date"]][:10]
        if not ant or len(dep) < 5: continue
        short = r["direction"] == "short"; pr = sig["price"]; pc = ant[-1]["close"]
        sg = -1 if short else 1
        ja = (pr / pc - 1) * 100 * sg
        # excluir saltos de serie (desdobramento/dado ruim)
        if abs(pr / pc - 1) > 0.25 or any(abs(b["close"] / pr - 1) > 0.4 for b in dep): continue
        r5 = (dep[4]["close"] / pr - 1) * 100 * sg
        r10 = (dep[-1]["close"] / pr - 1) * 100 * sg
        r0 = ((dia[0]["close"] / pr - 1) * 100 * sg) if dia else None
        res.append((r["ticker"], r["date"][5:], r["direction"], ja, r0, r5, r10))
    if not res:
        messenger.send("Atraso: sem dados."); return
    L = ["ALERTA CEDO OU TARDE? (preço da ação, a favor do alerta)", f"{len(res)} alertas com dados", ""]
    for nome, f in (("Cedo (ja andou menos de 1%)", lambda x: x < 1), ("Médio (1 a 3%)", lambda x: 1 <= x < 3), ("Tarde (mais de 3%)", lambda x: x >= 3)):
        g = [x for x in res if f(x[3])]
        if not g:
            L.append(f"{nome}: 0 alertas"); continue
        m5 = sum(x[5] for x in g) / len(g); m10 = sum(x[6] for x in g) / len(g)
        p5 = sum(1 for x in g if x[5] > 0)
        z = [x[4] for x in g if x[4] is not None]
        L.append(f"{nome}: {len(g)} alertas")
        L.append(f"  retorno médio em 5 pregões: {'+' if m5 >= 0 else '−'}{fmt(abs(m5), 1)}% · positivos {p5}/{len(g)}")
        L.append(f"  retorno médio em 10 pregões: {'+' if m10 >= 0 else '−'}{fmt(abs(m10), 1)}%")
        if z:
            mz = sum(z) / len(z)
            L.append(f"  até o fechamento do dia do alerta: {'+' if mz >= 0 else '−'}{fmt(abs(mz), 1)}%")
    L.append("")
    for x in sorted(res, key=lambda x: x[3]):
        L.append(f"{'▲' if x[2] == 'long' else '▼'} {x[0]} {x[1][3:]}/{x[1][:2]} · já andou {'+' if x[3] >= 0 else '−'}{fmt(abs(x[3]), 1)}% · depois 5d {'+' if x[5] >= 0 else '−'}{fmt(abs(x[5]), 1)}%")
    txt = "\n".join(L)
    for i in range(0, len(txt), 3800):
        messenger.send(txt[i:i + 3800])



def job_diag_movido():
    try:
        _diag_movido()
    except Exception as e:
        log.exception("diag_movido falhou")
        messenger.send(f"Teste do movimento falhou: {type(e).__name__}: {str(e)[:200]}")


def _diag_movido():
    """Comando 'movido' (so leitura): 5 anos de candles diarios nos ativos; dado que a acao ja andou X% no dia (fechamento vs fechamento anterior), o que acontece nos proximos 5 pregoes? Divide por metade do periodo para checar estabilidade."""
    from messenger import fmt
    api = Brapi()
    dia_de = lambda c: datetime.fromtimestamp(c["date"], TZ).date()
    faixas = [("queda de 5% ou mais", -99, -5), ("queda de 3 a 5%", -5, -3), ("queda de 1 a 3%", -3, -1), ("quase parado (-1 a +1%)", -1, 1), ("alta de 1 a 3%", 1, 3), ("alta de 3 a 5%", 3, 5), ("alta de 5% ou mais", 5, 99)]
    acc = {f[0]: [[], []] for f in faixas}   # [primeira metade, segunda metade] de retornos em 5 pregoes
    base = [[], []]; erros = 0; n_tick = 0
    for t in config.UNIVERSE:
        try:
            dl = api.history(t, "5y", "1d")
        except Exception as e:
            erros += 1; log.warning("diag_movido %s: %s", t, e); continue
        if len(dl) < 300: continue
        n_tick += 1
        cl = [b["close"] for b in dl]
        meio = len(dl) // 2
        for i in range(1, len(dl) - 5):
            if cl[i - 1] <= 0: continue
            janela = cl[i - 1:i + 6]
            if any(abs(janela[k + 1] / janela[k] - 1) > 0.25 for k in range(len(janela) - 1)):
                continue   # salto de serie (desdobramento/dado ruim)
            m = (cl[i] / cl[i - 1] - 1) * 100
            r5 = (cl[i + 5] / cl[i] - 1) * 100
            h = 0 if i < meio else 1
            base[h].append(r5)
            for nome, lo, hi in faixas:
                if lo <= m < hi:
                    acc[nome][h].append(r5); break
    if not n_tick:
        messenger.send("Movimento: sem dados."); return
    med = lambda v: sum(v) / len(v) if v else 0.0
    sg = lambda x: ("+" if x >= 0 else "−") + fmt(abs(x), 2) + "%"
    L = ["JÁ ANDOU NO DIA, E DEPOIS? (5 anos, " + str(n_tick) + " ativos, retorno em 5 pregões)", ""]
    allb = base[0] + base[1]
    L.append(f"Qualquer dia: média {sg(med(allb))} · sobe em {fmt(100 * sum(1 for x in allb if x > 0) / len(allb), 0)}% ({len(allb)} casos)")
    L.append("")
    for nome, lo, hi in faixas:
        a, b = acc[nome]; t = a + b
        if len(t) < 30:
            L.append(f"{nome}: poucos casos"); continue
        up = 100 * sum(1 for x in t if x > 0) / len(t)
        L.append(f"{nome}: {len(t)} casos · média {sg(med(t))} · sobe em {fmt(up, 0)}%")
        L.append(f"   1ª metade {sg(med(a))} ({len(a)}) · 2ª metade {sg(med(b))} ({len(b)})")
    L += ["", "Leitura: média acima do 'qualquer dia' = continua; abaixo = reverte. Só vale se as duas metades concordarem."]
    txt = "\n".join(L)
    for i in range(0, len(txt), 3800):
        messenger.send(txt[i:i + 3800])


def job_diag_setores():
    try:
        _diag_setores()
    except Exception as e:
        log.exception("diag_setores falhou")
        messenger.send(f"Teste de setores falhou: {type(e).__name__}: {str(e)[:200]}")

SETORES = {"Petróleo e combustíveis": ["PETR4", "PRIO3", "UGPA3", "CSAN3", "RAIZ4", "VBBR3", "BRAV3"],
           "Mineração e siderurgia": ["VALE3", "CSNA3", "USIM5", "GGBR4", "GOAU4", "CMIN3", "BRAP4"],
           "Bancos e finanças": ["ITUB4", "BBDC4", "BBAS3", "BPAC11", "ITSA4", "SANB11", "B3SA3"],
           "Varejo e consumo": ["MGLU3", "LREN3", "ASAI3", "ABEV3", "PCAR3", "CRFB3", "ARZZ3", "AZZA3", "VIVA3", "NATU3"],
           "Energia e saneamento": ["AXIA3", "CMIG4", "CPLE6", "SBSP3", "EQTL3", "TAEE11", "ENGI11", "ELET3", "ELET6"],
           "Indústria e transporte": ["WEGE3", "EMBR3", "RAIL3", "SIMH3", "RENT3", "CCRO3", "ECOR3"],
           "Papel e celulose": ["SUZB3", "KLBN11"]}

def _diag_setores():
    """Comando 'setores' (so leitura): 1 ano de candles diarios; o movimento do setor (sem o proprio ativo, descontado o BOVA11) nos ultimos 5/10/20 dias prevê a direcao do ativo? Mesmo criterio: +3% antes de -3% em ate 20 pregoes."""
    api = Brapi()
    dia_de = lambda c: datetime.fromtimestamp(c["date"], TZ).date()
    bova = api.history("BOVA11", "1y", "1d")
    bidx = {dia_de(b): k for k, b in enumerate(bova)}
    sec_of = {t: s for s, ts in SETORES.items() for t in ts}
    dados, erros = {}, 0
    for t in config.UNIVERSE:
        if t not in sec_of:
            continue
        try:
            dados[t] = api.history(t, "1y", "1d")
        except Exception as e:
            erros += 1; log.warning("diag_setores %s: %s", t, e)
    idx = {t: {dia_de(b): k for k, b in enumerate(dl)} for t, dl in dados.items()}
    acc = {}
    def add(g, dia, w3, w5, half):
        a = acc.setdefault(g, dict(d=set(), n3=0, w3=0, n5=0, w5=0, h=[[0, 0], [0, 0]]))
        if w3 is not None:
            a["d"].add(dia); a["n3"] += 1; a["w3"] += w3; a["h"][half][0] += 1; a["h"][half][1] += w3
        if w5 is not None:
            a["n5"] += 1; a["w5"] += w5
    for L in (5, 10, 20):
        ret = {}
        for t, dl in dados.items():
            for d, k in idx[t].items():
                if k >= L:
                    ret[(t, d)] = dl[k]["close"] / dl[k - L]["close"] - 1
        bret = {d: bova[k]["close"] / bova[k - L]["close"] - 1 for d, k in bidx.items() if k >= L}
        por_dia = {}
        for (t, d), v in ret.items():
            por_dia.setdefault(d, {}).setdefault(sec_of[t], []).append((t, v))
        val = {}
        for d, secs in por_dia.items():
            if d not in bret: continue
            for sec, mem in secs.items():
                for t, _ in mem:
                    outros = [v for m, v in mem if m != t]
                    if outros:
                        val[(t, d)] = (sum(outros) / len(outros) - bret[d]) * 100
        dias_v = {}
        for (t, d), v in val.items():
            dias_v.setdefault(d, []).append(v)
        q = {d: (sorted(v)[int(len(v) * 0.2)], sorted(v)[min(len(v) - 1, int(len(v) * 0.8))]) for d, v in dias_v.items() if len(v) >= 10}
        for (t, d), v in val.items():
            if d not in q: continue
            k = idx[t][d]; dl = dados[t]; n = len(dl)
            if k < 60 or k >= n - 1: continue
            half = 0 if k < n // 2 else 1
            forte, fraco = v >= q[d][1], v <= q[d][0]
            if not (forte or fraco): continue
            for dr in ("long", "short"):
                w3, w5 = _barreira(dl, k, dr, 0.03), _barreira(dl, k, dr, 0.05)
                if forte:
                    add((L, "Setor forte: call (a favor do setor)" if dr == "long" else "Setor forte: put (contra o setor)"), d, w3, w5, half)
                if fraco:
                    add((L, "Setor fraco: put (a favor do setor)" if dr == "short" else "Setor fraco: call (contra o setor)"), d, w3, w5, half)
    nomes = ["Setor forte: call (a favor do setor)", "Setor fraco: put (a favor do setor)", "Setor forte: put (contra o setor)", "Setor fraco: call (contra o setor)"]
    L_ = [f"Setores (último ano, {len(dados)} ativos mapeados em {len({sec_of[t] for t in dados})} setores): o setor (sem o próprio ativo, descontado o BOVA11) nos últimos 5, 10 ou 20 dias prevê o ativo? Chance de +3% a favor antes de −3% contra em até 20 pregões. Sem vantagem = 50%.", ""]
    for L in (5, 10, 20):
        L_.append(f"Setor medido em {L} dias (20% mais fortes e 20% mais fracos):")
        for nm in nomes:
            a = acc.get((L, nm))
            if not a or not a["n3"]:
                L_.append(f"  {nm}: sem casos"); continue
            h = lambda x: f"{x[1] / x[0] * 100:.0f}%" if x[0] else "-"
            L_.append(f"  {nm}: {a['n3']} casos em {len(a['d'])} dias · 3%: {a['w3'] / a['n3'] * 100:.0f}% · 5%: " + (f"{a['w5'] / a['n5'] * 100:.0f}%" if a["n5"] else "-") + f" · 1ª metade {h(a['h'][0])} · 2ª metade {h(a['h'][1])}")
    L_.append("")
    L_.append("Casos se agrupam em poucos dias de mercado. Ação, sem opção e sem custos. Diferença de poucos pontos de 50% é ruído; só vale o que se mantém acima de 55% nas duas metades.")
    messenger.send("\n".join(L_))

def job_diag_empilhar():
    try:
        _diag_empilhar()
    except Exception as e:
        log.exception("diag_empilhar falhou")
        messenger.send(f"Teste de filtros empilhados falhou: {type(e).__name__}: {str(e)[:200]}")

def _diag_empilhar(rng="1y", por_ano=False):
    """Comando 'empilhar' (so leitura): empilha leituras de direcao (setor 10d, forca relativa 10d, BOVA11 a favor, perto do extremo + movimento de 5 dias) em 6 combinacoes fixas e mede +3% antes de -3% em 20 pregoes. Inclui a combinacao completa na direcao contraria como prova de sanidade."""
    api = Brapi()
    dia_de = lambda c: datetime.fromtimestamp(c["date"], TZ).date()
    bova = api.history("BOVA11", rng, "1d")
    bcl = [b["close"] for b in bova]; bidx = {dia_de(b): k for k, b in enumerate(bova)}
    sec_of = {t: s for s, ts in SETORES.items() for t in ts}
    dados, erros = {}, 0
    for t in config.UNIVERSE:
        if t not in sec_of:
            continue
        try:
            dados[t] = api.history(t, rng, "1d")
        except Exception as e:
            erros += 1; log.warning("diag_empilhar %s: %s", t, e)
    idx = {t: {dia_de(b): k for k, b in enumerate(dl)} for t, dl in dados.items()}
    r10 = {}
    for t, dl in dados.items():
        for d, k in idx[t].items():
            if k >= 10:
                r10[(t, d)] = dl[k]["close"] / dl[k - 10]["close"] - 1
    b10 = {d: bcl[k] / bcl[k - 10] - 1 for d, k in bidx.items() if k >= 10}
    por_dia = {}
    for (t, d), v in r10.items():
        por_dia.setdefault(d, {}).setdefault(sec_of[t], []).append((t, v))
    setor, rs = {}, {}
    for d, secs in por_dia.items():
        if d not in b10: continue
        for sec, mem in secs.items():
            for t, v in mem:
                rs[(t, d)] = (v - b10[d]) * 100
                outros = [x for m, x in mem if m != t]
                if outros:
                    setor[(t, d)] = (sum(outros) / len(outros) - b10[d]) * 100
    def quant(m):
        dd = {}
        for (t, d), v in m.items():
            dd.setdefault(d, []).append(v)
        return {d: (sorted(v)[int(len(v) * 0.2)], sorted(v)[min(len(v) - 1, int(len(v) * 0.8))]) for d, v in dd.items() if len(v) >= 10}
    qs, qr = quant(setor), quant(rs)
    acc, yacc = {}, {}
    def add(g, dia, w3, w5, half):
        y = yacc.setdefault((g, dia.year), [0, 0])
        if w3 is not None:
            y[0] += 1; y[1] += w3
        a = acc.setdefault(g, dict(d=set(), n3=0, w3=0, n5=0, w5=0, h=[[0, 0], [0, 0]]))
        if w3 is not None:
            a["d"].add(dia); a["n3"] += 1; a["w3"] += w3; a["h"][half][0] += 1; a["h"][half][1] += w3
        if w5 is not None:
            a["n5"] += 1; a["w5"] += w5
    for t, dl in dados.items():
        n = len(dl)
        salto = [0] + [1 if abs(dl[i]["close"] / dl[i - 1]["close"] - 1) > 0.25 else 0 for i in range(1, n)]
        for d, k in idx[t].items():
            if por_ano and sum(salto[max(0, k - 20):k + 22]) > 0:
                continue      # protecao contra desdobramento/grupamento nao ajustado na serie
            if k < 60 or k >= n - 1 or d not in bidx or bidx[d] < 21 or (t, d) not in setor or d not in qs or d not in qr:
                continue
            kb = bidx[d]
            acima = bcl[kb] > sum(bcl[kb - 19:kb + 1]) / 20
            c = dl[k]["close"]; mom5 = (c / dl[k - 5]["close"] - 1) * 100
            h20 = max(b["high"] for b in dl[k - 20:k]); l20 = min(b["low"] for b in dl[k - 20:k])
            half = 0 if k < n // 2 else 1
            for dr in ("long", "short"):
                lg = dr == "long"
                A = setor[(t, d)] >= qs[d][1] if lg else setor[(t, d)] <= qs[d][0]
                B = rs[(t, d)] >= qr[d][1] if lg else rs[(t, d)] <= qr[d][0]
                C = acima == lg
                D = ((c >= 0.98 * h20) if lg else (c <= 1.02 * l20)) and (mom5 >= 4 if lg else mom5 <= -4)
                w3, w5 = _barreira(dl, k, dr, 0.03), _barreira(dl, k, dr, 0.05)
                add("Todos os dias (referência, call)" if lg else "Todos os dias (referência, put)", d, w3, w5, half)
                if A: add("1. Setor a favor", d, w3, w5, half)
                if A and B: add("2. Setor + força relativa a favor", d, w3, w5, half)
                if A and C: add("3. Setor + BOVA11 a favor", d, w3, w5, half)
                if B and C: add("4. Força relativa + BOVA11 a favor", d, w3, w5, half)
                if A and B and C: add("5. Setor + força relativa + BOVA11 a favor", d, w3, w5, half)
                if A and B and C and D: add("6. Os três + perto do extremo com movimento de 5 dias", d, w3, w5, half)
                # prova de sanidade: combinacao 5 na direcao contraria (call onde o setor/forca/mercado apontam para put e vice-versa)
                A2 = setor[(t, d)] <= qs[d][0] if lg else setor[(t, d)] >= qs[d][1]
                B2 = rs[(t, d)] <= qr[d][0] if lg else rs[(t, d)] >= qr[d][1]
                C2 = acima != lg
                if A2 and B2 and C2: add("Sanidade: combinação 5 invertida (operando contra)", d, w3, w5, half)
    if por_ano:
        anos = sorted({a_ for (_, a_) in yacc})
        L = [f"Validação fora da amostra ({rng} de candles diários, {len(dados)} ativos mapeados). MESMOS critérios do teste anterior, nada ajustado. Chance de +3% a favor antes de −3% contra em até 20 pregões, por ano. Sem vantagem = 50%. Casos com salto diário acima de 25% na janela foram excluídos (possível desdobramento sem ajuste).", ""]
        for g in ["Todos os dias (referência, call)", "Todos os dias (referência, put)", "2. Setor + força relativa a favor", "5. Setor + força relativa + BOVA11 a favor", "6. Os três + perto do extremo com movimento de 5 dias", "Sanidade: combinação 5 invertida (operando contra)"]:
            tot = [sum(yacc.get((g, a_), [0, 0])[0] for a_ in anos), sum(yacc.get((g, a_), [0, 0])[1] for a_ in anos)]
            if not tot[0]:
                L.append(f"{g}: sem casos"); continue
            L.append(f"{g}: total {tot[1] / tot[0] * 100:.0f}% ({tot[0]}) · " + " · ".join(f"{a_} {yacc[(g, a_)][1] / yacc[(g, a_)][0] * 100:.0f}% ({yacc[(g, a_)][0]})" for a_ in anos if yacc.get((g, a_), [0, 0])[0] >= 20))
        L.append("")
        L.append("Anos com menos de 20 casos não aparecem. Entre parênteses, número de casos (do mesmo dia andam juntos). Só vale se a combinação 5 e 6 ficarem acima de 55% e a invertida abaixo de 50% em cada ano.")
        messenger.send("\n".join(L))
        return
    L = [f"Filtros empilhados (último ano, {len(dados)} ativos mapeados). Chance de +3% a favor antes de −3% contra em até 20 pregões (e 5%). Sem vantagem = 50%. 6 combinações fixas definidas antes do teste.", ""]
    ordem = ["Todos os dias (referência, call)", "Todos os dias (referência, put)", "1. Setor a favor", "2. Setor + força relativa a favor", "3. Setor + BOVA11 a favor", "4. Força relativa + BOVA11 a favor",
             "5. Setor + força relativa + BOVA11 a favor", "6. Os três + perto do extremo com movimento de 5 dias", "Sanidade: combinação 5 invertida (operando contra)"]
    for g in ordem:
        a = acc.get(g)
        if not a or not a["n3"]:
            L.append(f"{g}: sem casos"); continue
        h = lambda x: f"{x[1] / x[0] * 100:.0f}%" if x[0] else "-"
        L.append(f"{g}: {a['n3']} casos em {len(a['d'])} dias · 3%: {a['w3'] / a['n3'] * 100:.0f}% · 5%: " + (f"{a['w5'] / a['n5'] * 100:.0f}%" if a["n5"] else "-") + f" · 1ª metade {h(a['h'][0])} · 2ª metade {h(a['h'][1])}")
    L.append("")
    L.append("Quanto mais filtros, menos casos e mais risco de ruído parecer vantagem. Só vale o que passa de 55% nas duas metades e com muitos dias distintos. Ação, sem opção e sem custos.")
    messenger.send("\n".join(L))

def job_diag_validar():
    try:
        _diag_empilhar("5y", True)
    except Exception as e:
        log.exception("diag_validar falhou")
        messenger.send(f"Validação fora da amostra falhou: {type(e).__name__}: {str(e)[:200]}")

def fmt_(x, n=2):
    return messenger.fmt(x, n)

def sgn_(v):
    return ("+" if v >= 0 else "−") + messenger.fmt(abs(v), 1) + "%"

def job_posicoes():
    """Confere stop/parcial/alvo/prazo das posicoes abertas (so avisa; nunca opera)."""
    try:
        positions.check(Brapi(), now())
    except Exception as e:
        log.warning("Posicoes: %s", e)

def job_placar():
    """Fim do dia: resolve alertas pendentes (alvo, saida ou prazo). Sexta-feira envia o placar da semana."""
    try:
        n = outcomes.update(Brapi(), now().date())
        log.info("Placar: %d alertas resolvidos agora", n)
        if now().weekday() == 4:
            messenger.send(outcomes.relatorio())
    except Exception as e:
        log.warning("Placar: %s", e)

def job_telegram_poll():
    """Le comandos do Telegram (comprei / vendi / posicoes / bomdia)."""
    positions.EXTRA.update({"bomdia": job_morning, "abertura": job_morning, "variacao": job_diag_variacao, "atrasos": job_diag_atrasos, "volume": job_diag_volume, "niveis": job_niveis, "usim5": job_explicar, "resultados": job_diag_resultados, "regras": job_diag_regras, "filtros": job_diag_filtros, "fatores": job_diag_fatores, "bova": job_diag_bova, "comparar": job_comparar, "dias": job_comparar_dias, "repique": job_diag_repique, "forte": job_diag_forte, "opcao": job_diag_opcao, "recuo": job_diag_recuo, "cenarios": job_diag_cenarios, "exemplos": job_diag_exemplos, "saidas": job_diag_saidas, "continuacao": job_diag_continuacao, "exemplos2": job_diag_exemplos2, "lista": job_diag_lista, "direcao": job_diag_direcao, "setores": job_diag_setores, "empilhar": job_diag_empilhar, "validar": job_diag_validar, "auditoria": job_diag_auditoria, "excursao": job_diag_excursao, "atraso": job_diag_atraso, "movido": job_diag_movido})
    try:
        positions.poll(Brapi(), now().date())
    except Exception as e:
        log.warning("Leitura do Telegram: %s", e)

def job_seed_csan3():
    """Unica vez: cadastra a posicao de CSANX285 comprada em 28/10 (500 a 0,44)."""
    key = "seed:csan3:v1"
    if database.get_meta(key):
        return
    opt = dict(symbol="CSANX285", strike=2.85, expiration="2026-12-18")
    msg, pid = positions.open_position("CSAN3", 500, 0.44, date(2026, 10, 28), stop=2.65, spot0=2.51, partial=2.15, target=1.91,
                                       deadline="2026-11-12", option=opt, direction="short")
    if pid and messenger.send(msg):
        database.set_meta(key, "1")

def _once_key(key, fn):
    """Roda fn uma unica vez para a chave dada (sobrevive a reinicios)."""
    def run():
        if database.get_meta(key):
            return
        fn()                       # a chave so e gravada depois: se o deploy derrubar o robo no meio, o envio roda de novo na subida
        database.set_meta(key, "1")
    return run

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

def job_hello():
    """Uma mensagem de confirmacao por token do Telegram (so na 1a vez que o token novo sobe): prova que o envio funciona."""
    import hashlib
    tag = hashlib.sha256((config.TELEGRAM_BOT_TOKEN or "").encode()).hexdigest()[:10]
    key = "hello:" + tag
    if not config.TELEGRAM_BOT_TOKEN or database.get_meta(key):
        return
    if messenger.send("Radar de Opções: conexão com o Telegram OK. Vigiando 30 ativos."):
        database.set_meta(key, "1")

def job_summary():
    """Fim do dia: uma mensagem curta com alertas enviados e quantos ativos tinham dado de 15 min (distingue 'sem rompimento' de 'dado incompleto')."""
    hoje = str(now().date())
    st = database.stats_for(hoje)
    if not st:
        return
    total = st[-1]["total"]
    melhor = max(r["analisados"] for r in st)
    ultimo = st[-1]["analisados"]
    erros = sum(r["erros"] for r in st)
    L = [f"Resumo de {now().strftime('%d/%m')}",
         f"Alertas enviados: {database.count_alerts(hoje, True)}",
         f"Ativos com dado de 15 min: até {melhor}/{total} (último ciclo: {ultimo}/{total})"]
    if erros:
        L.append(f"Erros de dados: {erros}")
    if melhor < 0.8 * total:
        L.append("⚠️ Dados incompletos: o robô pode ter perdido sinais.")
    elif database.count_alerts(hoje) == 0:
        L.append("Sem rompimento confirmado hoje.")
    sem = database.no_option_signals(hoje)
    if sem:
        L.append("Sinais sem opção: " + ", ".join(f"{t} {'▲' if d == 'long' else '▼'}" + (f" ({why.lower()})" if why else "") for t, d, why in sem))
    ruim = database.ruim_signals(hoje)
    if ruim:
        L.append("Alertas ❌ RUIM (não enviados): " + ", ".join(f"{t} {'▲' if d == 'long' else '▼'}" for t, d in ruim))
    bq = database.mercado_bloq_signals(hoje)
    if bq:
        L.append("Bloqueados (BOVA11 no mesmo sentido, não enviados): " + ", ".join(f"{t} {'▲' if d == 'long' else '▼'}" for t, d in bq))
    try:
        near = _near_list(Brapi())
        if near:
            L += ["Perto de romper amanhã:"] + near
    except Exception as e:
        log.warning("Lista de proximos rompimentos falhou: %s", e)
    try:
        cart = positions.resumo(Brapi(), now().date())
        if cart:
            L += [""] + cart
    except Exception as e:
        log.warning("Resumo da carteira falhou: %s", e)
    if messenger.send("\n".join(L)):
        database.set_meta("summary_last", hoje)

def job_boot_status():
    """Uma mensagem por deploy: versao no ar, ultimo ciclo gravado e ultimo resumo enviado (prova de vida e historico recente)."""
    ver = (os.getenv("RAILWAY_GIT_COMMIT_SHA") or "local")[:7]
    key = "boot:" + ver
    if database.get_meta(key):
        return
    with database.conn() as c_:
        r = c_.execute("SELECT ts, analisados, total FROM intraday_stats ORDER BY ts DESC LIMIT 1").fetchone()
    ult = f"{r['ts'][8:10]}/{r['ts'][5:7]} {r['ts'][11:16]} ({r['analisados']}/{r['total']} com dado)" if r else "nenhum ainda"
    resumo = database.get_meta("summary_last") or "nenhum ainda"
    if messenger.send(f"Robô atualizado (versão {ver})\nÚltimo ciclo gravado: {ult}\nÚltimo resumo diário enviado: {resumo}"):
        database.set_meta(key, "1")

def _near_list(api, today=None):
    """Ativos do universo a menos de 1% do nivel de 20 dias (busca candles diarios).
    Marca na linha o balanço que cai dentro do prazo de uma operacao (mesma janela do alerta)."""
    import report, balancos
    items = []
    for t in config.UNIVERSE:
        try:
            r = report.near_breakout(t, api.history(t, "3mo", "1d"), today, TZ)
        except BrapiError:
            continue
        if r:
            items.append((t, r))
    notas = {}
    try:
        hoje = today or now().date()
        fim = positions.add_pregoes(hoje, config.TIME_EXIT_DAYS)
        for t, _ in items:
            d = balancos.entre(t, hoje, fim)
            if d:
                notas[t] = "balanço HOJE" if d == hoje else f"balanço {d.strftime('%d/%m')}"
    except Exception as e:
        log.warning("Balanço na lista de proximos rompimentos: %s", e)
    return report.near_lines(items, notas)

def job_morning():
    """09:30: robo vivo, estado da IV do mercado e ativos perto de romper."""
    import report
    d = database.latest_status_date()
    rows = database.latest_status(d) if d else []
    cont, baratos = report.iv_counts(rows)
    L = [f"Bom dia · {now().strftime('%d/%m')}", f"Robô ativo · {len(config.UNIVERSE)} ativos",
         f"IV do mercado: {cont['cara']} cara · {cont['média']} média · {cont['barata']} barata"]
    near = _near_list(Brapi(), now().date())
    L += (["Perto de romper (a menos de 1%):"] + near) if near else ["Nenhum ativo perto de romper."]
    if baratos:
        L.append("IV barata: " + ", ".join(baratos))
    try:   # balanços de hoje ou amanha nos ativos com posicao aberta
        import balancos
        hoje = now().date(); amanha = positions.add_pregoes(hoje, 1)
        with database.conn() as c:
            positions._ensure(c)
            tks = sorted({r["ticker"] for r in positions._rows(c)})
        for t in tks:
            d = balancos.entre(t, hoje, amanha)
            if d:
                L.append(f"⚠️ Balanço de {t} {'HOJE' if d == hoje else 'amanhã'} (você tem posição)")
    except Exception as e:
        log.warning("Aviso de balanço: %s", e)
    try:   # balancos do pregao anterior e de hoje em qualquer dos ativos do universo (so o calendario; o conteudo e lido a parte)
        import balancos
        lb = balancos.linha_bom_dia(config.UNIVERSE, now().date())
        if lb:
            L.append(lb)
    except Exception as e:
        log.warning("Linha de balanços do bom dia: %s", e)
    messenger.send("\n".join(L))

def job_heartbeat():
    """13:00: prova de vida (ciclos feitos, cobertura de dados e alertas do dia)."""
    hoje = str(now().date())
    st = database.stats_for(hoje)
    if not st:
        messenger.send(f"{now().strftime('%H:%M')} · ⚠️ robô sem ciclos hoje. Verifique o Railway.")
        return
    lag = database.get_meta("lag_last")
    atraso = f"\nAtraso dos dados: ~{lag} min" + (" ⚠️" if lag and int(lag) > 20 else "") if lag else ""
    messenger.send(f"{now().strftime('%H:%M')} · robô ativo\nCiclos: {len(st)} · ativos com dado: {st[-1]['analisados']}/{st[-1]['total']}{atraso}\nAlertas hoje: {database.count_alerts(hoje, True)}")

def job_status():
    d, wl = screener.watchlist()
    print("Watchlist:", d, list(wl))
    if d:
        for r in database.latest_status(d):
            print(r)

def _util(fn):
    """Jobs agendados nao rodam em feriado da B3 (sem pregao: evita resumo, 'bom dia' e alertas com dado velho)."""
    import functools
    @functools.wraps(fn)
    def run(*a, **k):
        if str(now().date()) in positions.FERIADOS:
            log.info("Feriado da B3: %s nao roda", fn.__name__)
            return
        return fn(*a, **k)
    return run

def serve():
    from apscheduler.schedulers.blocking import BlockingScheduler
    s = BlockingScheduler(timezone=config.TZ)
    wk = "mon-fri"
    # EOD roda de manha (08:30): os dados do pregao anterior da brapi ja estao disponiveis desde ~21h
    s.add_job(job_eod, "cron", day_of_week=wk, hour=8, minute=30, id="eod", misfire_grace_time=3600, coalesce=True)
    if config.WATCHLIST_MSG and config.IV_GATE:   # com a regra B a watchlist tem todos os ativos: mensagem das 09:30 seria so ruido
        s.add_job(job_pre, "cron", day_of_week=wk, hour=9, minute=30, id="pre", misfire_grace_time=1800, coalesce=True)
    s.add_job(_util(job_intraday), "cron", day_of_week=wk, hour=9, minute="45-55/5", id="open5a")
    s.add_job(_util(job_intraday), "cron", day_of_week=wk, hour=10, minute="0,5,10", id="open5b")
    s.add_job(_util(job_intraday), "cron", day_of_week=wk, hour="10-17", minute="15,30,45", id="m15a")
    s.add_job(_util(job_intraday), "cron", day_of_week=wk, hour="11-17", minute=0, id="m15b")
    s.add_job(job_bootstrap, "cron", day_of_week=wk, hour=8, minute=0, id="bootstrap_daily", misfire_grace_time=3600, coalesce=True)
    s.add_job(job_bootstrap, "date", run_date=now() + timedelta(seconds=10), id="bootstrap")
    s.add_job(_util(job_morning), "cron", day_of_week=wk, hour=9, minute=30, id="morning", misfire_grace_time=1800, coalesce=True)
    s.add_job(_util(job_heartbeat), "cron", day_of_week=wk, hour=13, minute=0, id="heartbeat", misfire_grace_time=1800, coalesce=True)
    s.add_job(_util(job_summary), "cron", day_of_week=wk, hour=18, minute=30, id="summary", misfire_grace_time=3600, coalesce=True)
    s.add_job(job_boot_status, "date", run_date=now() + timedelta(seconds=50), id="boot_status")
    s.add_job(job_hello, "date", run_date=now() + timedelta(seconds=30), id="hello")
    # na subida, recalcula a watchlist com a regra vigente (assim a mudanca vale no mesmo dia, sem esperar o EOD das 08:30)
    s.add_job(job_eod, "date", run_date=now() + timedelta(seconds=90), id="eod_boot")
    s.add_job(_util(job_posicoes), "cron", day_of_week=wk, hour="10-17", minute="*/5", id="posicoes", max_instances=1, coalesce=True)
    s.add_job(_util(job_placar), "cron", day_of_week=wk, hour=18, minute=45, id="placar", misfire_grace_time=3600, coalesce=True)
    if now().date() == date(2026, 10, 30):   # reenvio unico da abertura de 30/10 ja no formato novo
        s.add_job(_once_key("resend:morning:2026-10-30", job_morning), "date", run_date=now() + timedelta(seconds=45), id="resend_morning")
    if now().date() == date(2026, 10, 31):   # envio unico do placar detalhado (sabado)
        s.add_job(_once_key("send:placar:2026-10-31", lambda: (outcomes.update(Brapi(), now().date()), messenger.send(outcomes.relatorio()))), "date", run_date=now() + timedelta(seconds=60), id="send_placar")
    if now().date() == date(2026, 11, 4):   # teste unico: variacao ate a hora do alerta
        s.add_job(_once_key("diag_var2:2026-11-04", job_diag_variacao), "date", run_date=now() + timedelta(seconds=60), id="diag_var")
        s.add_job(_once_key("diag_atr:2026-11-04", job_diag_atrasos), "date", run_date=now() + timedelta(seconds=75), id="diag_atr")
        s.add_job(_once_key("diag_vol:2026-11-04", job_diag_volume), "date", run_date=now() + timedelta(seconds=90), id="diag_vol")
    if now().date() == date(2026, 11, 5):   # envio unico: distancia ate o nivel (AXIA3 e vizinhos)
        s.add_job(_once_key("niveis:2026-11-05", job_niveis), "date", run_date=now() + timedelta(seconds=75), id="niveis_once")
        s.add_job(_once_key("explicar:USIM5:2026-11-05", job_explicar), "date", run_date=now() + timedelta(seconds=85), id="explicar_once")
    if now().date() == date(2026, 11, 6):   # envio unico: resultados dos alertas por IV/veredito e teste de stop mais largo
        s.add_job(_once_key("resultados:2026-11-06", job_diag_resultados), "date", run_date=now() + timedelta(seconds=80), id="resultados_once")
        s.add_job(_once_key("regras:2026-11-06", job_diag_regras), "date", run_date=now() + timedelta(seconds=85), id="regras_once")
        s.add_job(_once_key("filtros:2026-11-06", job_diag_filtros), "date", run_date=now() + timedelta(seconds=90), id="filtros_once")
    if now().date() == date(2026, 11, 9):   # (reenvio do teste BOVA11 por faixa, ja sem o caractere que o Telegram engolia)
        s.add_job(_once_key("bova2:2026-11-09", job_diag_bova), "date", run_date=now() + timedelta(seconds=95), id="bova_once2")
        s.add_job(_once_key("comparar:2026-11-09", job_comparar), "date", run_date=now() + timedelta(seconds=90), id="comparar_once")
        s.add_job(_once_key("dias:2026-11-09", job_comparar_dias), "date", run_date=now() + timedelta(seconds=100), id="dias_once")
        s.add_job(_once_key("repique:2026-11-09", job_diag_repique), "date", run_date=now() + timedelta(seconds=110), id="repique_once")
        s.add_job(_once_key("forte:2026-11-09", job_diag_forte), "date", run_date=now() + timedelta(seconds=115), id="forte_once")
    if now().date() == date(2026, 11, 9):   # envio unico: o que separa rompimentos que dao certo
        s.add_job(_once_key("fatores:2026-11-09", job_diag_fatores), "date", run_date=now() + timedelta(seconds=80), id="fatores_once")
        s.add_job(_once_key("bova:2026-11-09", job_diag_bova), "date", run_date=now() + timedelta(seconds=85), id="bova_once")
    if date(2026, 11, 10) <= now().date() <= date(2026, 11, 12):   # envio unico do teste de opcao (a chave impede repetir)
        s.add_job(_once_key("opcao2:2026-11-10", job_diag_opcao), "date", run_date=now() + timedelta(seconds=120), id="opcao_once")
    if date(2026, 11, 10) <= now().date() <= date(2026, 11, 12):   # envio unico do teste de recuo
        s.add_job(_once_key("recuo:2026-11-10", job_diag_recuo), "date", run_date=now() + timedelta(seconds=130), id="recuo_once")
    if date(2026, 11, 10) <= now().date() <= date(2026, 11, 12):   # envio unico do teste de cenarios
        s.add_job(_once_key("cenarios:2026-11-10", job_diag_cenarios), "date", run_date=now() + timedelta(seconds=140), id="cenarios_once")
    if date(2026, 11, 10) <= now().date() <= date(2026, 11, 12):   # envio unico do teste de exemplos
        s.add_job(_once_key("exemplos:2026-11-10", job_diag_exemplos), "date", run_date=now() + timedelta(seconds=150), id="exemplos_once")
    if date(2026, 11, 10) <= now().date() <= date(2026, 11, 12):   # envio unico do teste de saidas
        s.add_job(_once_key("saidas2:2026-11-10", job_diag_saidas), "date", run_date=now() + timedelta(seconds=120), id="saidas_once")
    if date(2026, 11, 10) <= now().date() <= date(2026, 11, 12):   # envio unico do teste de continuacao
        s.add_job(_once_key("continuacao2:2026-11-10", job_diag_continuacao), "date", run_date=now() + timedelta(seconds=300), id="continuacao_once")
    if date(2026, 11, 10) <= now().date() <= date(2026, 11, 12):   # envio unico dos exemplos de outubro
        s.add_job(_once_key("exemplos2c:2026-11-10", job_diag_exemplos2), "date", run_date=now() + timedelta(seconds=480), id="exemplos2_once")
    if date(2026, 11, 10) <= now().date() <= date(2026, 11, 12):   # envio unico da lista do usuario
        s.add_job(_once_key("lista:2026-11-10", job_diag_lista), "date", run_date=now() + timedelta(seconds=120), id="lista_once")
    if date(2026, 11, 11) <= now().date() <= date(2026, 11, 13):   # envio unico do teste de direcao
        s.add_job(_once_key("direcao:2026-11-11", job_diag_direcao), "date", run_date=now() + timedelta(seconds=120), id="direcao_once")
    if date(2026, 11, 11) <= now().date() <= date(2026, 11, 13):   # envio unico do teste de setores
        s.add_job(_once_key("setores:2026-11-11", job_diag_setores), "date", run_date=now() + timedelta(seconds=150), id="setores_once")
    if date(2026, 11, 11) <= now().date() <= date(2026, 11, 13):   # envio unico do teste de filtros empilhados
        s.add_job(_once_key("empilhar:2026-11-11", job_diag_empilhar), "date", run_date=now() + timedelta(seconds=170), id="empilhar_once")
    if date(2026, 11, 11) <= now().date() <= date(2026, 11, 13):   # envio unico da validacao fora da amostra
        s.add_job(_once_key("validar:2026-11-11", job_diag_validar), "date", run_date=now() + timedelta(seconds=150), id="validar_once")
    if date(2026, 11, 12) <= now().date() <= date(2026, 11, 14):   # envio unico da auditoria
        s.add_job(_once_key("auditoria:2026-11-12", job_diag_auditoria), "date", run_date=now() + timedelta(seconds=120), id="auditoria_once")
    if date(2026, 11, 12) <= now().date() <= date(2026, 11, 14):   # envio unico da excursao
        s.add_job(_once_key("excursao:2026-11-12", job_diag_excursao), "date", run_date=now() + timedelta(seconds=150), id="excursao_once")
    if date(2026, 11, 12) <= now().date() <= date(2026, 11, 14):   # envio unico do teste de atraso
        s.add_job(_once_key("atraso:2026-11-12", job_diag_atraso), "date", run_date=now() + timedelta(seconds=150), id="atraso_once")
    if date(2026, 11, 12) <= now().date() <= date(2026, 11, 14):   # envio unico do teste do movimento
        s.add_job(_once_key("movido:2026-11-12", job_diag_movido), "date", run_date=now() + timedelta(seconds=150), id="movido_once")
    s.add_job(job_telegram_poll, "interval", seconds=30, id="tg_poll", max_instances=1, coalesce=True)
    s.add_job(job_seed_csan3, "date", run_date=now() + timedelta(seconds=100), id="seed_csan3")
    s.add_job(job_diag_opcoes, "date", run_date=now() + timedelta(seconds=120), id="diag_opc")
    s.add_job(job_diag_semanais, "date", run_date=now() + timedelta(seconds=150), id="diag_sem")
    s.add_job(job_dist_iv, "date", run_date=now() + timedelta(seconds=90), id="dist_iv")
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
    jobs = {"serve": serve, "pre": job_pre, "intraday": job_intraday, "eod": job_eod, "status": job_status, "debug-iv": job_debug_iv, "backtest-iv": job_backtest_iv, "summary": job_summary, "morning": job_morning, "heartbeat": job_heartbeat, "check-iv": job_check_iv, "shadow-report": job_shadow_report, "tick": job_tick, "telegram-test": job_telegram_test, "bootstrap": job_bootstrap,
            "backfill": lambda: job_backfill(int(sys.argv[2]) if len(sys.argv) > 2 else 365)}
    if cmd not in jobs:
        sys.exit(f"Comando invalido: {cmd}. Use: {', '.join(jobs)}")
    jobs[cmd]()
