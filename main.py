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



def job_diag_toque():
    try:
        _diag_toque()
    except Exception as e:
        log.exception("diag_toque falhou")
        messenger.send(f"Teste de toque falhou: {type(e).__name__}: {str(e)[:200]}")


def _diag_toque():
    """Comando 'toque' (so leitura): para cada alerta enviado, o caminho da opcao (preco estimado por Black-Scholes, IV do alerta) ate hoje: dia e valor do pico, pior queda antes do pico e valor hoje. Usa maxima/minima diaria da acao (otimista: pico exige vender na hora certa)."""
    from messenger import fmt
    import json as _j
    from datetime import datetime as _dt
    api = Brapi(); tz = positions.config_tz()
    with database.conn() as c:
        rows = [dict(r) for r in c.execute("SELECT date,ticker,direction,payload FROM alerts ORDER BY date").fetchall()]
    rows = [r for r in rows if not _j.loads(r["payload"]).get("bloq_mercado")]
    res = []
    for r in rows:
        p = _j.loads(r["payload"]); o, sig = p.get("option"), p.get("signal")
        if not o or not sig: continue
        try:
            d0 = _dt.strptime(r["date"], "%Y-%m-%d").date()
            daily = api.history(r["ticker"], "3mo", "1d")
            bars = [(_dt.fromtimestamp(b["date"], tz).date(), b) for b in daily]
            bars = [(d, b) for d, b in bars if d > d0][:15]
            if len(bars) < 1: continue
            call = sig["direction"] == "long"
            pr = sig["price"]
            if any(abs(b["close"] / pr - 1) > 0.4 for d, b in bars): continue
            exp = _dt.strptime(o["expiration"], "%Y-%m-%d").date()
            iv = positions.implied_vol(call, o["ask"], o.get("spot_ref") or pr, o["strike"], max((exp - d0).days, 1) / 365.0)
            q = dict(direction=sig["direction"], strike=o["strike"], expiration=o["expiration"], iv=iv)
            e0 = positions.est_option(q, pr, d0)
            if not e0 or e0 <= 0: continue
            pico, dpico, kp, piorantes, piorgeral = -1e9, None, 0, 0.0, 0.0
            for k, (dd, b) in enumerate(bars, 1):
                fav = b["high"] if call else b["low"]; adv = b["low"] if call else b["high"]
                vf = positions.est_option(q, fav, dd) / e0 - 1
                va = positions.est_option(q, adv, dd) / e0 - 1
                piorgeral = min(piorgeral, va)
                if vf > pico:
                    pico, dpico, kp, piorantes = vf, dd, k, piorgeral
            hoje = positions.est_option(q, bars[-1][1]["close"], bars[-1][0]) / e0 - 1
            res.append(dict(t=r["ticker"], d=r["date"], di=r["direction"], sym=o.get("symbol", ""), pico=pico * 100, dpico=dpico, kp=kp, pa=piorantes * 100, hoje=hoje * 100, n=len(bars)))
        except Exception as e:
            log.warning("toque %s: %s", r["ticker"], e)
    if not res:
        messenger.send("Toque: sem dados."); return
    n = len(res)
    L = ["PICO DA OPÇÃO DEPOIS DE CADA ALERTA (estimado, ate 15 pregões ou hoje)", f"{n} alertas", ""]
    for X in (20, 30, 50, 100):
        h = [x for x in res if x["pico"] >= X]
        L.append(f"Pico de +{X}% ou mais: {len(h)} de {n} ({fmt(100 * len(h) / n, 0)}%)")
    L.append("")
    for x in res:
        sg = lambda v: ("+" if v >= 0 else "−") + fmt(abs(v), 0) + "%"
        cls = "JANELA" if x["pico"] >= 20 else ("fraco" if x["pico"] >= 5 else "NÃO VALIA")
        L.append(f"{'▲' if x['di'] == 'long' else '▼'} {x['sym'] or x['t']} {x['d'][8:10]}/{x['d'][5:7]} · pico {sg(x['pico'])} em {x['dpico'].strftime('%d/%m')} ({x['kp']}º pregão) · antes dele caiu até {sg(x['pa'])} · hoje {sg(x['hoje'])} · {cls}")
    L += ["", "JANELA = pico de +20% ou mais; NÃO VALIA = nunca passou de +5%. Pico usa a máxima do dia: é o teto, não o que se captura de fato."]
    txt = "\n".join(L)
    for i in range(0, len(txt), 3800):
        messenger.send(txt[i:i + 3800])



def job_diag_veredito():
    try:
        _diag_veredito()
    except Exception as e:
        log.exception("diag_veredito falhou")
        messenger.send(f"Teste do veredito falhou: {type(e).__name__}: {str(e)[:200]}")


def _diag_veredito():
    """Comando 'veredito' (so leitura): cruza o selo (BOM / NEUTRO / RUIM) e a IV de cada alerta com o pico da opcao depois dele (Black-Scholes, maxima/minima diaria da acao). Inclui os RUIM nao enviados (falso negativo)."""
    from messenger import fmt, _empate
    import json as _j, iv_filter
    from types import SimpleNamespace as NS
    from datetime import datetime as _dt
    api = Brapi(); tz = positions.config_tz()
    with database.conn() as c:
        rows = [dict(r) for r in c.execute("SELECT date,ticker,direction,payload FROM alerts ORDER BY date").fetchall()]
    res = []
    for r in rows:
        p = _j.loads(r["payload"]); o, sig = p.get("option"), p.get("signal")
        if not o or not sig or p.get("bloq_mercado"): continue
        try:
            d0 = _dt.strptime(r["date"], "%Y-%m-%d").date()
            daily = api.history(r["ticker"], "3mo", "1d")
            bars = [(_dt.fromtimestamp(b["date"], tz).date(), b) for b in daily]
            bars = [(d, b) for d, b in bars if d > d0][:15]
            if not bars: continue
            call = sig["direction"] == "long"; pr = sig["price"]
            if any(abs(b["close"] / pr - 1) > 0.4 for d, b in bars): continue
            exp = _dt.strptime(o["expiration"], "%Y-%m-%d").date()
            iv = positions.implied_vol(call, o["ask"], o.get("spot_ref") or pr, o["strike"], max((exp - d0).days, 1) / 365.0)
            q = dict(direction=sig["direction"], strike=o["strike"], expiration=o["expiration"], iv=iv)
            e0 = positions.est_option(q, pr, d0)
            if not e0 or e0 <= 0: continue
            ivi = p.get("iv") or {}
            palavra = iv_filter.iv_tag(ivi.get("iv_rank", 0), ivi.get("iv_pct", 0)).split(" ", 1)[1] if ivi else "?"
            teto = e0 * (1.3 if o.get("last_only") else 1.1)
            _, _, cobre = _empate(NS(direction=sig["direction"], price=pr, target=sig["target"]), dict(o, ask=teto))
            if p.get("ruim"): v = "RUIM"
            elif cobre and palavra != "cara": v = "BOM"
            else: v = "NEUTRO"
            pico, kp, hoje = -1e9, 0, 0
            for k, (dd, b) in enumerate(bars, 1):
                fav = b["high"] if call else b["low"]
                vf = positions.est_option(q, fav, dd) / e0 - 1
                if vf > pico: pico, kp = vf, k
            hoje = positions.est_option(q, bars[-1][1]["close"], bars[-1][0]) / e0 - 1
            res.append(dict(v=v, iv=palavra, di=r["direction"], pico=pico * 100, kp=kp, hoje=hoje * 100, t=o.get("symbol") or r["ticker"], d=r["date"]))
        except Exception as e:
            log.warning("veredito %s: %s", r["ticker"], e)
    if not res:
        messenger.send("Veredito: sem dados."); return
    def bloco(nome, g):
        if not g: return f"{nome}: 0 alertas"
        pk = sorted(x["pico"] for x in g)
        return (f"{nome}: {len(g)} alertas · pico +20%: {sum(1 for x in g if x['pico'] >= 20)} ({fmt(100 * sum(1 for x in g if x['pico'] >= 20) / len(g), 0)}%) · "
                f"+50%: {sum(1 for x in g if x['pico'] >= 50)} · nunca passou de +5%: {sum(1 for x in g if x['pico'] < 5)} · pico mediano {fmt(pk[len(pk) // 2], 0)}% · positivos hoje {sum(1 for x in g if x['hoje'] > 0)}")
    L = ["O VEREDITO ACERTA? (pico da opção depois do alerta)", f"{len(res)} alertas (inclui os RUIM não enviados)", "", "POR SELO"]
    for v in ("BOM", "NEUTRO", "RUIM"):
        L.append(bloco(v, [x for x in res if x["v"] == v]))
    L += ["", "POR IV"]
    for w in ("barata", "média", "cara"):
        L.append(bloco(w, [x for x in res if x["iv"] == w]))
    L += ["", "POR DIREÇÃO"]
    for d, nm in (("long", "CALL"), ("short", "PUT")):
        L.append(bloco(nm, [x for x in res if x["di"] == d]))
    L += ["", "Cada alerta:"]
    for x in res:
        L.append(f"{'▲' if x['di'] == 'long' else '▼'} {x['t']} {x['d'][8:10]}/{x['d'][5:7]} · {x['v']} · IV {x['iv']} · pico {'+' if x['pico'] >= 0 else '−'}{fmt(abs(x['pico']), 0)}%")
    txt = "\n".join(L)
    for i in range(0, len(txt), 3800):
        messenger.send(txt[i:i + 3800])



def job_diag_controle():
    try:
        _diag_controle()
    except Exception as e:
        log.exception("diag_controle falhou")
        messenger.send(f"Teste de controle falhou: {type(e).__name__}: {str(e)[:200]}")


def _diag_controle():
    """Comando 'controle' (so leitura): para cada alerta com opcao, compara o pico da opcao do robo com o da opcao OPOSTA (put no lugar de call e vice-versa; strike espelhado em torno do preco, mesma IV, preco por Black-Scholes). Se o oposto tambem tem muitas janelas, o 'pico' vem da volatilidade e nao do acerto de direcao."""
    from messenger import fmt
    import json as _j
    from datetime import datetime as _dt
    api = Brapi(); tz = positions.config_tz()
    with database.conn() as c:
        rows = [dict(r) for r in c.execute("SELECT date,ticker,direction,payload FROM alerts ORDER BY date").fetchall()]
    res = []
    for r in rows:
        p = _j.loads(r["payload"]); o, sig = p.get("option"), p.get("signal")
        if not o or not sig or p.get("bloq_mercado"): continue
        try:
            d0 = _dt.strptime(r["date"], "%Y-%m-%d").date()
            daily = api.history(r["ticker"], "3mo", "1d")
            bars = [(_dt.fromtimestamp(b["date"], tz).date(), b) for b in daily]
            bars = [(d, b) for d, b in bars if d > d0][:15]
            if not bars: continue
            call = sig["direction"] == "long"; pr = sig["price"]
            if any(abs(b["close"] / pr - 1) > 0.4 for d, b in bars): continue
            exp = _dt.strptime(o["expiration"], "%Y-%m-%d").date()
            iv = positions.implied_vol(call, o["ask"], o.get("spot_ref") or pr, o["strike"], max((exp - d0).days, 1) / 365.0)
            K = o["strike"]; K2 = pr * pr / K          # strike espelhado em torno do preco
            def pico_de(is_call, strike):
                q = dict(direction="long" if is_call else "short", strike=strike, expiration=o["expiration"], iv=iv)
                e0 = positions.est_option(q, pr, d0)
                if not e0 or e0 <= 0: return None
                best = -1e9
                for dd, b in bars:
                    fav = b["high"] if is_call else b["low"]
                    best = max(best, positions.est_option(q, fav, dd) / e0 - 1)
                hoje = positions.est_option(q, bars[-1][1]["close"], bars[-1][0]) / e0 - 1
                return best * 100, hoje * 100
            a = pico_de(call, K); b2 = pico_de(not call, K2)
            if a and b2:
                res.append(dict(di=r["direction"], t=o.get("symbol") or r["ticker"], d=r["date"], a=a, b=b2))
        except Exception as e:
            log.warning("controle %s: %s", r["ticker"], e)
    if not res:
        messenger.send("Controle: sem dados."); return
    def bloco(nome, g, k):
        pk = sorted(x[k][0] for x in g)
        return (f"{nome}: pico +20% em {sum(1 for x in g if x[k][0] >= 20)} de {len(g)} ({fmt(100 * sum(1 for x in g if x[k][0] >= 20) / len(g), 0)}%) · +50%: {sum(1 for x in g if x[k][0] >= 50)} · "
                f"nunca passou de +5%: {sum(1 for x in g if x[k][0] < 5)} · mediano {fmt(pk[len(pk) // 2], 0)}% · positivos hoje {sum(1 for x in g if x[k][1] > 0)}")
    L = ["CONTROLE: OPÇÃO DO ROBÔ x OPÇÃO OPOSTA (mesmos alertas)", f"{len(res)} alertas", ""]
    for titulo, g in (("TODOS", res), ("SÓ PUTS (robô)", [x for x in res if x["di"] == "short"]), ("SÓ CALLS (robô)", [x for x in res if x["di"] == "long"])):
        if not g: continue
        L += [titulo, "  " + bloco("Robô  ", g, "a"), "  " + bloco("Oposta", g, "b")]
    melhor = sum(1 for x in res if x["a"][0] > x["b"][0])
    L += ["", f"Em quantos alertas o pico do robô foi maior que o da oposta: {melhor} de {len(res)}", "", "Cada alerta (pico robô / pico oposta):"]
    sg = lambda v: ("+" if v >= 0 else "−") + fmt(abs(v), 0) + "%"
    for x in res:
        L.append(f"{'▲' if x['di'] == 'long' else '▼'} {x['t']} {x['d'][8:10]}/{x['d'][5:7]} · {sg(x['a'][0])} / {sg(x['b'][0])}")
    L += ["", "Oposta = strike espelhado, mesma IV, sem skew nem spread. Compara ordem de grandeza, não valores exatos."]
    txt = "\n".join(L)
    for i in range(0, len(txt), 3800):
        messenger.send(txt[i:i + 3800])



def job_diag_regime():
    try:
        _diag_regime()
    except Exception as e:
        log.exception("diag_regime falhou")
        messenger.send(f"Teste de regime falhou: {type(e).__name__}: {str(e)[:200]}")


def _diag_regime():
    """Comando 'regime' (so leitura): 5 anos de candles diarios nos ativos. Rompimento do piso (fecha abaixo da minima de 20 dias) ou do topo (fecha acima da maxima de 20 dias), separado pelo mercado: BOVA11 abaixo ou acima da media de 20 dias. Mede, no sentido do sinal, retorno em 5 pregoes, % positivos e +3% antes de -3% em ate 20 pregoes. Compara com qualquer dia no mesmo regime (para ver se o rompimento acrescenta algo) e mostra as duas metades do periodo."""
    from messenger import fmt
    api = Brapi()
    dia_de = lambda c: datetime.fromtimestamp(c["date"], TZ).date()
    bova = api.history("BOVA11", "5y", "1d")
    bcl = [b["close"] for b in bova]; bidx = {dia_de(b): k for k, b in enumerate(bova)}
    def regime(d):
        k = bidx.get(d)
        if k is None or k < 20: return None
        return "alta" if bcl[k] >= sum(bcl[k - 19:k + 1]) / 20 else "queda"
    chaves = [("short", "queda"), ("short", "alta"), ("long", "queda"), ("long", "alta")]
    sinal = {k: [[], []] for k in chaves}; base = {k: [[], []] for k in chaves}   # [metade][(r5, barreira)]
    n_tick = 0
    for t in config.UNIVERSE:
        try:
            dl = api.history(t, "5y", "1d")
        except Exception as e:
            log.warning("diag_regime %s: %s", t, e); continue
        if len(dl) < 300: continue
        n_tick += 1
        cl = [b["close"] for b in dl]
        salto = [0] + [1 if abs(cl[i] / cl[i - 1] - 1) > 0.25 else 0 for i in range(1, len(cl))]
        pref = [0]
        for x in salto: pref.append(pref[-1] + x)
        meio = len(dl) // 2
        for i in range(21, len(dl) - 21):
            if pref[i + 22] - pref[i - 1] > 0: continue     # janela com salto de serie
            rg = regime(dia_de(dl[i]))
            if rg is None: continue
            h = 0 if i < meio else 1
            lo20 = min(b["low"] for b in dl[i - 20:i]); hi20 = max(b["high"] for b in dl[i - 20:i])
            for dr in ("short", "long"):
                sg = -1 if dr == "short" else 1
                r5 = (cl[i + 5] / cl[i] - 1) * 100 * sg
                br = _barreira(dl, i, dr, 0.03)
                base[(dr, rg)][h].append((r5, br))
                if (dr == "short" and cl[i] < lo20) or (dr == "long" and cl[i] > hi20):
                    sinal[(dr, rg)][h].append((r5, br))
    if not n_tick:
        messenger.send("Regime: sem dados."); return
    def resumo(v):
        if not v: return "sem casos"
        up = 100 * sum(1 for x in v if x[0] > 0) / len(v)
        bb = [x[1] for x in v if x[1] is not None]
        w = (100 * sum(bb) / len(bb)) if bb else 0
        return f"{len(v)} casos · média 5d {'+' if sum(x[0] for x in v) / len(v) >= 0 else '−'}{fmt(abs(sum(x[0] for x in v) / len(v)), 2)}% · sobe {fmt(up, 0)}% · +3% antes de −3%: {fmt(w, 0)}%"
    L = ["ROMPIMENTO x MERCADO (5 anos, " + str(n_tick) + " ativos; mercado = BOVA11 vs média de 20 dias)", "No sentido do sinal. Sem vantagem = ~50%.", ""]
    for dr, nm in (("short", "PUT: fecha abaixo da mínima de 20 dias"), ("long", "CALL: fecha acima da máxima de 20 dias")):
        L.append(nm)
        for rg in ("queda", "alta"):
            a, b = sinal[(dr, rg)]
            ba, bb_ = base[(dr, rg)]
            L.append(f"  Mercado em {rg}:")
            L.append(f"    rompimento: {resumo(a + b)}")
            L.append(f"      1ª metade: {resumo(a)}")
            L.append(f"      2ª metade: {resumo(b)}")
            L.append(f"    qualquer dia: {resumo(ba + bb_)}")
        L.append("")
    L.append("Leitura: o rompimento só acrescenta algo se ficar acima de 'qualquer dia' no mesmo mercado, nas duas metades.")
    txt = "\n".join(L)
    for i in range(0, len(txt), 3800):
        messenger.send(txt[i:i + 3800])



def job_diag_regime_vivo():
    try:
        _diag_regime_vivo()
    except Exception as e:
        log.exception("diag_regime_vivo falhou")
        messenger.send(f"Teste de regime nos alertas falhou: {type(e).__name__}: {str(e)[:200]}")


def _diag_regime_vivo():
    """Comando 'regimevivo' (so leitura): para cada alerta do robo (enviados, RUIM e bloqueados pelo filtro do BOVA), o mercado no dia (BOVA11 do fechamento anterior vs media de 20 dias) e o pico da opcao depois do alerta. Mostra quantos puts caem em mercado em alta e lista os bloqueados."""
    from messenger import fmt
    import json as _j
    from datetime import datetime as _dt
    api = Brapi(); tz = positions.config_tz()
    bova = api.history("BOVA11", "3mo", "1d")
    bd = [(_dt.fromtimestamp(b["date"], tz).date(), b["close"]) for b in bova]
    def regime(d0):
        ant = [c for d, c in bd if d < d0]
        if len(ant) < 20: return None
        return "alta" if ant[-1] >= sum(ant[-20:]) / 20 else "queda"
    with database.conn() as c:
        rows = [dict(r) for r in c.execute("SELECT date,ticker,direction,payload FROM alerts ORDER BY date").fetchall()]
    res = []
    for r in rows:
        p = _j.loads(r["payload"]); o, sig = p.get("option"), p.get("signal")
        if not o or not sig: continue
        try:
            d0 = _dt.strptime(r["date"], "%Y-%m-%d").date()
            rg = regime(d0)
            if rg is None: continue
            daily = api.history(r["ticker"], "3mo", "1d")
            bars = [(_dt.fromtimestamp(b["date"], tz).date(), b) for b in daily]
            bars = [(d, b) for d, b in bars if d > d0][:15]
            if not bars: continue
            call = sig["direction"] == "long"; pr = sig["price"]
            if any(abs(b["close"] / pr - 1) > 0.4 for d, b in bars): continue
            exp = _dt.strptime(o["expiration"], "%Y-%m-%d").date()
            iv = positions.implied_vol(call, o["ask"], o.get("spot_ref") or pr, o["strike"], max((exp - d0).days, 1) / 365.0)
            q = dict(direction=sig["direction"], strike=o["strike"], expiration=o["expiration"], iv=iv)
            e0 = positions.est_option(q, pr, d0)
            if not e0 or e0 <= 0: continue
            pico = max(positions.est_option(q, b["high"] if call else b["low"], dd) / e0 - 1 for dd, b in bars) * 100
            hoje = (positions.est_option(q, bars[-1][1]["close"], bars[-1][0]) / e0 - 1) * 100
            res.append(dict(di=r["direction"], rg=rg, pico=pico, hoje=hoje, t=o.get("symbol") or r["ticker"], d=r["date"], bloq=bool(p.get("bloq_mercado")), env=not (p.get("ruim") or p.get("bloq_mercado"))))
        except Exception as e:
            log.warning("regimevivo %s: %s", r["ticker"], e)
    if not res:
        messenger.send("Regime nos alertas: sem dados."); return
    def bloco(nome, g):
        if not g: return f"{nome}: 0 alertas"
        pk = sorted(x["pico"] for x in g)
        return (f"{nome}: {len(g)} alertas · pico +20%: {sum(1 for x in g if x['pico'] >= 20)} · +50%: {sum(1 for x in g if x['pico'] >= 50)} · "
                f"nunca passou de +5%: {sum(1 for x in g if x['pico'] < 5)} · mediano {fmt(pk[len(pk) // 2], 0)}% · positivos hoje {sum(1 for x in g if x['hoje'] > 0)}")
    L = ["ALERTAS DO ROBÔ x MERCADO NO DIA (BOVA11 vs média de 20 dias)", f"{len(res)} alertas com opção (inclui RUIM e bloqueados)", ""]
    for di, nm in (("short", "PUTS"), ("long", "CALLS")):
        L.append(nm)
        for rg in ("alta", "queda"):
            L.append("  " + bloco(f"mercado em {rg}", [x for x in res if x["di"] == di and x["rg"] == rg]))
    L += ["", "Bloqueados pelo filtro do BOVA (não chegaram a você):"]
    bq = [x for x in res if x["bloq"]]
    if not bq: L.append("  nenhum com dados")
    for x in bq:
        L.append(f"  {'▲' if x['di'] == 'long' else '▼'} {x['t']} {x['d'][8:10]}/{x['d'][5:7]} · mercado em {x['rg']} · pico {'+' if x['pico'] >= 0 else '−'}{fmt(abs(x['pico']), 0)}% · hoje {'+' if x['hoje'] >= 0 else '−'}{fmt(abs(x['hoje']), 0)}%")
    L += ["", "Puts com mercado em alta (os que a regra cortaria):"]
    pa = [x for x in res if x["di"] == "short" and x["rg"] == "alta"]
    if not pa: L.append("  nenhum")
    for x in pa:
        L.append(f"  ▼ {x['t']} {x['d'][8:10]}/{x['d'][5:7]} · pico {'+' if x['pico'] >= 0 else '−'}{fmt(abs(x['pico']), 0)}% · hoje {'+' if x['hoje'] >= 0 else '−'}{fmt(abs(x['hoje']), 0)}%")
    txt = "\n".join(L)
    for i in range(0, len(txt), 3800):
        messenger.send(txt[i:i + 3800])



def _supertrend_dir(dl, per=10, mult=3.0):
    """Direcao (1 alta / -1 baixa) da linha de tendencia por ATR (tipo SuperTrend)."""
    n = len(dl)
    tr = [dl[0]["high"] - dl[0]["low"]] + [max(dl[i]["high"] - dl[i]["low"], abs(dl[i]["high"] - dl[i - 1]["close"]), abs(dl[i]["low"] - dl[i - 1]["close"])) for i in range(1, n)]
    atr = [0.0] * n
    for i in range(n):
        atr[i] = sum(tr[:i + 1]) / (i + 1) if i < per else (atr[i - 1] * (per - 1) + tr[i]) / per
    d = [1] * n; up = [0.0] * n; lo = [0.0] * n
    for i in range(n):
        m = (dl[i]["high"] + dl[i]["low"]) / 2
        bu, bl = m + mult * atr[i], m - mult * atr[i]
        if i == 0:
            up[i], lo[i] = bu, bl; continue
        up[i] = bu if (bu < up[i - 1] or dl[i - 1]["close"] > up[i - 1]) else up[i - 1]
        lo[i] = bl if (bl > lo[i - 1] or dl[i - 1]["close"] < lo[i - 1]) else lo[i - 1]
        if d[i - 1] == -1 and dl[i]["close"] > up[i - 1]: d[i] = 1
        elif d[i - 1] == 1 and dl[i]["close"] < lo[i - 1]: d[i] = -1
        else: d[i] = d[i - 1]
    return d


def _macd_hist(cl, a=12, b=26, c=9):
    def ema(v, p):
        k = 2 / (p + 1); o = [v[0]]
        for x in v[1:]: o.append(x * k + o[-1] * (1 - k))
        return o
    m = [x - y for x, y in zip(ema(cl, a), ema(cl, b))]
    sg = ema(m, c)
    return [x - y for x, y in zip(m, sg)]


def job_diag_macd():
    try:
        _diag_macd()
    except Exception as e:
        log.exception("diag_macd falhou")
        messenger.send(f"Teste do MACD falhou: {type(e).__name__}: {str(e)[:200]}")


def _diag_macd():
    """Comando 'macd' (so leitura): regra do usuario em candles diarios, 5 anos. Entrada: a linha de tendencia (ATR 10 x 3) vira E o histograma do MACD (12,26,9) cruza o zero no mesmo sentido, com ate 2 candles de diferenca. Saida alternativa: quando o histograma comeca a encolher. Compara com so a virada da linha (sem MACD) e com qualquer dia."""
    from messenger import fmt
    api = Brapi()
    ativos = list(config.UNIVERSE) + ["BOVA11"]
    G = {}
    def add(g, dr, h, r5, r10, br, rs, dias):
        x = G.setdefault((g, dr), dict(h=[[], []], r5=[], r10=[], br=[], rs=[], dias=[]))
        x["h"][h].append(r5)
        x["r5"].append(r5); x["r10"].append(r10)
        if br is not None: x["br"].append(br)
        x["rs"].append(rs); x["dias"].append(dias)
    n_tick = 0
    for t in ativos:
        try:
            dl = api.history(t, "5y", "1d")
        except Exception as e:
            log.warning("diag_macd %s: %s", t, e); continue
        if len(dl) < 300: continue
        n_tick += 1
        cl = [b["close"] for b in dl]; N = len(dl)
        salto = [0] + [1 if abs(cl[i] / cl[i - 1] - 1) > 0.25 else 0 for i in range(1, N)]
        pref = [0]
        for x in salto: pref.append(pref[-1] + x)
        di = _supertrend_dir(dl); hi = _macd_hist(cl)
        meio = N // 2
        ult = {("flip+macd", 1): -99, ("flip+macd", -1): -99, ("so linha", 1): -99, ("so linha", -1): -99}
        for i in range(60, N - 31):
            if pref[i + 32] - pref[i - 1] > 0: continue
            flip = {1: di[i] == 1 and di[i - 1] == -1, -1: di[i] == -1 and di[i - 1] == 1}
            cruz = {1: hi[i] > 0 >= hi[i - 1], -1: hi[i] < 0 <= hi[i - 1]}
            h = 0 if i < meio else 1
            for dr in (1, -1):
                grupos = ["qualquer dia"]
                if flip[dr]: grupos.append("so linha")
                fl_rec = any(di[k] == dr and di[k - 1] == -dr for k in range(i - 2, i + 1))
                cr_rec = any((hi[k] > 0 >= hi[k - 1]) if dr == 1 else (hi[k] < 0 <= hi[k - 1]) for k in range(i - 2, i + 1))
                if (flip[dr] and cr_rec) or (cruz[dr] and fl_rec):
                    grupos.append("flip+macd")
                for g in grupos:
                    if g != "qualquer dia":
                        if i - ult[(g, dr)] < 5: continue
                        ult[(g, dr)] = i
                    r5 = (cl[i + 5] / cl[i] - 1) * 100 * dr
                    r10 = (cl[i + 10] / cl[i] - 1) * 100 * dr
                    br = _barreira(dl, i, "long" if dr == 1 else "short", 0.03)
                    k_s = i + 30
                    for k in range(i + 1, i + 31):
                        if (dr == 1 and hi[k] < hi[k - 1]) or (dr == -1 and hi[k] > hi[k - 1]):
                            k_s = k; break
                    rs = (cl[k_s] / cl[i] - 1) * 100 * dr
                    add(g, dr, h, r5, r10, br, rs, k_s - i)
    if not n_tick:
        messenger.send("MACD: sem dados."); return
    med = lambda v: sum(v) / len(v) if v else 0.0
    sg = lambda x: ("+" if x >= 0 else "−") + fmt(abs(x), 2) + "%"
    def lin(x):
        if not x or len(x["r5"]) < 5: return "poucos casos"
        n = len(x["r5"]); up = 100 * sum(1 for v in x["r5"] if v > 0) / n
        br = (100 * sum(x["br"]) / len(x["br"])) if x["br"] else 0
        ups = 100 * sum(1 for v in x["rs"] if v > 0) / n
        return (f"{n} casos · 5d {sg(med(x['r5']))} ({fmt(up, 0)}% a favor) · 10d {sg(med(x['r10']))} · +3% antes de −3%: {fmt(br, 0)}%\n"
                f"      saída no encolher do histograma: {sg(med(x['rs']))} ({fmt(ups, 0)}% positivos, {fmt(med(x['dias']), 0)} pregões)")
    L = [f"REGRA DO MACD + LINHA (5 anos, diário, {n_tick} ativos incl. BOVA11)", "Linha ATR 10x3 · MACD 12,26,9 · entrada no fechamento do sinal", ""]
    for dr, nm in ((-1, "VENDA (put)"), (1, "COMPRA (call)")):
        L.append(nm)
        for g in ("flip+macd", "so linha", "qualquer dia"):
            L.append(f"  {g}: " + lin(G.get((g, dr))))
        x = G.get(("flip+macd", dr))
        if x and len(x["h"][0]) >= 5 and len(x["h"][1]) >= 5:
            L.append(f"  1ª metade {sg(med(x['h'][0]))} ({len(x['h'][0])}) · 2ª metade {sg(med(x['h'][1]))} ({len(x['h'][1])}) (retorno 5d)")
        L.append("")
    L.append("Leitura: a regra só importa se flip+macd ficar acima de 'qualquer dia' nas duas metades e acima de 'só linha'.")
    txt = "\n".join(L)
    for i in range(0, len(txt), 3800):
        messenger.send(txt[i:i + 3800])



def job_diag_macd_valida():
    try:
        _diag_macd_valida()
    except Exception as e:
        log.exception("diag_macd_valida falhou")
        messenger.send(f"Validação do MACD falhou: {type(e).__name__}: {str(e)[:200]}")


def _diag_macd_valida():
    """Comando 'macdvalida' (so leitura): a regra linha+MACD (mesmos parametros do 'macd') ano a ano, ativo a ativo, sem os 5 melhores ativos e agrupando os sinais por semana (para tirar a correlacao entre ativos que viram juntos)."""
    from messenger import fmt
    api = Brapi()
    dia_de = lambda c: datetime.fromtimestamp(c["date"], TZ).date()
    sinais = []      # (dr, ticker, data, r5, barreira)
    base = {}        # (dr, ano) -> [r5 lista, barreiras]
    n_tick = 0
    for t in list(config.UNIVERSE) + ["BOVA11"]:
        try:
            dl = api.history(t, "5y", "1d")
        except Exception as e:
            log.warning("diag_macd_valida %s: %s", t, e); continue
        if len(dl) < 300: continue
        n_tick += 1
        cl = [b["close"] for b in dl]; N = len(dl)
        salto = [0] + [1 if abs(cl[i] / cl[i - 1] - 1) > 0.25 else 0 for i in range(1, N)]
        pref = [0]
        for x in salto: pref.append(pref[-1] + x)
        di = _supertrend_dir(dl); hi = _macd_hist(cl)
        ult = {1: -99, -1: -99}
        for i in range(60, N - 31):
            if pref[i + 32] - pref[i - 1] > 0: continue
            d = dia_de(dl[i])
            for dr in (1, -1):
                r5 = (cl[i + 5] / cl[i] - 1) * 100 * dr
                br = _barreira(dl, i, "long" if dr == 1 else "short", 0.03)
                b = base.setdefault((dr, d.year), [[], []]); b[0].append(r5)
                if br is not None: b[1].append(br)
                fl = di[i] == dr and di[i - 1] == -dr
                cr = (hi[i] > 0 >= hi[i - 1]) if dr == 1 else (hi[i] < 0 <= hi[i - 1])
                fl_rec = any(di[k] == dr and di[k - 1] == -dr for k in range(i - 2, i + 1))
                cr_rec = any((hi[k] > 0 >= hi[k - 1]) if dr == 1 else (hi[k] < 0 <= hi[k - 1]) for k in range(i - 2, i + 1))
                if ((fl and cr_rec) or (cr and fl_rec)) and i - ult[dr] >= 5:
                    ult[dr] = i
                    sinais.append((dr, t, d, r5, br))
    if not n_tick or not sinais:
        messenger.send("Validação do MACD: sem dados."); return
    med = lambda v: sum(v) / len(v) if v else 0.0
    sg = lambda x: ("+" if x >= 0 else "−") + fmt(abs(x), 2) + "%"
    pc = lambda v: fmt(100 * sum(v) / len(v), 0) + "%" if v else "-"
    L = [f"VALIDAÇÃO DA REGRA LINHA + MACD ({n_tick} ativos, 5 anos)", "Retorno em 5 pregões no sentido do sinal; entre parênteses a média de qualquer dia no mesmo ano", ""]
    for dr, nm in ((1, "COMPRA"), (-1, "VENDA")):
        L.append(nm + " · por ano")
        anos = sorted({s_[2].year for s_ in sinais if s_[0] == dr})
        for a in anos:
            g = [s_ for s_ in sinais if s_[0] == dr and s_[2].year == a]
            b = base.get((dr, a), [[], []])
            if len(g) < 5: continue
            L.append(f"  {a}: {len(g)} sinais · {sg(med([x[3] for x in g]))} ({sg(med(b[0]))}) · a favor {pc([1 if x[3] > 0 else 0 for x in g])} · +3% antes de −3%: {pc([x[4] for x in g if x[4] is not None])} ({pc(b[1])})")
        g = [s_ for s_ in sinais if s_[0] == dr]
        por = {}
        for x in g: por.setdefault(x[1], []).append(x[3])
        lista = sorted(((med(v), t, len(v)) for t, v in por.items() if len(v) >= 3), reverse=True)
        pos = sum(1 for m, _, _ in lista if m > 0)
        L.append(f"  por ativo (com 3+ sinais): {pos} de {len(lista)} com média positiva")
        top5 = {t for _, t, _ in lista[:5]}
        rest = [x[3] for x in g if x[1] not in top5]
        L.append(f"  sem os 5 melhores ativos ({', '.join(sorted(top5))}): {len(rest)} sinais · {sg(med(rest))} · a favor {pc([1 if v > 0 else 0 for v in rest])}")
        sem = {}
        for x in g: sem.setdefault(x[2].isocalendar()[:2], []).append(x[3])
        medias = [med(v) for v in sem.values()]
        L.append(f"  por semana (tira a correlação): {len(sem)} semanas com sinal · média {sg(med(medias))} · semanas positivas {pc([1 if m > 0 else 0 for m in medias])}")
        bo = [x[3] for x in g if x[1] == "BOVA11"]
        if bo: L.append(f"  só BOVA11: {len(bo)} sinais · {sg(med(bo))} · a favor {pc([1 if v > 0 else 0 for v in bo])}")
        L.append("")
    L.append("Leitura: vale se o sinal ganhar do 'qualquer dia' na maioria dos anos, sem depender de poucos ativos, e se as semanas positivas passarem de ~55%.")
    txt = "\n".join(L)
    for i in range(0, len(txt), 3800):
        messenger.send(txt[i:i + 3800])



def job_diag_macd_opcao():
    try:
        _diag_macd_opcao()
    except Exception as e:
        log.exception("diag_macd_opcao falhou")
        messenger.send(f"Teste do MACD na opção falhou: {type(e).__name__}: {str(e)[:200]}")


def _diag_macd_opcao():
    """Comando 'macdopcao' (so leitura): a regra linha+MACD traduzida para opcao (Black-Scholes, delta 0,5, proximo vencimento mensal com 25+ dias, vol historica de 20 dias, spread de 5% por lado). Saida pelo encolher do histograma e saida fixa em 5 pregoes; compara com entrar em qualquer dia (5 pregoes)."""
    from messenger import fmt
    import opcao_teste as ot
    api = Brapi(); CUSTO = 0.05
    dia_de = lambda c: datetime.fromtimestamp(c["date"], TZ).date()
    R = {}
    def add(chave, **kw):
        x = R.setdefault(chave, dict(rs=[], r5=[], pico=[], dias=[]))
        for k, v in kw.items(): x[k].append(v)
    def opcao(dl, i, dr, vol_d):
        call = dr == 1; d = dia_de(dl[i]); S = dl[i]["close"]
        venc = ot.vencimentos(d, 1, 25)[0]
        sig = ot.sigma_hist(dl[:i + 1], d)
        dias0 = (venc - d).days
        K = ot.strike_por_delta(call, S, dias0 / 365.0, sig, 0.5)
        p0 = ot.valor(call, S, K, dias0, sig) * (1 + CUSTO)
        return call, venc, sig, K, p0
    n_tick = 0
    for t in list(config.UNIVERSE) + ["BOVA11"]:
        try:
            dl = api.history(t, "5y", "1d")
        except Exception as e:
            log.warning("diag_macd_opcao %s: %s", t, e); continue
        if len(dl) < 300: continue
        n_tick += 1
        grupo = "BOVA11" if t == "BOVA11" else "ações"
        cl = [b["close"] for b in dl]; N = len(dl)
        salto = [0] + [1 if abs(cl[i] / cl[i - 1] - 1) > 0.25 else 0 for i in range(1, N)]
        pref = [0]
        for x in salto: pref.append(pref[-1] + x)
        di = _supertrend_dir(dl); hi = _macd_hist(cl)
        ult = {1: -99, -1: -99}
        for i in range(60, N - 32):
            if pref[i + 33] - pref[i - 1] > 0: continue
            for dr in (1, -1):
                fl = di[i] == dr and di[i - 1] == -dr
                cr = (hi[i] > 0 >= hi[i - 1]) if dr == 1 else (hi[i] < 0 <= hi[i - 1])
                fl_rec = any(di[k] == dr and di[k - 1] == -dr for k in range(i - 2, i + 1))
                cr_rec = any((hi[k] > 0 >= hi[k - 1]) if dr == 1 else (hi[k] < 0 <= hi[k - 1]) for k in range(i - 2, i + 1))
                sinal = ((fl and cr_rec) or (cr and fl_rec)) and i - ult[dr] >= 5
                amostra = (i % 5 == 0) and t != "BOVA11"
                if not sinal and not amostra: continue
                if sinal: ult[dr] = i
                try:
                    call, venc, sig, K, p0 = opcao(dl, i, dr, None)
                    if p0 <= 0: continue
                    d0 = dia_de(dl[i])
                    def val(k, preco):
                        return ot.valor(call, preco, K, (venc - dia_de(dl[k])).days, sig)
                    r5 = val(i + 5, cl[i + 5]) * (1 - CUSTO) / p0 - 1
                    k_s = i + 30
                    for k in range(i + 1, i + 31):
                        if (dr == 1 and hi[k] < hi[k - 1]) or (dr == -1 and hi[k] > hi[k - 1]):
                            k_s = k; break
                    rs = val(k_s, cl[k_s]) * (1 - CUSTO) / p0 - 1
                    pico = max(val(k, dl[k]["high"] if call else dl[k]["low"]) * (1 - CUSTO) / p0 - 1 for k in range(i + 1, k_s + 1))
                    if sinal: add((grupo, dr, "sinal"), rs=rs * 100, r5=r5 * 100, pico=pico * 100, dias=k_s - i)
                    if amostra: add((grupo, dr, "qualquer dia"), r5=r5 * 100)
                except Exception as e:
                    log.warning("macd_opcao %s: %s", t, e)
    if not n_tick:
        messenger.send("MACD na opção: sem dados."); return
    med = lambda v: sorted(v)[len(v) // 2] if v else 0.0
    avg = lambda v: sum(v) / len(v) if v else 0.0
    sg = lambda x: ("+" if x >= 0 else "−") + fmt(abs(x), 0) + "%"
    L = ["REGRA LINHA + MACD NA OPÇÃO (estimada, 5 anos)", "Opção no dinheiro (delta 0,5), próximo vencimento mensal com 25+ dias, spread de 5% por lado", ""]
    for g in ("ações", "BOVA11"):
        for dr, nm in ((1, "CALL"), (-1, "PUT")):
            x = R.get((g, dr, "sinal")); b = R.get((g, dr, "qualquer dia"))
            if not x or len(x["r5"]) < 5: continue
            n = len(x["r5"])
            L.append(f"{nm} em {g}: {n} sinais")
            L.append(f"  sair no encolher do histograma ({fmt(avg(x['dias']), 0)} pregões): média {sg(avg(x['rs']))} · mediana {sg(med(x['rs']))} · positivos {fmt(100 * sum(1 for v in x['rs'] if v > 0) / n, 0)}%")
            L.append(f"  sair em 5 pregões: média {sg(avg(x['r5']))} · mediana {sg(med(x['r5']))} · positivos {fmt(100 * sum(1 for v in x['r5'] if v > 0) / n, 0)}%")
            L.append(f"  pico da opção até a saída: +20% em {fmt(100 * sum(1 for v in x['pico'] if v >= 20) / n, 0)}% · +50% em {fmt(100 * sum(1 for v in x['pico'] if v >= 50) / n, 0)}%")
            if b and len(b["r5"]) >= 30:
                L.append(f"  qualquer dia (5 pregões, {len(b['r5'])} casos): média {sg(avg(b['r5']))} · mediana {sg(med(b['r5']))} · positivos {fmt(100 * sum(1 for v in b['r5'] if v > 0) / len(b['r5']), 0)}%")
            L.append("")
    L.append("Estimativa: IV constante, sem skew, vol histórica; é ordem de grandeza. Pico usa a máxima do dia (teto).")
    txt = "\n".join(L)
    for i in range(0, len(txt), 3800):
        messenger.send(txt[i:i + 3800])



LISTA_DIARIA = ["BOVA11", "PETR4", "VALE3", "BBAS3", "ITUB4", "BBDC4"]

def job_bova_diario(manual=False):
    try:
        _bova_diario(manual)
    except Exception as e:
        log.exception("bova_diario falhou")
        messenger.send(f"Rotina diária linha+MACD falhou: {type(e).__name__}: {str(e)[:200]}")

def job_bova_manual():
    job_bova_diario(True)

def _bova_diario(manual=False):
    """Rotina diaria (depois do fechamento) no BOVA11 e em 5 acoes liquidas: CALL quando a linha vira para cima e o histograma do MACD cruza o zero (regra do Couto, grafico diario); depois avisa para sair quando o histograma comecar a encolher ou a linha virar para baixo."""
    api, t0 = Brapi(), now()
    estados = []
    for t in LISTA_DIARIA:
        try:
            estados.append(_linha_macd_ativo(api, t0, t, manual))
        except Exception as e:
            log.warning("linha+MACD %s: %s", t, e)
            estados.append(f"{t}: erro ({str(e)[:60]})")
    if manual:
        messenger.send("Linha + MACD (diário), estado de hoje:\n" + "\n".join(x for x in estados if x))

def _linha_macd_ativo(api, t0, t, manual):
    from messenger import fmt
    from types import SimpleNamespace as NS
    dl = api.history(t, "1y", "1d")
    if len(dl) < 80: return f"{t}: poucos candles"
    dia = lambda c: datetime.fromtimestamp(c["date"], TZ).date()
    if dia(dl[-1]) != t0.date():
        return f"{t}: sem o candle de hoje"
    cl = [b["close"] for b in dl]; N = len(dl)
    di = _supertrend_dir(dl); hi = _macd_hist(cl)
    ult = -99; sinal_hoje = False
    for i in range(60, N):
        fl = any(di[k] == 1 and di[k - 1] == -1 for k in range(i - 2, i + 1))
        cr = any(hi[k] > 0 >= hi[k - 1] for k in range(i - 2, i + 1))
        nasce = (di[i] == 1 and di[i - 1] == -1) or (hi[i] > 0 >= hi[i - 1])   # o sinal nasce no candle do ultimo dos dois eventos
        if fl and cr and nasce and i - ult >= 5:
            ult = i
            if i == N - 1: sinal_hoje = True
    hoje_s = str(t0.date())
    ka = "bova:aberta" if t == "BOVA11" else f"lm:aberta:{t}"      # BOVA11 mantem a chave original
    kc = "bova:call:" if t == "BOVA11" else f"lm:call:{t}:"
    aberta = database.get_meta(ka)
    if aberta and aberta < hoje_s:
        encolhe = hi[-1] < hi[-2]
        virou = di[-1] == -1
        dias = sum(1 for c in dl if aberta < str(dia(c)) <= hoje_s)
        if encolhe or virou or dias >= 30:
            motivo = "a linha virou para baixo" if virou else ("o histograma do MACD começou a encolher" if encolhe else "30 pregões sem saída")
            messenger.send(f"🔔 <b>{t} · hora de sair da CALL</b>\nDiário: {motivo}.\n{t} fechou a {fmt(cl[-1])} ({dias} pregões desde o sinal de {aberta[8:]}/{aberta[5:7]}).")
            database.set_meta(ka, "")
            aberta = ""
    if sinal_hoje and not database.get_meta(kc + hoje_s):
        sig = NS(ticker=t, direction="long")
        try:
            opt, err = options_selector.select(api, sig, t0.date())
        except BrapiError as e:
            opt, err = None, str(e)
        L = [f"🟢 <b>CALL · {t}</b>", "Diário: a linha virou para cima e o MACD cruzou", f"{t} fechou a {fmt(cl[-1])}", ""]
        if opt:
            L += [f"Opção: <code>{opt['symbol']}</code> · strike {fmt(opt['strike'])} · {messenger._dm(opt['expiration'])}",
                  f"Compra: até ~{fmt(opt['ask'])} (preço do fechamento, confira o ask)", f"Delta {fmt(abs(opt['delta']))} · spread {fmt(opt['spread_pct'], 0)}%"]
        else:
            L += [f"Sem opção líquida no filtro: {err}"]
        L += ["", "Aviso de saída: quando o histograma começar a encolher ou a linha virar para baixo."]
        messenger.send("\n".join(L))
        database.set_meta(kc + hoje_s, "1")
        database.set_meta(ka, hoje_s)
        return f"{t}: SINAL de call hoje"
    return f"{t}: linha {'para cima' if di[-1] == 1 else 'para baixo'}, histograma {'subindo' if hi[-1] > hi[-2] else 'caindo'}, call em aberto: {aberta or 'não'}"



def job_diag_intradia():
    try:
        _diag_intradia()
    except Exception as e:
        log.exception("diag_intradia falhou")
        messenger.send(f"Teste intradiário linha+MACD falhou: {type(e).__name__}: {str(e)[:200]}")

def _diag_intradia():
    """Comando 'intradia' (so leitura): regra linha + MACD nos candles intradiarios (5 min se a brapi entregar, senao 15) do BOVA11 e de PETR4/VALE3/BBAS3/ITUB4/BBDC4. Entrada no fechamento do candle do sinal (10:30 a 16:30), saida quando o histograma encolhe (1 ou 2 barras seguidas), a linha vira contra ou o dia acaba. Compara com entrar em candles aleatorios com a mesma saida."""
    from messenger import fmt
    api = Brapi()
    dados = {}; usado = None
    for iv_, rg in (("5m", "1mo"), ("5m", "5d"), ("15m", "1mo")):
        try:
            tmp = {}
            for t in LISTA_DIARIA:
                h = api.history(t, rg, iv_)
                if len(h) > 100: tmp[t] = h
            if tmp:
                dados, usado = tmp, f"{iv_} / {rg}"; break
        except Exception as e:
            log.warning("intradia %s %s: %s", iv_, rg, e)
    if not dados:
        messenger.send("Teste intradiário: a brapi não entregou candles intradiários."); return
    hm = lambda c: datetime.fromtimestamp(c["date"], TZ)
    R = {}
    def add(ch, **kw):
        x = R.setdefault(ch, dict(mov=[], mfe=[], dur=[], dia=set(), ref=[]))
        for k, v in kw.items():
            if k == "dia": x["dia"].add(v)
            else: x[k].append(v)
    ndias = set()
    for t, dl in dados.items():
        cl = [b["close"] for b in dl]; N = len(dl)
        di = _supertrend_dir(dl); hi = _macd_hist(cl)
        dias = [hm(b).date() for b in dl]
        ndias.update(dias)
        meio = sorted(set(dias))[len(set(dias)) // 2]
        ult = {1: -99, -1: -99}
        for i in range(40, N - 1):
            h = hm(dl[i])
            if not (10 <= h.hour + h.minute / 60 <= 16.5): continue
            for dr in (1, -1):
                fl = any(di[k] == dr and di[k - 1] == -dr for k in range(i - 2, i + 1))
                cr = any((hi[k] > 0 >= hi[k - 1]) if dr == 1 else (hi[k] < 0 <= hi[k - 1]) for k in range(i - 2, i + 1))
                nasce = (di[i] == dr and di[i - 1] == -dr) or ((hi[i] > 0 >= hi[i - 1]) if dr == 1 else (hi[i] < 0 <= hi[i - 1]))
                sinal = fl and cr and nasce and i - ult[dr] >= 5
                base = (i % 6 == 0) and dr == 1 or (i % 6 == 3 and dr == -1)
                if not sinal and not base: continue
                if sinal: ult[dr] = i
                for modo in (1, 2):
                    k = i + 1; fim = i
                    while k < N and dias[k] == dias[i]:
                        fim = k
                        enc = all((hi[k - j] < hi[k - j - 1]) if dr == 1 else (hi[k - j] > hi[k - j - 1]) for j in range(modo))
                        if enc or di[k] == -dr: break
                        k += 1
                    mov = dr * (cl[fim] / cl[i] - 1) * 100
                    mfe = max(dr * (((dl[j]["high"] if dr == 1 else dl[j]["low"]) / cl[i]) - 1) * 100 for j in range(i + 1, fim + 1)) if fim > i else 0.0
                    ch = ("sinal" if sinal else "qualquer candle", "BOVA11" if t == "BOVA11" else "ações", dr, modo)
                    add(ch, mov=mov, mfe=mfe, dur=(fim - i), dia=dias[i])
    min_c = 5 if usado.startswith("5m") else 15
    med = lambda v: sorted(v)[len(v) // 2] if v else 0.0
    avg = lambda v: sum(v) / len(v) if v else 0.0
    sg = lambda x, n=2: ("+" if x >= 0 else "−") + fmt(abs(x), n) + "%"
    L = [f"REGRA LINHA + MACD INTRADIÁRIA (candles de {usado}, {len(ndias)} pregões)", "Entrada no fechamento do candle do sinal (10:00 a 16:30). Saída: histograma encolhe, linha vira contra ou fim do dia. Movimento do ativo, sem custo.", "Para cobrir ~10% de spread na opção, o ativo precisa andar uns +0,5%.", ""]
    for g in ("BOVA11", "ações"):
        for dr, nm in ((1, "CALL"), (-1, "PUT")):
            for modo in (1, 2):
                x = R.get(("sinal", g, dr, modo)); b = R.get(("qualquer candle", g, dr, modo))
                if not x or len(x["mov"]) < 5: continue
                n = len(x["mov"]); nd = max(1, len(ndias))
                L.append(f"{nm} {g}, sair com {modo} barra{'s' if modo > 1 else ''} encolhendo: {n} sinais ({fmt(n / nd, 1)} por dia)")
                L.append(f"  ativo: média {sg(avg(x['mov']))} · mediana {sg(med(x['mov']))} · positivos {fmt(100 * sum(1 for v in x['mov'] if v > 0) / n, 0)}% · acima de +0,5%: {fmt(100 * sum(1 for v in x['mov'] if v >= 0.5) / n, 0)}%")
                L.append(f"  melhor ponto (pico): média {sg(avg(x['mfe']))} · duração média {fmt(avg(x['dur']) * min_c, 0)} min")
                if b and len(b["mov"]) >= 30:
                    L.append(f"  qualquer candle ({len(b['mov'])} casos): média {sg(avg(b['mov']))} · positivos {fmt(100 * sum(1 for v in b['mov'] if v > 0) / len(b['mov']), 0)}% · acima de +0,5%: {fmt(100 * sum(1 for v in b['mov'] if v >= 0.5) / len(b['mov']), 0)}%")
                L.append("")
    L.append("Só leitura. Não inclui o preço real da opção.")
    txt = "\n".join(L)
    for i in range(0, len(txt), 3800):
        messenger.send(txt[i:i + 3800])



# horarios (aprox., +-10 min) lidos nos prints do Couto em 12/11/2026; 1 = compra, -1 = venda
MARCAS_12_11 = {"BOVA11": [("12:30", 1), ("14:15", -1), ("17:40", 1)],
                "PETR4": [("10:40", 1), ("11:30", -1), ("12:13", 1), ("17:31", -1)],
                "SUZB3": [("12:40", -1), ("15:47", 1)]}

def job_diag_calibra():
    try:
        _diag_calibra()
    except Exception as e:
        log.exception("diag_calibra falhou")
        messenger.send(f"Calibração da linha e do MACD falhou: {type(e).__name__}: {str(e)[:200]}")

def _diag_calibra():
    """Comando 'calibra' (so leitura): testa varios parametros da linha (periodo x fator) e do MACD e mostra quais reproduzem as compras/vendas que o Couto marcou nos prints de 12/11 (BOVA11, PETR4, SUZB3)."""
    from messenger import fmt
    api = Brapi()
    dados = {}; usado = None
    for iv_, rg in (("5m", "5d"), ("5m", "1mo"), ("15m", "1mo")):
        try:
            tmp = {t: api.history(t, rg, iv_) for t in MARCAS_12_11}
            if all(len(v) > 100 for v in tmp.values()):
                dados, usado = tmp, iv_; break
        except Exception as e:
            log.warning("calibra %s %s: %s", iv_, rg, e)
    if not dados:
        messenger.send("Calibração: a brapi não entregou candles intradiários."); return
    tol = 12 if usado == "5m" else 20
    hm = lambda c: datetime.fromtimestamp(c["date"], TZ)
    alvo = date(2026, 11, 12)
    minutos = lambda hhmm: int(hhmm[:2]) * 60 + int(hhmm[3:])
    def eventos(dl, serie, tipo):
        """lista (minuto, direcao) em 12/11 entre 10:30 e 17:45"""
        out = []
        for i in range(1, len(dl)):
            h = hm(dl[i])
            if h.date() != alvo: continue
            m = h.hour * 60 + h.minute
            if not (10 * 60 + 30 <= m <= 17 * 60 + 45): continue
            if tipo == "linha":
                if serie[i] != serie[i - 1]: out.append((m, serie[i]))
            else:
                if serie[i] > 0 >= serie[i - 1]: out.append((m, 1))
                elif serie[i] < 0 <= serie[i - 1]: out.append((m, -1))
        return out
    def casa(ev, marcas, tol_):
        usados = set(); ach = 0
        for hhmm, d in marcas:
            mm = minutos(hhmm); best = None
            for k, (m, dd) in enumerate(ev):
                if dd == d and k not in usados and abs(m - mm) <= tol_ and (best is None or abs(m - mm) < abs(ev[best][0] - mm)): best = k
            if best is not None: usados.add(best); ach += 1
        return ach, len(ev) - len(usados)
    total = sum(len(v) for v in MARCAS_12_11.values())
    linhas = []
    for per in (7, 10, 14, 20, 34, 50):
        for mult in (1.0, 1.5, 2.0, 2.5, 3.0, 3.5):
            ach = ext = 0; det = []
            for t, dl in dados.items():
                ev = eventos(dl, _supertrend_dir(dl, per, mult), "linha")
                a, e = casa(ev, MARCAS_12_11[t], tol)
                ach += a; ext += e; det.append(f"{t} {a}/{len(MARCAS_12_11[t])}+{e}")
            linhas.append((ach * 2 - ext, per, mult, ach, ext, det))
    linhas.sort(key=lambda x: (-x[0], x[1]))
    macds = []
    for a_, b_, c_ in ((12, 26, 9), (14, 34, 9), (14, 34, 5), (14, 34, 14), (8, 21, 5), (5, 34, 5), (12, 34, 9), (10, 34, 10)):
        ach = ext = 0
        for t, dl in dados.items():
            ev = eventos(dl, _macd_hist([b["close"] for b in dl], a_, b_, c_), "macd")
            a, e = casa(ev, MARCAS_12_11[t], tol + 8)
            ach += a; ext += e
        macds.append((ach * 2 - ext * 0.5, a_, b_, c_, ach, ext))
    macds.sort(key=lambda x: -x[0])
    L = [f"CALIBRAÇÃO (candles de {usado}, 12/11; {total} marcas lidas nos seus prints, tolerância ±{tol} min)", "", "LINHA (período, fator): marcas achadas / extras (viradas que você não marcou)"]
    for sc, per, mult, ach, ext, det in linhas[:6]:
        L.append(f"  {per}, {fmt(mult, 1)}: {ach}/{total} achadas · {ext} extras ({'; '.join(det)})")
    padrao = next(x for x in linhas if x[1] == 10 and x[2] == 3.0)
    L.append(f"  (a que eu usava, 10 e 3,0: {padrao[3]}/{total} achadas · {padrao[4]} extras)")
    L += ["", "MACD (rápida, lenta, sinal): cruzamentos do histograma perto das marcas"]
    for sc, a_, b_, c_, ach, ext in macds[:5]:
        L.append(f"  {a_}, {b_}, {c_}: {ach}/{total} achadas · {ext} cruzamentos extras")
    L += ["", "Os horários das marcas são aproximados (±10 min, lidos do eixo dos prints). Só leitura."]
    messenger.send("\n".join(L))



def _dir_halftrend(dl, amp=4):
    """HalfTrend (Everget): 1 = alta (compra), -1 = baixa (venda). So a amplitude decide as viradas; o desvio do canal so afeta as bandas."""
    n = len(dl); trend = 0; nxt = 0
    max_low = dl[0]["low"]; min_high = dl[0]["high"]; out = []
    for i in range(n):
        j = max(0, i - amp + 1)
        hi_p = max(b["high"] for b in dl[j:i + 1]); lo_p = min(b["low"] for b in dl[j:i + 1])
        hi_ma = sum(b["high"] for b in dl[j:i + 1]) / (i + 1 - j); lo_ma = sum(b["low"] for b in dl[j:i + 1]) / (i + 1 - j)
        low1 = dl[i - 1]["low"] if i else dl[i]["low"]; high1 = dl[i - 1]["high"] if i else dl[i]["high"]
        if nxt == 1:
            max_low = max(lo_p, max_low)
            if hi_ma < max_low and dl[i]["close"] < low1:
                trend = 1; nxt = 0; min_high = hi_p
        else:
            min_high = min(hi_p, min_high)
            if lo_ma > min_high and dl[i]["close"] > high1:
                trend = 0; nxt = 1; max_low = lo_p
        out.append(1 if trend == 0 else -1)
    return out

def _dir_donchian(dl, n):
    d = [1] * len(dl); cur = 1
    for i in range(len(dl)):
        if i >= n:
            hh = max(b["high"] for b in dl[i - n:i]); ll = min(b["low"] for b in dl[i - n:i])
            if dl[i]["close"] > hh: cur = 1
            elif dl[i]["close"] < ll: cur = -1
        d[i] = cur
    return d

def _dir_hilo(dl, n):
    d = [1] * len(dl); cur = 1
    for i in range(len(dl)):
        if i >= n:
            hi = sum(b["high"] for b in dl[i - n:i]) / n; lo = sum(b["low"] for b in dl[i - n:i]) / n
            if dl[i]["close"] > hi: cur = 1
            elif dl[i]["close"] < lo: cur = -1
        d[i] = cur
    return d

def _dir_chandelier(dl, n, k):
    N = len(dl)
    tr = [dl[0]["high"] - dl[0]["low"]] + [max(dl[i]["high"] - dl[i]["low"], abs(dl[i]["high"] - dl[i - 1]["close"]), abs(dl[i]["low"] - dl[i - 1]["close"])) for i in range(1, N)]
    atr = [0.0] * N
    for i in range(N):
        atr[i] = sum(tr[:i + 1]) / (i + 1) if i < n else (atr[i - 1] * (n - 1) + tr[i]) / n
    d = [1] * N; cur = 1
    for i in range(N):
        if i > n:
            j = i - 1
            ls = max(b["high"] for b in dl[j - n + 1:j + 1]) - k * atr[j]
            ss = min(b["low"] for b in dl[j - n + 1:j + 1]) + k * atr[j]
            if cur == 1 and dl[i]["close"] < ls: cur = -1
            elif cur == -1 and dl[i]["close"] > ss: cur = 1
        d[i] = cur
    return d

def job_diag_calibra2():
    try:
        _diag_calibra2()
    except Exception as e:
        log.exception("diag_calibra2 falhou")
        messenger.send(f"Calibração 2 falhou: {type(e).__name__}: {str(e)[:200]}")

def _diag_calibra2():
    """Comando 'calibra2' (so leitura): outras familias de linha de tendencia (Donchian, HiLo, Chandelier, SuperTrend com fatores altos) contra as 9 marcas do Couto em 12/11."""
    from messenger import fmt
    api = Brapi()
    dados = {}; usado = None
    for iv_, rg in (("5m", "5d"), ("5m", "1mo"), ("15m", "1mo")):
        try:
            tmp = {t: api.history(t, rg, iv_) for t in MARCAS_12_11}
            if all(len(v) > 100 for v in tmp.values()):
                dados, usado = tmp, iv_; break
        except Exception as e:
            log.warning("calibra2 %s %s: %s", iv_, rg, e)
    if not dados:
        messenger.send("Calibração 2: a brapi não entregou candles intradiários."); return
    tol = 12 if usado == "5m" else 20
    hm = lambda c: datetime.fromtimestamp(c["date"], TZ)
    alvo = date(2026, 11, 12)
    minutos = lambda hhmm: int(hhmm[:2]) * 60 + int(hhmm[3:])
    def eventos(dl, d):
        out = []
        for i in range(1, len(dl)):
            h = hm(dl[i])
            if h.date() != alvo: continue
            m = h.hour * 60 + h.minute
            if 10 * 60 + 30 <= m <= 17 * 60 + 45 and d[i] != d[i - 1]: out.append((m, d[i]))
        return out
    def casa(ev, marcas):
        usados = set(); ach = 0; erros = []
        for hhmm, dr in marcas:
            mm = minutos(hhmm); best = None
            for k, (m, dd) in enumerate(ev):
                if dd == dr and k not in usados and abs(m - mm) <= tol and (best is None or abs(m - mm) < abs(ev[best][0] - mm)): best = k
            if best is not None: usados.add(best); ach += 1; erros.append(abs(ev[best][0] - mm))
        return ach, len(ev) - len(usados), erros
    total = sum(len(v) for v in MARCAS_12_11.values())
    cand = []
    for n in (10, 14, 20, 34, 50): cand.append((f"Donchian {n}", lambda dl, n=n: _dir_donchian(dl, n)))
    for n in (10, 14, 20, 34, 50): cand.append((f"HiLo {n}", lambda dl, n=n: _dir_hilo(dl, n)))
    for n in (14, 22, 34):
        for k in (1.5, 2.0, 3.0): cand.append((f"Chandelier {n} x{fmt(k, 1)}", lambda dl, n=n, k=k: _dir_chandelier(dl, n, k)))
    for mult in (4.0, 5.0, 6.0): cand.append((f"SuperTrend 34 x{fmt(mult, 1)}", lambda dl, m=mult: _supertrend_dir(dl, 34, m)))
    for amp in (2, 3, 4, 5, 6, 8): cand.append((f"HalfTrend amplitude {amp}", lambda dl, a=amp: _dir_halftrend(dl, a)))
    res = []
    for nome, fn in cand:
        ach = ext = 0; det = []; erro_tot = []
        for t, dl in dados.items():
            a, e, er = casa(eventos(dl, fn(dl)), MARCAS_12_11[t])
            ach += a; ext += e; erro_tot += er; det.append(f"{t} {a}/{len(MARCAS_12_11[t])}+{e}")
        res.append((ach * 2 - ext, nome, ach, ext, det, (sum(erro_tot) / len(erro_tot) if erro_tot else 0)))
    res.sort(key=lambda x: -x[0])
    L = [f"CALIBRAÇÃO 2 (candles de {usado}, 12/11; {total} marcas, tolerância ±{tol} min)", "Marcas achadas / viradas extras (viradas que o seu gráfico não mostra)", ""]
    top = res[:8]
    ht4 = next((x for x in res if x[1] == "HalfTrend amplitude 4"), None)
    if ht4 and ht4 not in top: top = top[:7] + [ht4]
    for sc, nome, ach, ext, det, er in top:
        L.append(f"{nome}: {ach}/{total} achadas · {ext} extras · erro médio {fmt(er, 0)} min ({'; '.join(det)})")
    L += ["", "Só leitura. Horários das marcas aproximados."]
    messenger.send("\n".join(L))



def _dir_trail(dl, a, k):
    """Linha propria (tipo HalfTrend): em alta a linha sobe com a menor minima das ultimas 'a' velas e so desce de lado; vira quando o fechamento passa k ATR abaixo dela (e o contrario na baixa)."""
    N = len(dl)
    tr = [dl[0]["high"] - dl[0]["low"]] + [max(dl[i]["high"] - dl[i]["low"], abs(dl[i]["high"] - dl[i - 1]["close"]), abs(dl[i]["low"] - dl[i - 1]["close"])) for i in range(1, N)]
    atr = [0.0] * N
    for i in range(N):
        atr[i] = sum(tr[:i + 1]) / (i + 1) if i < 14 else (atr[i - 1] * 13 + tr[i]) / 14
    d = [1] * N; cur = 1; line = dl[0]["low"]
    for i in range(1, N):
        j = max(0, i - a)
        ll = min(b["low"] for b in dl[j:i]); hh = max(b["high"] for b in dl[j:i])
        if cur == 1:
            line = max(line, ll)
            if dl[i]["close"] < line - k * atr[i - 1]:
                cur = -1; line = hh
        else:
            line = min(line, hh)
            if dl[i]["close"] > line + k * atr[i - 1]:
                cur = 1; line = ll
        d[i] = cur
    return d

def job_diag_calibra3():
    try:
        _diag_calibra3()
    except Exception as e:
        log.exception("diag_calibra3 falhou")
        messenger.send(f"Calibração 3 falhou: {type(e).__name__}: {str(e)[:200]}")

def _diag_calibra3():
    """Comando 'calibra3' (so leitura): linha propria (janela x folga em ATR) contra as 9 marcas do Couto em 12/11."""
    from messenger import fmt
    api = Brapi()
    dados = {}; usado = None
    for iv_, rg in (("5m", "5d"), ("5m", "1mo"), ("15m", "1mo")):
        try:
            tmp = {t: api.history(t, rg, iv_) for t in MARCAS_12_11}
            if all(len(v) > 100 for v in tmp.values()):
                dados, usado = tmp, iv_; break
        except Exception as e:
            log.warning("calibra3 %s %s: %s", iv_, rg, e)
    if not dados:
        messenger.send("Calibração 3: a brapi não entregou candles intradiários."); return
    tol = 12 if usado == "5m" else 20
    hm = lambda c: datetime.fromtimestamp(c["date"], TZ)
    alvo = date(2026, 11, 12)
    minutos = lambda hhmm: int(hhmm[:2]) * 60 + int(hhmm[3:])
    def eventos(dl, d):
        out = []
        for i in range(1, len(dl)):
            h = hm(dl[i])
            if h.date() != alvo: continue
            m = h.hour * 60 + h.minute
            if 10 * 60 + 30 <= m <= 17 * 60 + 45 and d[i] != d[i - 1]: out.append((m, d[i]))
        return out
    def casa(ev, marcas):
        usados = set(); ach = 0; erros = []
        for hhmm, dr in marcas:
            mm = minutos(hhmm); best = None
            for k, (m, dd) in enumerate(ev):
                if dd == dr and k not in usados and abs(m - mm) <= tol and (best is None or abs(m - mm) < abs(ev[best][0] - mm)): best = k
            if best is not None: usados.add(best); ach += 1; erros.append(abs(ev[best][0] - mm))
        return ach, len(ev) - len(usados), erros
    total = sum(len(v) for v in MARCAS_12_11.values())
    res = []
    for a in range(2, 13):
        for k in (0.0, 0.5, 1.0, 1.5, 2.0, 3.0):
            ach = ext = 0; det = []; er_t = []
            for t, dl in dados.items():
                x, e, er = casa(eventos(dl, _dir_trail(dl, a, k)), MARCAS_12_11[t])
                ach += x; ext += e; er_t += er; det.append(f"{t} {x}/{len(MARCAS_12_11[t])}+{e}")
            res.append((ach * 2 - ext, a, k, ach, ext, det, (sum(er_t) / len(er_t) if er_t else 0)))
    res.sort(key=lambda x: (-x[0], x[1]))
    L = [f"LINHA PRÓPRIA (candles de {usado}, 12/11; {total} marcas, ±{tol} min)", "janela (velas) · folga em ATR: marcas achadas / viradas extras", ""]
    for sc, a, k, ach, ext, det, er in res[:8]:
        L.append(f"{a} velas, {fmt(k, 1)} ATR: {ach}/{total} achadas · {ext} extras · erro {fmt(er, 0)} min ({'; '.join(det)})")
    L += ["", "Só leitura. Com só 9 marcas o ajuste pode ser coincidência; serve de ponto de partida."]
    messenger.send("\n".join(L))



MARCAS_11_11 = {"PETR4": [("11:55", 1), ("15:05", -1)]}

def job_diag_valida_linha():
    try:
        _diag_valida_linha()
    except Exception as e:
        log.exception("diag_valida_linha falhou")
        messenger.send(f"Validação da linha falhou: {type(e).__name__}: {str(e)[:200]}")

def _diag_valida_linha():
    """Comando 'validalinha' (so leitura): as linhas candidatas contra as marcas do Couto em 11/11 (PETR4), dia que nao foi usado para ajustar."""
    from messenger import fmt
    api = Brapi()
    dados = {}; usado = None
    for iv_, rg in (("5m", "5d"), ("5m", "1mo"), ("15m", "1mo")):
        try:
            tmp = {t: api.history(t, rg, iv_) for t in MARCAS_11_11}
            if all(len(v) > 100 for v in tmp.values()):
                dados, usado = tmp, iv_; break
        except Exception as e:
            log.warning("validalinha %s %s: %s", iv_, rg, e)
    if not dados:
        messenger.send("Validação: a brapi não entregou candles de 11/11 (o histórico intradiário vai até 7 dias)."); return
    tol = 12 if usado == "5m" else 20
    hm = lambda c: datetime.fromtimestamp(c["date"], TZ)
    alvo = date(2026, 11, 11)
    minutos = lambda hhmm: int(hhmm[:2]) * 60 + int(hhmm[3:])
    def eventos(dl, d):
        out = []
        for i in range(1, len(dl)):
            h = hm(dl[i])
            if h.date() != alvo: continue
            m = h.hour * 60 + h.minute
            if 10 * 60 + 30 <= m <= 17 * 60 + 45 and d[i] != d[i - 1]: out.append((m, d[i]))
        return out
    fm = lambda m: f"{m // 60:02d}:{m % 60:02d}"
    cands = [("Própria 6 velas, 0,5 ATR", lambda dl: _dir_trail(dl, 6, 0.5)), ("Própria 7 velas, 0,5 ATR", lambda dl: _dir_trail(dl, 7, 0.5)),
             ("Própria 10 velas, 0 ATR", lambda dl: _dir_trail(dl, 10, 0.0)), ("Própria 8 velas, 0 ATR", lambda dl: _dir_trail(dl, 8, 0.0)),
             ("Própria 5 velas, 1,5 ATR", lambda dl: _dir_trail(dl, 5, 1.5)), ("SuperTrend 10 x3 (a dos alertas diários)", lambda dl: _supertrend_dir(dl, 10, 3.0)),
             ("HalfTrend amplitude 2", lambda dl: _dir_halftrend(dl, 2)), ("HalfTrend amplitude 4", lambda dl: _dir_halftrend(dl, 4)),
             ("Donchian 10", lambda dl: _dir_donchian(dl, 10)), ("HiLo 20", lambda dl: _dir_hilo(dl, 20))]
    L = [f"VALIDAÇÃO DA LINHA (PETR4, 11/11, candles de {usado}). Suas marcas: " + ", ".join(f"{h} {'compra' if d == 1 else 'venda'}" for h, d in MARCAS_11_11["PETR4"]) + f" (±{tol} min)", ""]
    for nome, fn in cands:
        dl = dados["PETR4"]; ev = eventos(dl, fn(dl))
        usados = set(); ach = 0
        for hhmm, dr in MARCAS_11_11["PETR4"]:
            mm = minutos(hhmm); best = None
            for k, (m, dd) in enumerate(ev):
                if dd == dr and k not in usados and abs(m - mm) <= tol and (best is None or abs(m - mm) < abs(ev[best][0] - mm)): best = k
            if best is not None: usados.add(best); ach += 1
        L.append(f"{nome}: {ach}/2 achadas · {len(ev) - len(usados)} extras · viradas: " + (" ".join(f"{fm(m)}{'▲' if d == 1 else '▼'}" for m, d in ev) or "nenhuma"))
    L += ["", "▲ = linha virou para alta (compra), ▼ = baixa (venda). Só leitura."]
    messenger.send("\n".join(L))



def job_diag_intradia2():
    try:
        _diag_intradia2()
    except Exception as e:
        log.exception("diag_intradia2 falhou")
        messenger.send(f"Teste intradiário 2 falhou: {type(e).__name__}: {str(e)[:200]}")

def _diag_intradia2():
    """Comando 'intradia2' (so leitura): regra linha propria (6 velas, 0,5 ATR) + cruzamento do MACD (14-34-14) nos candles intradiarios do BOVA11 e das 5 acoes. Saidas: linha vira contra / histograma encolhe 2 barras / 1 barra. Compara com entrar em candles aleatorios com a mesma saida."""
    from messenger import fmt
    api = Brapi()
    dados = {}; usado = None
    for iv_, rg in (("5m", "1mo"), ("5m", "5d"), ("15m", "1mo")):
        try:
            tmp = {}
            for t in LISTA_DIARIA:
                h = api.history(t, rg, iv_)
                if len(h) > 100: tmp[t] = h
            if tmp:
                dados, usado = tmp, f"{iv_} / {rg}"; break
        except Exception as e:
            log.warning("intradia2 %s %s: %s", iv_, rg, e)
    if not dados:
        messenger.send("Teste intradiário 2: a brapi não entregou candles intradiários."); return
    hm = lambda c: datetime.fromtimestamp(c["date"], TZ)
    R = {}; ndias = set()
    def add(ch, mov, mfe, dur, dia):
        x = R.setdefault(ch, dict(mov=[], mfe=[], dur=[], dia=[]))
        x["mov"].append(mov); x["mfe"].append(mfe); x["dur"].append(dur); x["dia"].append(dia)
    for t, dl in dados.items():
        cl = [b["close"] for b in dl]; N = len(dl)
        di = _dir_trail(dl, 6, 0.5); hi = _macd_hist(cl, 14, 34, 14)
        dias = [hm(b).date() for b in dl]; ndias.update(dias)
        ult = {1: -99, -1: -99}
        for i in range(40, N - 1):
            h = hm(dl[i])
            if not (10 <= h.hour + h.minute / 60 <= 16.5): continue
            for dr in (1, -1):
                fl = any(di[k] == dr and di[k - 1] == -dr for k in range(i - 2, i + 1))
                cr = any((hi[k] > 0 >= hi[k - 1]) if dr == 1 else (hi[k] < 0 <= hi[k - 1]) for k in range(i - 2, i + 1))
                nasce = (di[i] == dr and di[i - 1] == -dr) or ((hi[i] > 0 >= hi[i - 1]) if dr == 1 else (hi[i] < 0 <= hi[i - 1]))
                sinal = fl and cr and nasce and i - ult[dr] >= 5
                base = (i % 6 == 0) if dr == 1 else (i % 6 == 3)
                if not sinal and not base: continue
                if sinal: ult[dr] = i
                for modo in ("linha", 2, 1):
                    k = i + 1; fim = i
                    while k < N and dias[k] == dias[i]:
                        fim = k
                        if di[k] == -dr: break
                        if modo != "linha" and all((hi[k - j] < hi[k - j - 1]) if dr == 1 else (hi[k - j] > hi[k - j - 1]) for j in range(modo)): break
                        k += 1
                    mov = dr * (cl[fim] / cl[i] - 1) * 100
                    mfe = max(dr * (((dl[j]["high"] if dr == 1 else dl[j]["low"]) / cl[i]) - 1) * 100 for j in range(i + 1, fim + 1)) if fim > i else 0.0
                    add(("sinal" if sinal else "base", "BOVA11" if t == "BOVA11" else "ações", dr, modo), mov, mfe, fim - i, dias[i])
    min_c = 15 if usado.startswith("15m") else 5
    med = lambda v: sorted(v)[len(v) // 2] if v else 0.0
    avg = lambda v: sum(v) / len(v) if v else 0.0
    sg = lambda x: ("+" if x >= 0 else "−") + fmt(abs(x), 2) + "%"
    pc = lambda v, f: fmt(100 * sum(1 for a in v if f(a)) / len(v), 0) + "%" if v else "-"
    nd = max(1, len(ndias)); metade = sorted(ndias)[len(ndias) // 2]
    L = [f"LINHA PRÓPRIA + MACD INTRADIÁRIO (candles {usado}, {len(ndias)} pregões)", "Linha de 6 velas e 0,5 ATR, MACD 14-34-14. Entrada no fechamento do candle do sinal (10:00 a 16:30). Movimento do ativo, sem custo. Cobrir ~10% de spread pede uns +0,5%.", ""]
    nomes = {"linha": "sair quando a linha vira contra", 2: "sair com 2 barras encolhendo", 1: "sair com 1 barra encolhendo"}
    for g in ("BOVA11", "ações"):
        for dr, nm in ((1, "CALL"), (-1, "PUT")):
            for modo in ("linha", 2, 1):
                x = R.get(("sinal", g, dr, modo)); b = R.get(("base", g, dr, modo))
                if not x or len(x["mov"]) < 5: continue
                n = len(x["mov"])
                p1 = [m for m, d in zip(x["mov"], x["dia"]) if d < metade]; p2 = [m for m, d in zip(x["mov"], x["dia"]) if d >= metade]
                L.append(f"{nm} {g}, {nomes[modo]}: {n} sinais ({fmt(n / nd, 1)}/dia)")
                L.append(f"  média {sg(avg(x['mov']))} · mediana {sg(med(x['mov']))} · positivos {pc(x['mov'], lambda a: a > 0)} · acima de +0,5%: {pc(x['mov'], lambda a: a >= 0.5)} · pico médio {sg(avg(x['mfe']))} · {fmt(avg(x['dur']) * min_c, 0)} min")
                L.append(f"  metades: 1ª {sg(avg(p1))} ({len(p1)}) · 2ª {sg(avg(p2))} ({len(p2)})")
                if b and len(b["mov"]) >= 30:
                    L.append(f"  qualquer candle ({len(b['mov'])}): média {sg(avg(b['mov']))} · positivos {pc(b['mov'], lambda a: a > 0)} · acima de +0,5%: {pc(b['mov'], lambda a: a >= 0.5)}")
            L.append("")
    L.append("Só leitura. Sem o preço real da opção.")
    txt = "\n".join(L)
    for i in range(0, len(txt), 3800):
        messenger.send(txt[i:i + 3800])



def job_diag_lateral():
    try:
        _diag_lateral()
    except Exception as e:
        log.exception("diag_lateral falhou")
        messenger.send(f"Teste do lateral falhou: {type(e).__name__}: {str(e)[:200]}")

def _diag_lateral():
    """Comando 'lateral' (so leitura): sinais da linha propria (6 velas, 0,5 ATR) + MACD 14-34-14, separados pela 'energia' das barras do histograma nas 24 velas antes do sinal (tamanho medio da barra / ATR) e pela simetria (quantas barras positivas x negativas)."""
    from messenger import fmt
    api = Brapi()
    dados = {}; usado = None
    for iv_, rg in (("5m", "1mo"), ("5m", "5d"), ("15m", "1mo")):
        try:
            tmp = {}
            for t in LISTA_DIARIA:
                h = api.history(t, rg, iv_)
                if len(h) > 100: tmp[t] = h
            if tmp:
                dados, usado = tmp, f"{iv_} / {rg}"; break
        except Exception as e:
            log.warning("lateral %s %s: %s", iv_, rg, e)
    if not dados:
        messenger.send("Teste do lateral: a brapi não entregou candles intradiários."); return
    hm = lambda c: datetime.fromtimestamp(c["date"], TZ)
    LJ = 24; sinais = []
    for t, dl in dados.items():
        cl = [b["close"] for b in dl]; N = len(dl)
        di = _dir_trail(dl, 6, 0.5); hi = _macd_hist(cl, 14, 34, 14)
        tr = [dl[0]["high"] - dl[0]["low"]] + [max(dl[i]["high"] - dl[i]["low"], abs(dl[i]["high"] - dl[i - 1]["close"]), abs(dl[i]["low"] - dl[i - 1]["close"])) for i in range(1, N)]
        atr = [0.0] * N
        for i in range(N):
            atr[i] = sum(tr[:i + 1]) / (i + 1) if i < 14 else (atr[i - 1] * 13 + tr[i]) / 14
        dias = [hm(b).date() for b in dl]
        ult = {1: -99, -1: -99}
        for i in range(60, N - 1):
            h = hm(dl[i])
            if not (10 <= h.hour + h.minute / 60 <= 16.5): continue
            for dr in (1, -1):
                fl = any(di[k] == dr and di[k - 1] == -dr for k in range(i - 2, i + 1))
                cr = any((hi[k] > 0 >= hi[k - 1]) if dr == 1 else (hi[k] < 0 <= hi[k - 1]) for k in range(i - 2, i + 1))
                nasce = (di[i] == dr and di[i - 1] == -dr) or ((hi[i] > 0 >= hi[i - 1]) if dr == 1 else (hi[i] < 0 <= hi[i - 1]))
                if not (fl and cr and nasce and i - ult[dr] >= 5): continue
                ult[dr] = i
                jan = hi[i - 2 - LJ:i - 2]
                energia = (sum(abs(x) for x in jan) / LJ) / atr[i] if atr[i] else 0
                simet = abs(sum(1 for x in jan if x > 0) / LJ - 0.5)
                res = {}
                for modo in ("linha", 2):
                    k = i + 1; fim = i
                    while k < N and dias[k] == dias[i]:
                        fim = k
                        if di[k] == -dr: break
                        if modo != "linha" and all((hi[k - j] < hi[k - j - 1]) if dr == 1 else (hi[k - j] > hi[k - j - 1]) for j in range(modo)): break
                        k += 1
                    res[modo] = dr * (cl[fim] / cl[i] - 1) * 100
                sinais.append(dict(energia=energia, simet=simet, linha=res["linha"], b2=res[2]))
    if len(sinais) < 40:
        messenger.send(f"Teste do lateral: só {len(sinais)} sinais, pouco para separar em faixas."); return
    sinais.sort(key=lambda x: x["energia"])
    q = len(sinais) // 4
    faixas = [sinais[:q], sinais[q:2 * q], sinais[2 * q:3 * q], sinais[3 * q:]]
    med = lambda v: sorted(v)[len(v) // 2] if v else 0.0
    avg = lambda v: sum(v) / len(v) if v else 0.0
    sg = lambda x: ("+" if x >= 0 else "−") + fmt(abs(x), 2) + "%"
    pc = lambda v, f: (fmt(100 * sum(1 for a in v if f(a)) / len(v), 0) + "%") if v else "-"
    def linha(nome, grp):
        a = [x["linha"] for x in grp]; b = [x["b2"] for x in grp]
        return [f"{nome}: {len(grp)} sinais", f"  sair na virada da linha: média {sg(avg(a))} · mediana {sg(med(a))} · positivos {pc(a, lambda v: v > 0)} · +0,5%: {pc(a, lambda v: v >= 0.5)}",
                f"  sair com 2 barras encolhendo: média {sg(avg(b))} · mediana {sg(med(b))} · positivos {pc(b, lambda v: v > 0)} · +0,5%: {pc(b, lambda v: v >= 0.5)}"]
    L = [f"TESTE DO LATERAL (candles {usado}; BOVA11 + 5 ações, calls e puts juntos; {len(sinais)} sinais)", f"Energia = tamanho médio das barras do histograma nas {LJ} velas antes do sinal, em ATRs. Faixa 1 = mais lateral.", ""]
    nomes = ["Faixa 1 (mais lateral)", "Faixa 2", "Faixa 3", "Faixa 4 (menos lateral)"]
    for n_, g in zip(nomes, faixas):
        L += linha(f"{n_}, energia até {fmt(max(x['energia'] for x in g), 2)}", g)
    L += ["", "Simetria das barras (positivas x negativas) antes do sinal:"]
    sim = [x for x in sinais if x["simet"] <= 0.15]; nao = [x for x in sinais if x["simet"] > 0.15]
    L += linha("Quase simétricas (perto de metade e metade)", sim) + linha("Um lado domina", nao)
    L += ["", "Só leitura. Movimento do ativo, sem custo; cobrir o spread da opção pede uns +0,5%."]
    messenger.send("\n".join(L))



def job_diag_lateral2():
    try:
        _diag_lateral2()
    except Exception as e:
        log.exception("diag_lateral2 falhou")
        messenger.send(f"Teste do lateral 2 falhou: {type(e).__name__}: {str(e)[:200]}")

def _diag_lateral2():
    """Comando 'lateral2' (so leitura): robustez do corte por simetria das barras do histograma (limiar, janela, metades do periodo, BOVA11 x acoes, calls x puts)."""
    from messenger import fmt
    api = Brapi()
    dados = {}; usado = None
    for iv_, rg in (("5m", "1mo"), ("5m", "5d"), ("15m", "1mo")):
        try:
            tmp = {}
            for t in LISTA_DIARIA:
                h = api.history(t, rg, iv_)
                if len(h) > 100: tmp[t] = h
            if tmp:
                dados, usado = tmp, f"{iv_} / {rg}"; break
        except Exception as e:
            log.warning("lateral2 %s %s: %s", iv_, rg, e)
    if not dados:
        messenger.send("Teste do lateral 2: a brapi não entregou candles intradiários."); return
    hm = lambda c: datetime.fromtimestamp(c["date"], TZ)
    JANELAS = (12, 24, 36); sinais = []
    for t, dl in dados.items():
        cl = [b["close"] for b in dl]; N = len(dl)
        di = _dir_trail(dl, 6, 0.5); hi = _macd_hist(cl, 14, 34, 14)
        dias = [hm(b).date() for b in dl]
        ult = {1: -99, -1: -99}
        for i in range(60, N - 1):
            h = hm(dl[i])
            if not (10 <= h.hour + h.minute / 60 <= 16.5): continue
            for dr in (1, -1):
                fl = any(di[k] == dr and di[k - 1] == -dr for k in range(i - 2, i + 1))
                cr = any((hi[k] > 0 >= hi[k - 1]) if dr == 1 else (hi[k] < 0 <= hi[k - 1]) for k in range(i - 2, i + 1))
                nasce = (di[i] == dr and di[i - 1] == -dr) or ((hi[i] > 0 >= hi[i - 1]) if dr == 1 else (hi[i] < 0 <= hi[i - 1]))
                if not (fl and cr and nasce and i - ult[dr] >= 5): continue
                ult[dr] = i
                simet = {}
                for lj in JANELAS:
                    jan = hi[i - 2 - lj:i - 2]
                    simet[lj] = abs(sum(1 for x in jan if x > 0) / lj - 0.5)
                res = {}
                for modo in ("linha", 2):
                    k = i + 1; fim = i
                    while k < N and dias[k] == dias[i]:
                        fim = k
                        if di[k] == -dr: break
                        if modo != "linha" and all((hi[k - j] < hi[k - j - 1]) if dr == 1 else (hi[k - j] > hi[k - j - 1]) for j in range(modo)): break
                        k += 1
                    res[modo] = dr * (cl[fim] / cl[i] - 1) * 100
                sinais.append(dict(simet=simet, linha=res["linha"], b2=res[2], dia=dias[i], g="BOVA11" if t == "BOVA11" else "ações", dr=dr))
    if len(sinais) < 40:
        messenger.send(f"Teste do lateral 2: só {len(sinais)} sinais."); return
    dias_u = sorted({x["dia"] for x in sinais}); metade = dias_u[len(dias_u) // 2]
    avg = lambda v: sum(v) / len(v) if v else 0.0
    sg = lambda x: ("+" if x >= 0 else "−") + fmt(abs(x), 2) + "%"
    pc = lambda v, f: (fmt(100 * sum(1 for a in v if f(a)) / len(v), 0) + "%") if v else "-"
    def lin(rot, grp):
        if len(grp) < 5: return f"{rot}: {len(grp)} sinais (pouco)"
        a = [x["linha"] for x in grp]; b = [x["b2"] for x in grp]
        return f"{rot}: {len(grp)} sinais · virada da linha {sg(avg(a))} ({pc(a, lambda v: v > 0)} pos., +0,5%: {pc(a, lambda v: v >= 0.5)}) · 2 barras {sg(avg(b))} ({pc(b, lambda v: v > 0)} pos.)"
    L = [f"ROBUSTEZ DA SIMETRIA (candles {usado}; {len(sinais)} sinais, {len(dias_u)} pregões)", "Simétrico = nas velas antes do sinal, o histograma ficou perto de metade positivo e metade negativo.", "", "1) Limiar (janela de 24 velas):"]
    for thr in (0.10, 0.15, 0.20, 0.25):
        sim = [x for x in sinais if x["simet"][24] <= thr]; nao = [x for x in sinais if x["simet"][24] > thr]
        L.append(lin(f"  simétrico até {fmt(thr, 2)}", sim)); L.append(lin("    resto", nao))
    L += ["", "2) Janela (limiar 0,15):"]
    for lj in JANELAS:
        L.append(lin(f"  {lj} velas", [x for x in sinais if x["simet"][lj] <= 0.15]))
    base = [x for x in sinais if x["simet"][24] <= 0.15]
    L += ["", "3) Só os simétricos (24 velas, 0,15), por recorte:",
          lin("  1ª metade dos pregões", [x for x in base if x["dia"] < metade]), lin("  2ª metade dos pregões", [x for x in base if x["dia"] >= metade]),
          lin("  BOVA11", [x for x in base if x["g"] == "BOVA11"]), lin("  ações", [x for x in base if x["g"] == "ações"]),
          lin("  calls", [x for x in base if x["dr"] == 1]), lin("  puts", [x for x in base if x["dr"] == -1]),
          "", "Só leitura. Movimento do ativo, sem custo."]
    txt = "\n".join(L)
    for i in range(0, len(txt), 3800):
        messenger.send(txt[i:i + 3800])



def job_gravar_candles(manual=False):
    """Guarda todo dia os candles de 5 min do BOVA11 e das 5 acoes (a brapi so entrega ~7 dias; assim o historico cresce). Nao altera nenhum alerta."""
    try:
        api = Brapi(); novos = 0; erros = []
        for t in LISTA_DIARIA:
            try:
                novos += database.save_candles(t, "5m", api.history(t, "5d", "5m"))
            except Exception as e:
                erros.append(f"{t}: {str(e)[:60]}")
        log.info("Candles de 5 min guardados: %d novos, %d erros", novos, len(erros))
        if manual:
            from datetime import datetime as _dt
            res = database.candles_resumo()
            L = [f"Candles de 5 min guardados hoje: {novos} novos."] + [f"{r['ticker']}: {r['n']} candles, de {_dt.fromtimestamp(r['a'], TZ).strftime('%d/%m')} a {_dt.fromtimestamp(r['b'], TZ).strftime('%d/%m')}" for r in res]
            if erros: L.append("Erros: " + "; ".join(erros))
            messenger.send("\n".join(L))
        elif erros:
            log.warning("Candles com erro: %s", "; ".join(erros))
    except Exception:
        log.exception("gravar_candles falhou")

def job_gravar_candles_manual():
    job_gravar_candles(True)

def _idx_sinais_call(di, hi, i_min=60):
    """Indices onde a linha vira para cima e o histograma cruza o zero (janela de 2 candles), com 5 candles de intervalo: mesma regra do alerta diario."""
    out = []; ult = -99
    for i in range(i_min, len(di)):
        fl = any(di[k] == 1 and di[k - 1] == -1 for k in range(i - 2, i + 1))
        cr = any(hi[k] > 0 >= hi[k - 1] for k in range(i - 2, i + 1))
        nasce = (di[i] == 1 and di[i - 1] == -1) or (hi[i] > 0 >= hi[i - 1])
        if fl and cr and nasce and i - ult >= 5:
            ult = i; out.append(i)
    return out

def _dias_na_direcao(di):
    n = 1
    while n < len(di) and di[-1 - n] == di[-1]: n += 1
    return n


def job_diag_linhas2():
    try:
        _diag_linhas2()
    except Exception as e:
        log.exception("diag_linhas2 falhou")
        messenger.send(f"Teste das linhas no diário falhou: {type(e).__name__}: {str(e)[:200]}")

def _diag_linhas2(tickers=None, titulo="BOVA11 + 5 ações"):
    """Comando 'linhas2' (so leitura): sinais de call do diario (linha vira + MACD cruza) com a SuperTrend 10x3 e com a linha propria, nos 6 ativos, com o maior historico que a brapi entregar. Mede o ativo e a opcao estimada (BS, ATM, 45 dias, 5% de custo por lado) apos 5 e 10 pregoes e na saida por histograma encolhendo, contra entrar em qualquer dia."""
    import opcao_teste as ot
    from messenger import fmt
    api = Brapi(); dados = {}; usado = None
    for rg in ("5y", "3y", "2y", "1y"):
        try:
            tmp = {}
            for t in (tickers or LISTA_DIARIA):
                try: tmp[t] = api.history(t, rg, "1d")
                except Exception as e: log.warning("linhas2 %s %s: %s", t, rg, e)
            tmp = {t: v for t, v in tmp.items() if len(v) > 100}
            if (tmp and all(len(v) > 200 for v in tmp.values())) or (tmp and rg == "1y"):
                dados, usado = tmp, rg; break
        except Exception as e:
            log.warning("linhas2 %s: %s", rg, e)
    if not dados:
        messenger.send("Teste das linhas: a brapi não entregou o diário."); return
    dia = lambda dl, k: datetime.fromtimestamp(dl[k]["date"], TZ).date()
    res = {"st": [], "pr": []}; base = []
    for t, dl in dados.items():
        N = len(dl); cl = [b["close"] for b in dl]; hi = _macd_hist(cl)
        di = {"st": _supertrend_dir(dl), "pr": _dir_trail(dl, 6, 0.5)}
        v_ = lambda i: ot.sigma_hist(dl[:i + 1], dia(dl, i))
        def medir(i):
            if i + 10 >= N: return None
            S0 = cl[i]; v = v_(i); K = ot.strike_por_delta(True, S0, 45 / 365.0, v, 0.5)
            P0 = ot.valor(True, S0, K, 45, v)
            if P0 <= 0.01: return None
            opc = lambda S1, h: (ot.valor(True, S1, K, max(45 - round(h * 7 / 5), 1), v) * 0.95 / (P0 * 1.05) - 1) * 100
            r = {}
            for h in (5, 10):
                r[f"a{h}"] = (cl[i + h] / S0 - 1) * 100
                r[f"o{h}"] = opc(cl[i + h], h)
                r[f"p{h}"] = opc(max(b["high"] for b in dl[i + 1:i + h + 1]), h)
            return r
        for i in range(60, N - 10):
            r = medir(i)
            if r: base.append(r)
        for nome in ("st", "pr"):
            for i in _idx_sinais_call(di[nome], hi):
                r = medir(i)
                if r: r["t"] = t; res[nome].append(r)
    sg = lambda x: ("+" if x >= 0 else "−") + fmt(abs(x), 1) + "%"
    avg = lambda v: sum(v) / len(v) if v else 0.0
    def bloco(nome, rs):
        if not rs: return [f"{nome}: sem sinais medidos"]
        n = len(rs); o = [f"{nome}: {n} sinais"]
        for h in (5, 10):
            a = [r[f"a{h}"] for r in rs]; op = [r[f"o{h}"] for r in rs]; pk = [r[f"p{h}"] for r in rs]
            o.append(f"  {h} pregões: ativo {sg(avg(a))} (positivo em {fmt(100 * sum(1 for x in a if x > 0) / n, 0)}%) · opção {sg(avg(op))} (positiva em {fmt(100 * sum(1 for x in op if x > 0) / n, 0)}%) · pico da opção acima de +20% em {fmt(100 * sum(1 for x in pk if x >= 20) / n, 0)}%")
        return o
    L = [f"TESTE DAS LINHAS NO DIÁRIO (histórico {usado}; {titulo}; só calls)", "Opção: BS, ATM, 45 dias, 5% de custo por lado. Pico usa a máxima do período (teto).", ""]
    L += bloco("SuperTrend 10x3 (alerta atual)", res["st"]) + [""] + bloco("Linha própria 6 velas / 0,5 ATR", res["pr"]) + [""] + bloco("Qualquer dia (base)", base)
    pr_t = {}
    for r in res["pr"]: pr_t[r["t"]] = pr_t.get(r["t"], 0) + 1
    st_t = {}
    for r in res["st"]: st_t[r["t"]] = st_t.get(r["t"], 0) + 1
    L += ["", "Sinais da SuperTrend por ativo: " + (" · ".join(f"{k} {v}" for k, v in sorted(st_t.items())) or "nenhum"), "Sinais da linha própria por ativo: " + " · ".join(f"{k} {v}" for k, v in sorted(pr_t.items())), "Poucos sinais por ativo: leia como indício, não como prova."]
    messenger.send("\n".join(L))

def job_diag_linhas3():
    try:
        outros = [t for t in config.UNIVERSE if t not in LISTA_DIARIA]
        _diag_linhas2(outros, f"outros {len(outros)} ativos do robô")
    except Exception as e:
        log.exception("diag_linhas3 falhou")
        messenger.send(f"Teste das linhas nos outros ativos falhou: {type(e).__name__}: {str(e)[:200]}")


def job_diag_escolha():
    try:
        _diag_escolha()
    except Exception as e:
        log.exception("diag_escolha falhou")
        messenger.send(f"Teste da escolha da opção falhou: {type(e).__name__}: {str(e)[:200]}")

def _diag_escolha(tickers=None, titulo="todos os ativos do robô"):
    """Comando 'opcoes' (so leitura): nos sinais de call da SuperTrend 10x3 + MACD (diario, todos os ativos do robo, ate 5 anos), compara a escolha da opcao: delta 0,4 / 0,5 / 0,7 x vencimento 30 / 45 / 60 / 90 dias, saindo em 5, 10, 20 pregoes ou pelo histograma encolhendo (o aviso de saida do robo). BS, vol historica, 5% de custo por lado. Contra entrar em qualquer dia (1 a cada 5)."""
    import opcao_teste as ot
    from messenger import fmt
    api = Brapi(); dados = {}; usado = None
    tks = list(tickers) if tickers else list(dict.fromkeys(list(LISTA_DIARIA) + list(config.UNIVERSE)))
    for rg in ("5y", "3y", "2y", "1y"):
        tmp = {}
        for t in tks:
            try: tmp[t] = api.history(t, rg, "1d")
            except Exception as e: log.warning("opcoes %s %s: %s", t, rg, e)
        tmp = {t: v for t, v in tmp.items() if len(v) > 100}
        if tmp and (all(len(v) > 200 for v in tmp.values()) or rg == "1y"):
            dados, usado = tmp, rg; break
    if not dados:
        messenger.send("Teste da opção: a brapi não entregou o diário."); return
    dia = lambda dl, k: datetime.fromtimestamp(dl[k]["date"], TZ).date()
    DELTAS = (0.4, 0.5, 0.7); DTES = (30, 45, 60, 90); HOLDS = (5, 10, 20)
    sinais = []; base = []
    for t, dl in dados.items():
        N = len(dl); cl = [b["close"] for b in dl]; hi = _macd_hist(cl); di = _supertrend_dir(dl)
        for i in _idx_sinais_call(di, hi):
            if i + 20 < N: sinais.append((dl, cl, hi, di, i, True))
        for i in range(60, N - 20, 5): base.append((dl, cl, hi, di, i, False))
    def saida_hist(hi, di, i, N):
        k = i + 1
        while k < N - 1 and k - i < 20 and not (hi[k] < hi[k - 1] or di[k] == -1): k += 1
        return k
    def rodar(lista, delta, dte):
        acc = {h: [] for h in HOLDS}; acc["H"] = []; dias_h = []
        for dl, cl, hi, di, i, _ in lista:
            S0 = cl[i]; v = ot.sigma_hist(dl[:i + 1], dia(dl, i))
            K = ot.strike_por_delta(True, S0, dte / 365.0, v, delta)
            P0 = ot.valor(True, S0, K, dte, v)
            if P0 <= 0.01: continue
            op = lambda S1, h: (ot.valor(True, S1, K, max(dte - round(h * 7 / 5), 1), v) * 0.95 / (P0 * 1.05) - 1) * 100
            for h in HOLDS: acc[h].append(op(cl[i + h], h))
            k = saida_hist(hi, di, i, len(dl)); acc["H"].append(op(cl[k], k - i)); dias_h.append(k - i)
        return acc, dias_h
    avg = lambda v: sum(v) / len(v) if v else 0.0
    pos = lambda v: 100 * sum(1 for x in v if x > 0) / len(v) if v else 0.0
    sg = lambda x: ("+" if x >= 0 else "−") + fmt(abs(x), 0) + "%"
    L = [f"ESCOLHA DA OPÇÃO NOS SINAIS DIÁRIOS ({titulo}; SuperTrend + MACD; {len(sinais)} sinais, histórico {usado}; só calls)",
         "Cada célula: média da opção (% positivas). BS, vol histórica, 5% de custo por lado. Base = qualquer dia, 10 pregões.", ""]
    melhores = []
    for d in DELTAS:
        for T in DTES:
            a, dh = rodar(sinais, d, T); b, _ = rodar(base, d, T)
            cel = lambda k: f"{sg(avg(a[k]))} ({fmt(pos(a[k]), 0)}%)"
            L.append(f"Delta {fmt(d, 1)} · {T}d: 5p {cel(5)} · 10p {cel(10)} · 20p {cel(20)} · saída hist {cel('H')}, {fmt(avg(dh), 1)} pregões · base {sg(avg(b[10]))} ({fmt(pos(b[10]), 0)}%)")
            melhores.append((pos(a["H"]) + pos(a[10]), d, T, avg(a["H"]), avg(a[10])))
    melhores.sort(reverse=True)
    L += ["", "Melhores por % de opções positivas (saída histograma + 10 pregões):"]
    for _, d, T, mh, m10 in melhores[:3]:
        L.append(f"  delta {fmt(d, 1)} · {T} dias: saída hist {sg(mh)} · 10 pregões {sg(m10)}")
    L += ["", "Spread igual para todas as opções; delta alto costuma ter spread menor em % e vencimento longo, menos desgaste: confira no livro real antes de usar."]
    txt = "\n".join(L)
    for i in range(0, len(txt), 3800): messenger.send(txt[i:i + 3800])

def job_diag_escolha6():
    try:
        _diag_escolha(list(LISTA_DIARIA), "só BOVA11 + 5 ações")
    except Exception as e:
        log.exception("diag_escolha6 falhou")
        messenger.send(f"Teste da escolha da opção nos 6 falhou: {type(e).__name__}: {str(e)[:200]}")

def job_diag_linhas():
    try:
        _diag_linhas()
    except Exception as e:
        log.exception("diag_linhas falhou")
        messenger.send(f"Comparação das linhas falhou: {type(e).__name__}: {str(e)[:200]}")

def _diag_linhas():
    """Comando 'linhas' (so leitura): no grafico diario dos 6 ativos, compara a linha do alerta atual (SuperTrend 10x3) com a linha propria (6 velas, 0,5 ATR): direcao hoje, ha quantos pregoes, e quantos sinais de call (linha vira + MACD cruza) cada uma deu em 1 ano."""
    api = Brapi(); L = ["LINHA DO ALERTA DIÁRIO x LINHA PRÓPRIA (gráfico diário, 1 ano)", "Sinal = linha vira para cima e histograma do MACD 12-26-9 cruza o zero.", ""]
    tot = {"st": 0, "pr": 0, "ambos": 0}
    for t in LISTA_DIARIA:
        try:
            dl = api.history(t, "1y", "1d")
            if len(dl) < 80:
                L.append(f"{t}: poucos candles"); continue
            cl = [b["close"] for b in dl]
            hi = _macd_hist(cl)
            st = _supertrend_dir(dl); pr = _dir_trail(dl, 6, 0.5)
            ss, sp = _idx_sinais_call(st, hi), _idx_sinais_call(pr, hi)
            tot["st"] += len(ss); tot["pr"] += len(sp); tot["ambos"] += len(set(ss) & set(sp))
            seta = lambda d: "▲" if d[-1] == 1 else "▼"
            ult = lambda s: datetime.fromtimestamp(dl[s[-1]]["date"], TZ).strftime("%d/%m") if s else "-"
            L.append(f"{t}: SuperTrend {seta(st)} ({_dias_na_direcao(st)} pregões) · própria {seta(pr)} ({_dias_na_direcao(pr)} pregões) · histograma {'sobe' if hi[-1] > hi[-2] else 'cai'}")
            L.append(f"   sinais de call em 1 ano: SuperTrend {len(ss)} (último {ult(ss)}) · própria {len(sp)} (último {ult(sp)})")
        except Exception as e:
            L.append(f"{t}: erro ({str(e)[:60]})")
    L += ["", f"Total de sinais em 1 ano: SuperTrend {tot['st']} · própria {tot['pr']} · mesmo candle nas duas {tot['ambos']}", "Só leitura: nada é alterado no alerta das 18:10."]
    messenger.send("\n".join(L))


# ---- Placar de regras intradiarias de 5 min (so registro e medicao: nao envia alerta de entrada) ----
VARIANTES_5M = {"A": "linha + MACD cruzando o zero", "B": "A com barras do histograma simétricas antes (compressão)", "C": "reentrada: tendência de pé, histograma crescendo e recuo de 0,5 ATR"}

def _atr_serie(dl):
    N = len(dl)
    tr = [dl[0]["high"] - dl[0]["low"]] + [max(dl[i]["high"] - dl[i]["low"], abs(dl[i]["high"] - dl[i - 1]["close"]), abs(dl[i]["low"] - dl[i - 1]["close"])) for i in range(1, N)]
    atr = [0.0] * N
    for i in range(N):
        atr[i] = sum(tr[:i + 1]) / (i + 1) if i < 14 else (atr[i - 1] * 13 + tr[i]) / 14
    return atr

def _sinais_5m(dl, i_min=60):
    """Lista (variante, direcao, i, simetria) para todos os candles i >= i_min. Linha propria (6 velas, 0,5 ATR) + MACD 14-34-14. Entre 10:30 e 16:30."""
    N = len(dl)
    if N < i_min + 5: return []
    cl = [b["close"] for b in dl]
    di = _dir_trail(dl, 6, 0.5); hi = _macd_hist(cl, 14, 34, 14); atr = _atr_serie(dl)
    hm = [datetime.fromtimestamp(b["date"], TZ) for b in dl]
    LJ = 24; out = []
    ult = {("A", 1): -99, ("A", -1): -99, ("C", 1): -99, ("C", -1): -99}
    for i in range(i_min, N):
        h = hm[i].hour + hm[i].minute / 60
        if not (10.5 <= h <= 16.5): continue
        for dr in (1, -1):
            fl = any(di[k] == dr and di[k - 1] == -dr for k in range(i - 2, i + 1))
            cr = any((hi[k] > 0 >= hi[k - 1]) if dr == 1 else (hi[k] < 0 <= hi[k - 1]) for k in range(i - 2, i + 1))
            nasce = (di[i] == dr and di[i - 1] == -dr) or ((hi[i] > 0 >= hi[i - 1]) if dr == 1 else (hi[i] < 0 <= hi[i - 1]))
            if fl and cr and nasce and i - ult[("A", dr)] >= 5:
                ult[("A", dr)] = i
                jan = hi[i - 2 - LJ:i - 2]
                simet = abs(sum(1 for x in jan if x > 0) / LJ - 0.5)
                out.append(("A", dr, i, simet))
                if simet <= 0.15: out.append(("B", dr, i, simet))
            # C: tendencia de pe, histograma do lado certo crescendo por 3 barras, recuo de 0,5 ATR da extrema das ultimas 12 velas (mesmo dia)
            if i >= 12 and di[i] == dr and dr * hi[i] > 0 and dr * (hi[i] - hi[i - 1]) > 0 and dr * (hi[i - 1] - hi[i - 2]) > 0 and i - ult[("C", dr)] >= 12:
                jan12 = dl[i - 12:i]
                if all(hm[k].date() == hm[i].date() for k in (i - 12, i)):
                    ref = max(b["high"] for b in jan12) if dr == 1 else min(b["low"] for b in jan12)
                    if dr * (ref - cl[i]) >= 0.5 * atr[i]:
                        ult[("C", dr)] = i
                        out.append(("C", dr, i, 0.0))
    return out

def job_sinais_5m():
    """A cada 5 min no pregao: grava os sinais novos das regras de teste (candle ja fechado). So registro, sem alerta."""
    try:
        t0 = now()
        hh = t0.hour + t0.minute / 60
        if not (10.5 <= hh <= 17.2): return
        api = Brapi(); novos = 0
        for t in LISTA_DIARIA:
            try:
                dl = api.history(t, "5d", "5m")
                if dl and dl[-1]["date"] + 300 > t0.timestamp(): dl = dl[:-1]      # candle ainda formando
                N = len(dl)
                for var, dr, i, simet in _sinais_5m(dl):
                    if i >= N - 3:   # os 3 ultimos fechados (cobre atraso da brapi); a chave unica evita duplicar
                        dia = datetime.fromtimestamp(dl[i]["date"], TZ).strftime("%Y-%m-%d")
                        if database.save_sinal_intra(var, t, dr, dl[i]["date"], dia, dl[i]["close"], simet): novos += 1
            except Exception as e:
                log.warning("sinais_5m %s: %s", t, e)
        if novos: log.info("Sinais 5m gravados: %d", novos)
    except Exception:
        log.exception("sinais_5m falhou")

def _avalia_sinal(dl, daily, s):
    """Mede um sinal gravado: pico a favor, saida na virada da linha, saida com 2 barras encolhendo (ou fim do dia) e opcao estimada (BS, ATM, 35 dias, 5% de custo por lado)."""
    import opcao_teste as ot
    idx = next((k for k, b in enumerate(dl) if b["date"] == s["ts"]), None)
    if idx is None: return {"sem_dado": 1}
    cl = [b["close"] for b in dl]; dr = s["dir"]
    di = _dir_trail(dl, 6, 0.5); hi = _macd_hist(cl, 14, 34, 14)
    dia = lambda k: datetime.fromtimestamp(dl[k]["date"], TZ).date()
    N = len(dl); d0 = dia(idx)
    k = idx + 1; fim_l = fim_h = idx; pico = 0.0; aberto_l = aberto_h = True
    while k < N and dia(k) == d0:
        fav = (dl[k]["high"] / cl[idx] - 1) * 100 if dr == 1 else (1 - dl[k]["low"] / cl[idx]) * 100
        pico = max(pico, fav)
        if aberto_l:
            fim_l = k
            if di[k] == -dr: aberto_l = False
        if aberto_h:
            fim_h = k
            if di[k] == -dr or all((hi[k - j] < hi[k - j - 1]) if dr == 1 else (hi[k - j] > hi[k - j - 1]) for j in range(2)): aberto_h = False
        k += 1
    pct = lambda f: dr * (cl[f] / cl[idx] - 1) * 100
    S0 = cl[idx]; call = dr == 1; v = ot.sigma_hist(daily, d0)
    K = ot.strike_por_delta(call, S0, 35 / 365.0, v, 0.5)
    P0 = ot.valor(call, S0, K, 35, v)
    opc = lambda S1: (ot.valor(call, S1, K, 35, v) * 0.95 / (P0 * 1.05) - 1) * 100 if P0 > 0.01 else None
    S_pico = S0 * (1 + dr * pico / 100)
    return {"pico": pico, "linha": pct(fim_l), "h2": pct(fim_h), "opc_h2": opc(cl[fim_h]), "opc_pico": opc(S_pico)}

def _roda_avaliacao():
    pend = [s for s in database.sinais_intra(True) if s["dia"] <= str(now().date())]
    if not pend: return 0
    api = Brapi(); cache = {}; n = 0
    for s in pend:
        t = s["ticker"]
        if t not in cache:
            try: cache[t] = (api.history(t, "5d", "5m"), api.history(t, "3mo", "1d"))
            except Exception as e:
                log.warning("avaliacao %s: %s", t, e); cache[t] = None
        if not cache[t]: continue
        dl, daily = cache[t]
        if s["dia"] == str(now().date()) and now().hour < 18: continue   # dia ainda aberto
        try:
            database.set_res_sinal(s["id"], json.dumps(_avalia_sinal(dl, daily, s))); n += 1
        except Exception as e:
            log.warning("avalia %s: %s", t, e)
    return n

def _placar_5m_texto(desde=None):
    from messenger import fmt
    L = ["PLACAR DAS REGRAS DE 5 MIN (só registro; nenhum alerta de entrada)", "Opção = estimativa BS, ATM, 35 dias, 5% de custo por lado.", ""]
    todos = [s for s in database.sinais_intra() if (desde is None or s["dia"] >= desde)]
    pend = sum(1 for s in todos if not s["res"])
    for var, nome in VARIANTES_5M.items():
        av = [(s, json.loads(s["res"])) for s in todos if s["var"] == var and s["res"]]
        av = [(s, r) for s, r in av if not r.get("sem_dado")]
        if not av:
            L.append(f"{var} ({nome}): sem sinais medidos ainda"); continue
        n = len(av); pc = lambda f: fmt(100 * sum(1 for s, r in av if f(r)) / n, 0) + "%"
        mh = sum(r["h2"] for s, r in av) / n
        mo = [r["opc_h2"] for s, r in av if r["opc_h2"] is not None]
        L.append(f"{var} ({nome}): {n} sinais")
        L.append(f"  ativo, saída pelo histograma: média {'+' if mh >= 0 else '−'}{fmt(abs(mh), 2)}% · acima de +0,5%: {pc(lambda r: r['h2'] >= 0.5)} · pico a favor acima de +0,5%: {pc(lambda r: r['pico'] >= 0.5)}")
        L.append(f"  opção: pico acima de +20%: {pc(lambda r: (r['opc_pico'] or 0) >= 20)} · saída positiva: {fmt(100 * sum(1 for x in mo if x > 0) / max(len(mo), 1), 0)}%")
        bo = [r["h2"] for s, r in av if s["ticker"] == "BOVA11"]
        if bo: L.append(f"  BOVA11: {len(bo)} sinais, acima de +0,5%: {fmt(100 * sum(1 for x in bo if x >= 0.5) / len(bo), 0)}%")
    if pend: L.append(f"\n{pend} sinais ainda sem medir (dia aberto ou sem dado).")
    if not todos: L.append("Ainda não há sinais gravados.")
    return "\n".join(L)

def job_placar_5m(manual=False, semanal=False):
    try:
        _roda_avaliacao()
        if manual or semanal:
            messenger.send(_placar_5m_texto())
    except Exception as e:
        log.exception("placar_5m falhou")
        messenger.send(f"Placar 5 min falhou: {type(e).__name__}: {str(e)[:200]}")

def job_placar_5m_manual():
    job_placar_5m(True)

def job_placar_5m_semanal():
    job_placar_5m(False, True)



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
    positions.EXTRA.update({"bomdia": job_morning, "abertura": job_morning, "variacao": job_diag_variacao, "atrasos": job_diag_atrasos, "volume": job_diag_volume, "niveis": job_niveis, "usim5": job_explicar, "resultados": job_diag_resultados, "regras": job_diag_regras, "filtros": job_diag_filtros, "fatores": job_diag_fatores, "bova": job_diag_bova, "comparar": job_comparar, "dias": job_comparar_dias, "repique": job_diag_repique, "forte": job_diag_forte, "opcao": job_diag_opcao, "recuo": job_diag_recuo, "cenarios": job_diag_cenarios, "exemplos": job_diag_exemplos, "saidas": job_diag_saidas, "continuacao": job_diag_continuacao, "exemplos2": job_diag_exemplos2, "lista": job_diag_lista, "direcao": job_diag_direcao, "setores": job_diag_setores, "empilhar": job_diag_empilhar, "validar": job_diag_validar, "auditoria": job_diag_auditoria, "excursao": job_diag_excursao, "atraso": job_diag_atraso, "movido": job_diag_movido, "toque": job_diag_toque, "veredito": job_diag_veredito, "controle": job_diag_controle, "regime": job_diag_regime, "regimevivo": job_diag_regime_vivo, "macd": job_diag_macd, "macdvalida": job_diag_macd_valida, "macdopcao": job_diag_macd_opcao, "bovadiario": job_bova_manual, "intradia": job_diag_intradia, "calibra": job_diag_calibra, "calibra2": job_diag_calibra2, "calibra3": job_diag_calibra3, "validalinha": job_diag_valida_linha, "intradia2": job_diag_intradia2, "lateral": job_diag_lateral, "lateral2": job_diag_lateral2, "candles": job_gravar_candles_manual, "placar5m": job_placar_5m_manual, "linhas": job_diag_linhas, "linhas2": job_diag_linhas2, "linhas3": job_diag_linhas3, "escolha": job_diag_escolha, "escolha6": job_diag_escolha6})
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

def _linha_diario_bomdia(api):
    """Linha do bom dia (so leitura): estado diario de BOVA11 e das 5 acoes no ultimo fechamento (linha + histograma do MACD + call em aberto). Nao dispara alerta nem grava nada."""
    partes = []
    for t in LISTA_DIARIA:
        try:
            dl = api.history(t, "1y", "1d")
            if len(dl) < 80:
                partes.append(f"{t} sem dado"); continue
            di = _supertrend_dir(dl); hi = _macd_hist([b["close"] for b in dl])
            ka = "bova:aberta" if t == "BOVA11" else f"lm:aberta:{t}"
            seta = "▲" if di[-1] == 1 else "▼"
            hist = "sobe" if hi[-1] > hi[-2] else "cai"
            partes.append(f"{seta} {t} {hist}" + (" · call aberta" if database.get_meta(ka) else ""))
        except Exception as e:
            log.warning("bom dia diario %s: %s", t, e)
            partes.append(f"{t} erro")
    return "Diário (linha · MACD): " + " | ".join(partes)

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
    try:   # estado diario de BOVA11 e das 5 acoes (so leitura)
        L.append(_linha_diario_bomdia(Brapi()))
    except Exception as e:
        log.warning("Linha diaria do bom dia: %s", e)
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
    s.add_job(_util(job_gravar_candles), "cron", day_of_week=wk, hour=18, minute=20, id="gravar_candles", misfire_grace_time=3600, coalesce=True)
    s.add_job(_util(job_sinais_5m), "cron", day_of_week=wk, hour="10-17", minute="*/5", id="sinais_5m", misfire_grace_time=120, coalesce=True)
    s.add_job(_util(job_placar_5m), "cron", day_of_week=wk, hour=18, minute=35, id="placar_5m", misfire_grace_time=3600, coalesce=True)
    s.add_job(_util(job_placar_5m_semanal), "cron", day_of_week="fri", hour=18, minute=40, id="placar_5m_semanal", misfire_grace_time=3600, coalesce=True)
    s.add_job(job_gravar_candles, "date", run_date=now() + timedelta(seconds=90), id="gravar_candles_boot")
    s.add_job(_util(job_bova_diario), "cron", day_of_week=wk, hour=18, minute=10, id="bova_diario", misfire_grace_time=3600, coalesce=True)
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
    if date(2026, 11, 12) <= now().date() <= date(2026, 11, 14):   # envio unico do teste de toque
        s.add_job(_once_key("toque:2026-11-12", job_diag_toque), "date", run_date=now() + timedelta(seconds=150), id="toque_once")
    if date(2026, 11, 12) <= now().date() <= date(2026, 11, 14):   # envio unico do teste do veredito
        s.add_job(_once_key("veredito:2026-11-12", job_diag_veredito), "date", run_date=now() + timedelta(seconds=150), id="veredito_once")
    if date(2026, 11, 12) <= now().date() <= date(2026, 11, 14):   # envio unico do teste de controle
        s.add_job(_once_key("controle:2026-11-12", job_diag_controle), "date", run_date=now() + timedelta(seconds=150), id="controle_once")
    if date(2026, 11, 12) <= now().date() <= date(2026, 11, 14):   # envio unico do teste de regime
        s.add_job(_once_key("regime:2026-11-12", job_diag_regime), "date", run_date=now() + timedelta(seconds=150), id="regime_once")
    if date(2026, 11, 12) <= now().date() <= date(2026, 11, 14):   # envio unico do regime nos alertas
        s.add_job(_once_key("regimevivo:2026-11-12", job_diag_regime_vivo), "date", run_date=now() + timedelta(seconds=150), id="regimevivo_once")
    if date(2026, 11, 12) <= now().date() <= date(2026, 11, 14):   # envio unico do teste do MACD
        s.add_job(_once_key("macd:2026-11-12", job_diag_macd), "date", run_date=now() + timedelta(seconds=150), id="macd_once")
    if date(2026, 11, 12) <= now().date() <= date(2026, 11, 14):   # envio unico da validacao do MACD
        s.add_job(_once_key("macdvalida:2026-11-12", job_diag_macd_valida), "date", run_date=now() + timedelta(seconds=150), id="macdvalida_once")
    if date(2026, 11, 13) <= now().date() <= date(2026, 11, 14):   # envio unico do teste do lateral 2
        s.add_job(_once_key("lateral2:2026-11-13", job_diag_lateral2), "date", run_date=now() + timedelta(seconds=150), id="lateral2_once")
    if date(2026, 11, 13) <= now().date() <= date(2026, 11, 14):   # envio unico do teste do lateral
        s.add_job(_once_key("lateral:2026-11-13", job_diag_lateral), "date", run_date=now() + timedelta(seconds=150), id="lateral_once")
    if date(2026, 11, 13) <= now().date() <= date(2026, 11, 14):   # envio unico do teste intradiario 2
        s.add_job(_once_key("intradia2:2026-11-13", job_diag_intradia2), "date", run_date=now() + timedelta(seconds=150), id="intradia2_once")
    if date(2026, 11, 13) <= now().date() <= date(2026, 11, 14):   # envio unico da validacao da linha
        s.add_job(_once_key("validalinha:2026-11-13", job_diag_valida_linha), "date", run_date=now() + timedelta(seconds=150), id="validalinha_once")
    if date(2026, 11, 13) <= now().date() <= date(2026, 11, 14):   # envio unico da calibracao 3
        s.add_job(_once_key("calibra3:2026-11-13", job_diag_calibra3), "date", run_date=now() + timedelta(seconds=150), id="calibra3_once")
    if date(2026, 11, 13) <= now().date() <= date(2026, 11, 14):   # envio unico da calibracao 2
        s.add_job(_once_key("calibra2:2026-11-13", job_diag_calibra2), "date", run_date=now() + timedelta(seconds=150), id="calibra2_once")
    if date(2026, 11, 12) <= now().date() <= date(2026, 11, 14):   # envio unico da calibracao
        s.add_job(_once_key("calibra:2026-11-12", job_diag_calibra), "date", run_date=now() + timedelta(seconds=240), id="calibra_once")
    if date(2026, 11, 12) <= now().date() <= date(2026, 11, 14):   # envio unico do teste intradiario
        s.add_job(_once_key("intradia:2026-11-12", job_diag_intradia), "date", run_date=now() + timedelta(seconds=200), id="intradia_once")
    if date(2026, 11, 12) <= now().date() <= date(2026, 11, 14):   # envio unico do teste do MACD na opcao
        s.add_job(_once_key("macdopcao:2026-11-12", job_diag_macd_opcao), "date", run_date=now() + timedelta(seconds=150), id="macdopcao_once")
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
