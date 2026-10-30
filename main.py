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
    for s in screener.rank_signals(sigs):
        iv = wl[s.ticker]
        database.log_signal(t0.isoformat(), s.ticker, s.direction, s.dict())
        try:
            opt, err = options_selector.select(api, s, t0.date())
        except BrapiError as e:
            opt, err = None, str(e)
        if opt:   # sem opcao liquida nao vira alerta: fica registrado e aparece no resumo das 18:05
            messenger.send(messenger.alert_msg(s, iv, opt, err, t0.date(), ibov, positions.contar_abertas()))
        else:
            log.info("%s %s: sinal sem opcao (%s); fica para o resumo", s.ticker, s.direction, err)
        database.save_alert(str(t0.date()), s.ticker, s.direction, t0.isoformat(), {"signal": s.dict(), "option": opt, "reason": err, "iv": {"iv_rank": iv["iv_rank"], "iv_pct": iv["iv_pct"]}})
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
    positions.EXTRA.update({"bomdia": job_morning, "abertura": job_morning})
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
        database.set_meta(key, "1")
        fn()
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
    s.add_job(job_morning, "cron", day_of_week=wk, hour=9, minute=30, id="morning", misfire_grace_time=1800, coalesce=True)
    s.add_job(job_heartbeat, "cron", day_of_week=wk, hour=13, minute=0, id="heartbeat", misfire_grace_time=1800, coalesce=True)
    s.add_job(job_summary, "cron", day_of_week=wk, hour=18, minute=5, id="summary", misfire_grace_time=3600, coalesce=True)
    s.add_job(job_boot_status, "date", run_date=now() + timedelta(seconds=50), id="boot_status")
    s.add_job(job_hello, "date", run_date=now() + timedelta(seconds=30), id="hello")
    # na subida, recalcula a watchlist com a regra vigente (assim a mudanca vale no mesmo dia, sem esperar o EOD das 08:30)
    s.add_job(job_eod, "date", run_date=now() + timedelta(seconds=90), id="eod_boot")
    s.add_job(job_posicoes, "cron", day_of_week=wk, hour="10-17", minute="*/5", id="posicoes", max_instances=1, coalesce=True)
    s.add_job(job_placar, "cron", day_of_week=wk, hour=18, minute=20, id="placar", misfire_grace_time=3600, coalesce=True)
    if now().date() == date(2026, 10, 30):   # reenvio unico da abertura de 30/10 ja no formato novo
        s.add_job(_once_key("resend:morning:2026-10-30", job_morning), "date", run_date=now() + timedelta(seconds=45), id="resend_morning")
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
