import os, sys, tempfile
from datetime import datetime, date
from zoneinfo import ZoneInfo
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config
config.DB_PATH = os.path.join(tempfile.mkdtemp(), "t.db")
import database, iv_filter, analyzer, options_selector, messenger

TZ = ZoneInfo("America/Sao_Paulo")

def test_iv_rank_percentile():
    s = list(range(10, 110))  # 100 valores 10..109
    assert round(iv_filter.iv_rank(s, 17.39 + 0), 1) == round((17.39 - 10) / 99 * 100, 1)
    assert iv_filter.iv_percentile(s, 40) == 30.0
    assert iv_filter.iv_favoravel(17.39, 14.68)
    assert not iv_filter.iv_favoravel(14.11, 65.48)   # ENEV3 do Raio-X
    assert not iv_filter.iv_favoravel(None, 10)

def test_evaluate_needs_history_then_approves():
    for i in range(80):
        database.save_iv("TEST3", f"2026-01-{i:03d}", 0.5 - i * 0.002 + (0.3 if i < 40 else 0), "x")
    iv, r, p, ok, note = iv_filter.evaluate("TEST3")
    assert r is not None and ok
    database.save_iv("SHORT3", "2026-01-01", 0.3, "x")
    assert iv_filter.evaluate("SHORT3")[3] is False

def test_pick_expiration():
    e = iv_filter.pick_expiration(["2026-10-16", "2026-11-20", "2026-12-18"], date(2026, 10, 8))
    assert e[0] == "2026-11-20"

def _daily(n=70, base=10.0):
    out = []
    t0 = datetime(2026, 7, 1, 12, tzinfo=TZ).timestamp()
    for i in range(n):
        out.append(dict(date=t0 + i * 86400, open=base, high=base + 0.3, low=base - 0.3, close=base, volume=1_000_000))
    return out

def _m15(day, closes, vol):
    t = datetime(2026, 10, 8, 10, 0, tzinfo=TZ).timestamp()
    return [dict(date=t + i * 900, open=c, high=c, low=c, close=c, volume=vol) for i, c in enumerate(closes)]

def test_breakout_long_and_stop_target():
    now = datetime(2026, 10, 8, 11, 30, tzinfo=TZ)
    daily = _daily()
    daily = [d for d in daily if datetime.fromtimestamp(d["date"], TZ).date() < now.date()]
    m15 = _m15(now, [10.2, 10.35, 10.4, 10.45, 10.5, 10.5], 600_000)
    s = analyzer.analyze("TEST3", daily, m15, now)
    assert s and s.direction == "long" and s.stop < s.level < s.price < s.target
    assert abs((s.target - s.price) - 2 * (s.price - s.stop)) < 1e-9

def test_no_signal_low_volume_or_inside_range():
    now = datetime(2026, 10, 8, 11, 30, tzinfo=TZ)
    daily = [d for d in _daily() if datetime.fromtimestamp(d["date"], TZ).date() < now.date()]
    assert analyzer.analyze("T", daily, _m15(now, [10.4, 10.45, 10.5, 10.5], 1_000), now) is None
    assert analyzer.analyze("T", daily, _m15(now, [10.0, 10.1, 10.0, 10.1], 600_000), now) is None

class FakeApi:
    def expirations(self, u, include_expired=False): return ["2026-11-20"]
    def analytics(self, u, e, date=None, side=None):
        return "2026-10-07", [dict(symbol="AAA", strike=10, delta=0.52, impliedVolatility=0.3, allocationRoundLot=100),
                              dict(symbol="BBB", strike=11, delta=0.30, impliedVolatility=0.3, allocationRoundLot=100),
                              dict(symbol="CCC", strike=10.5, delta=0.45, impliedVolatility=0.3, allocationRoundLot=100)]
    def chain(self, u, e, side=None):
        return "2026-10-07", [dict(symbol="AAA", bid=0.50, ask=0.52, openInterest=1000, volume=200),
                              dict(symbol="BBB", bid=0.1, ask=0.11, openInterest=1000, volume=200),
                              dict(symbol="CCC", bid=0.2, ask=0.4, openInterest=1000, volume=200)]  # spread alto

def test_option_selection_liquidity():
    now = datetime(2026, 10, 8, 11, 30, tzinfo=TZ)
    daily = [d for d in _daily() if datetime.fromtimestamp(d["date"], TZ).date() < now.date()]
    s = analyzer.analyze("TEST3", daily, _m15(now, [10.2, 10.35, 10.4, 10.45, 10.5, 10.5], 600_000), now)
    opt, err = options_selector.select(FakeApi(), s, date(2026, 10, 8))
    assert opt and opt["symbol"] == "AAA"
    txt = messenger.alert_msg(s, dict(iv=0.3, iv_rank=17.4, iv_pct=14.7), opt, err)
    assert "CALL" in txt and "<code>AAA</code>" in txt and "Alvo:" in txt and "Stop:" in txt and "BOM" in txt
    sem = messenger.alert_msg(s, dict(iv=0.3, iv_rank=17.4, iv_pct=14.7), None, "sem liquidez")
    assert "SEM OPÇÃO" in sem

def test_tick_schedule(monkeypatch):
    import main
    calls = []
    monkeypatch.setattr(main, "job_eod", lambda: calls.append("eod") or 17)
    monkeypatch.setattr(main, "job_intraday", lambda: calls.append("intra"))
    monkeypatch.setattr(main, "job_pre", lambda: calls.append("pre"))
    monkeypatch.setattr(database, "min_history", lambda t: 999)
    marks = set()
    monkeypatch.setattr(database, "done", lambda j, d: (j, d) in marks)
    monkeypatch.setattr(database, "mark_done", lambda j, d: marks.add((j, d)))
    def run(h, m, day=8):
        calls.clear()
        monkeypatch.setattr(main, "now", lambda: datetime(2026, 10, day, h, m, 30, tzinfo=TZ))
        main.job_tick(); return list(calls)
    assert run(8, 30) == ["eod"]            # quinta 08/10
    assert run(8, 35) == []                 # eod ja feito hoje
    assert run(9, 45) == ["intra"]          # abertura: a cada 5 min
    assert run(10, 5) == ["intra"]
    assert run(10, 15) == ["intra"]
    assert run(10, 20) == []                # depois das 10:15: so de 15 em 15
    assert run(10, 30) == ["intra"]
    assert run(17, 45) == ["intra"]
    assert run(17, 50) == []
    assert run(10, 30, day=10) == []        # sabado

def test_backfill_plan_error_not_marked(monkeypatch):
    import main
    from data_fetcher import BrapiPlanError
    monkeypatch.setattr(main, "Brapi", lambda: object())
    monkeypatch.setattr(config, "UNIVERSE", ["ZZZ3"])
    monkeypatch.setattr(main.iv_filter, "collect_iv", lambda *a, **k: (_ for _ in ()).throw(BrapiPlanError("403", 403)))
    sent = []
    monkeypatch.setattr(main.messenger, "send", lambda t: sent.append(t))
    assert main.job_backfill(10) is False        # interrompe
    assert len(sent) == 1 and "recusou" in sent[0]
    d = main.now().date()
    import datetime as dt
    for i in range(10):
        assert not database.was_tried("ZZZ3", str(d - dt.timedelta(days=i)))   # nada marcado como tentado

def test_no_false_active_message(monkeypatch):
    import main
    sent = []
    monkeypatch.setattr(main.messenger, "send", lambda t: sent.append(t))
    monkeypatch.setattr(database, "min_history", lambda t: 0)
    monkeypatch.setattr(main, "job_backfill", lambda *a, **k: True)
    monkeypatch.setattr(main, "job_eod", lambda: 1)           # 1 de 17 ok
    main.job_bootstrap()
    assert sent == []

def test_debug_iv_sends_summary(monkeypatch):
    import main
    sent = []
    monkeypatch.setattr(main.messenger, "send", lambda t: sent.append(t))
    monkeypatch.setenv("DEBUG_IV", "SUZB3")
    class Api:
        def expirations(self, u, include_expired=False): return ["2026-11-20"]
        def analytics(self, u, e, date=None, side=None):
            return "2026-10-16", [dict(symbol="SUZBK430", side="call", strike=43, impliedVolatility=0.27, optionPrice=1.2,
                                        openInterest=900, underlyingPrice=42.8, confidence="medium")]
    monkeypatch.setattr(main, "Brapi", lambda: Api())
    monkeypatch.setattr(main, "now", lambda: datetime(2026, 10, 20, 9, 0, tzinfo=TZ))
    main.job_debug_iv()
    assert len(sent) == 1 and "SUZBK430" in sent[0] and "IV ATM do dia" in sent[0]

def test_prefers_monthly_expiration():
    # 20/10/2026: semanal 27/11 (38 DTE) e mensal 19/11 (30 DTE) -> mensal
    e = iv_filter.pick_expiration(["2026-10-30", "2026-11-13", "2026-11-19", "2026-11-27", "2026-12-18"], date(2026, 10, 20))
    assert e[0] == "2026-11-19"
    # sem mensal na janela: nao usa semanal
    assert iv_filter.pick_expiration(["2026-11-27"], date(2026, 10, 20)) is None

def test_atm_iv_needs_enough_series_near_the_money():
    one = [dict(symbol="A", side="call", strike=44.38, impliedVolatility=0.15, underlyingPrice=43.05)]
    assert iv_filter.atm_iv(one) is None                       # caso SUZB3 do dia 16/10
    far = [dict(symbol="A", side="call", strike=60, impliedVolatility=0.9, underlyingPrice=43.05),
           dict(symbol="B", side="put", strike=30, impliedVolatility=0.8, underlyingPrice=43.05)]
    assert iv_filter.atm_iv(far) is None                       # muito longe do dinheiro
    ok = [dict(symbol="C", side="call", strike=43, impliedVolatility=0.27, underlyingPrice=43.05),
          dict(symbol="P", side="put", strike=43, impliedVolatility=0.25, underlyingPrice=43.05)]
    assert abs(iv_filter.atm_iv(ok) - 0.26) < 1e-9

def test_ensure_iv_method_resets_once():
    database.save_iv("RST3", "2026-01-01", 0.3, "x")
    assert database.ensure_iv_method("tag-x") is True
    assert database.iv_series("RST3", 10) == []
    database.save_iv("RST3", "2026-01-02", 0.3, "x")
    assert database.ensure_iv_method("tag-x") is False         # 2a vez nao apaga
    assert len(database.iv_series("RST3", 10)) == 1


def test_weekly_symbols_are_ignored():
    assert iv_filter.is_weekly_symbol("SUZBK443W4") and not iv_filter.is_weekly_symbol("SUZBK428")
    rows = [dict(symbol="SUZBK443W4", side="call", strike=43, impliedVolatility=0.10, underlyingPrice=43.05),
            dict(symbol="SUZBW443W4", side="put", strike=43, impliedVolatility=0.10, underlyingPrice=43.05)]
    assert iv_filter.atm_iv(rows) is None                      # so semanais -> sem IV

def test_option_selector_skips_weekly_options():
    class Api2(FakeApi):
        def analytics(self, u, e, date=None, side=None):
            return "2026-10-07", [dict(symbol="AAAW1", strike=10, delta=0.50, impliedVolatility=0.3, allocationRoundLot=100),
                                  dict(symbol="AAA", strike=10, delta=0.52, impliedVolatility=0.3, allocationRoundLot=100)]
        def chain(self, u, e, side=None):
            return "2026-10-07", [dict(symbol="AAAW1", bid=0.50, ask=0.51, openInterest=5000, volume=900),
                                  dict(symbol="AAA", bid=0.50, ask=0.52, openInterest=1000, volume=200)]
    now = datetime(2026, 10, 8, 11, 30, tzinfo=TZ)
    daily = [d for d in _daily() if datetime.fromtimestamp(d["date"], TZ).date() < now.date()]
    s = analyzer.analyze("TEST3", daily, _m15(now, [10.2, 10.35, 10.4, 10.45, 10.5, 10.5], 600_000), now)
    opt, _ = options_selector.select(Api2(), s, date(2026, 10, 8))
    assert opt and opt["symbol"] == "AAA"


def test_backtest_series():
    import iv_filter
    # IV caindo ate o minimo no fim: ultimo dia deve ser aprovado em todas as regras
    series = [30 + (i % 7) for i in range(100)] + [20]
    days, hits = iv_filter.backtest_series(series, window=252, min_hist=60)
    assert days == 42
    assert hits[(25, 30)] >= 1 and hits[(50, 50)] >= hits[(25, 30)]


def test_shadow_outcome_and_summary():
    import shadow
    from datetime import datetime
    ts = lambda d: int(datetime(2026, 10, d, 12, tzinfo=TZ).timestamp())
    sig = {"price": 10.0, "stop": 9.0, "target": 12.0, "direction": "long"}
    up = [{"date": ts(20), "high": 10.5, "low": 9.5, "close": 10.2}, {"date": ts(21), "high": 12.1, "low": 10.0, "close": 12.0}]
    assert shadow.outcome(sig, "2026-10-20", up, TZ) == ("alvo", 2.0)
    dn = [{"date": ts(20), "high": 10.5, "low": 8.9, "close": 9.0}]
    assert shadow.outcome(sig, "2026-10-20", dn, TZ) == ("stop", -1.0)
    short = {"price": 10.0, "stop": 11.0, "target": 8.0, "direction": "short"}
    assert shadow.outcome(short, "2026-10-20", [{"date": ts(20), "high": 10.4, "low": 9.0, "close": 9.5}], TZ)[0] == "aberto"
    rows = [({"rule_a": 1, "rule_b": 1, "rule_c": 1}, ("alvo", 2.0)), ({"rule_a": 0, "rule_b": 1, "rule_c": 1}, ("stop", -1.0))]
    out = shadow.summary(rows)
    assert "A (25/30): 1 | 1/0/0 | +2.00R" in out and "B (sem filtro): 2" in out


def test_save_shadow_dedup(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"))
    database.save_shadow("2026-10-20", "MGLU3", "long", "x", 0.5, 20, 25, 1, 1, 1, {"a": 1})
    database.save_shadow("2026-10-20", "MGLU3", "long", "y", 0.5, 20, 25, 1, 1, 1, {"a": 2})
    assert len(database.shadow_rows()) == 1
    assert database.latest_status_date() is None


def test_job_shadow_records_without_sending(monkeypatch, tmp_path):
    import main, analyzer
    from datetime import datetime
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "s.db"))
    database.save_iv_status("2026-10-19", "MGLU3", 0.5, 40.0, 50.0, False)
    database.save_iv_status("2026-10-19", "VALE3", 0.3, None, None, False)
    t0 = datetime(2026, 10, 20, 11, 0, tzinfo=TZ)
    class Api:
        def history(self, t, r, i): return [{"date": int(t0.timestamp())}]
    sig = analyzer.Signal("MGLU3", "long", 6.0, 5.9, 5.7, 6.4, 0.2, 1.8, "alta", "", "2 closes", "medio")
    monkeypatch.setattr(main.analyzer, "analyze", lambda *a: sig)
    sent = []
    monkeypatch.setattr(main.messenger, "send", lambda t: sent.append(t))
    main.job_shadow(Api(), t0, {})
    rows = database.shadow_rows()
    assert len(rows) == 1 and rows[0]["ticker"] == "MGLU3"
    assert (rows[0]["rule_a"], rows[0]["rule_b"], rows[0]["rule_c"]) == (0, 1, 1) and not sent


def test_check_iv_runs_once(monkeypatch, tmp_path):
    import main
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "c.db"))
    monkeypatch.setattr(config, "CHECK_IV", "MGLU3:2026-09-30,2026-09-29")
    for i in range(70):
        database.save_iv("MGLU3", f"2026-07-{(i % 28) + 1:02d}-x{i}", 0.5 + i * 0.001, "2026-11-19")
    database.save_iv("MGLU3", "2026-09-30", 0.40, "2026-11-19")
    sent = []
    monkeypatch.setattr(main.messenger, "send", lambda t: sent.append(t) or True)
    main.job_check_iv()
    main.job_check_iv()
    assert len(sent) == 1
    assert "2026-09-30" in sent[0] and "A sim" in sent[0] and "2026-09-29: sem IV salva" in sent[0]


def test_once_wrapper(monkeypatch, tmp_path):
    import main
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "o.db"))
    monkeypatch.setenv("DEBUG_IV", "PETR4")
    calls = []
    run = main._once("DEBUG_IV", lambda: calls.append(1))
    run(); run()
    assert calls == [1]
    monkeypatch.setenv("DEBUG_IV", "VALE3")   # novo valor roda de novo
    main._once("DEBUG_IV", lambda: calls.append(2))()
    assert calls == [1, 2]


def test_send_falls_back_to_plain_text(monkeypatch):
    import messenger
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "1")
    calls = []
    class R:
        def __init__(self, c, t=""): self.status_code, self.text = c, t
    def post(url, json=None, timeout=0):
        calls.append(json)
        return R(400, "Bad Request: can't parse entities") if "parse_mode" in json else R(200)
    monkeypatch.setattr(messenger.requests, "post", post)
    assert messenger.send("Rank<=25 <b>x</b>") is True
    assert len(calls) == 2 and "parse_mode" not in calls[1] and "<b>" not in calls[1]["text"]


def test_iv_tag_and_gate(monkeypatch):
    import iv_filter
    assert iv_filter.iv_tag(20, 25) == "✅ barata"
    assert iv_filter.iv_tag(99, 99) == "🔥 cara"
    assert iv_filter.iv_tag(40, 76) == "⚠️ média" and iv_filter.iv_tag(51, 60) == "🔥 cara"
    # regra B: IV cara nao bloqueia; regra A (IV_GATE) bloqueia
    monkeypatch.setattr(database, "iv_series", lambda t, w: [0.3 + i * 0.001 for i in range(100)])
    assert iv_filter.evaluate("X")[3] is True
    monkeypatch.setattr(config, "IV_GATE", True)
    assert iv_filter.evaluate("X")[3] is False


def test_alert_msg_verdicts():
    import messenger, analyzer
    sig = analyzer.Signal("MGLU3", "long", 13.60, 13.58, 12.98, 14.83, 0.9, 2.2, "alta", "", "2 closes", "medio")
    opt = {"symbol": "MGLUL160", "strike": 14.86, "ask": 2.39, "bid": 2.32, "dte": 58, "expiration": "2026-12-18",
           "stop_premium": 1.20, "max_loss_per_lot": 239.0}
    cara = dict(iv=1.49, iv_rank=100, iv_pct=100)
    txt = messenger.alert_msg(sig, cara, opt, "")
    assert "🟢 <b>CALL · MGLU3</b>" in txt and "❌ RUIM" in txt and "alvo só +9%" in txt and "IV cara" in txt
    assert "R$ 239 por lote" in txt and "<code>MGLUL160</code>" in txt and "Stop: 12,98" in txt and "tesoura" not in txt and "profit" not in txt.lower()
    media = dict(iv=0.4, iv_rank=50, iv_pct=60)
    assert "⚠️ NEUTRO" in messenger.alert_msg(sig, media, opt, "")           # alvo nao cobre, IV media
    put = analyzer.Signal("VALE3", "short", 65.70, 66.10, 66.90, 63.40, 1.0, 2.0, "baixa", "", "2 closes", "medio")
    popt = {"symbol": "VALEW660", "strike": 66.0, "ask": 1.90, "bid": 1.85, "dte": 58, "expiration": "2026-12-18",
            "stop_premium": 0.95, "max_loss_per_lot": 190.0}
    t2 = messenger.alert_msg(put, media, popt, "")
    assert "🔴 <b>PUT · VALE3</b>" in t2 and "✅ BOM" in t2 and "Empate -2,4%, alvo -4%. IV média." in t2
    t3 = messenger.alert_msg(sig, cara, None, "sem liquidez")
    assert "SEM OPÇÃO" in t3 and "Sem liquidez" in t3 and "Preço" not in t3 and t3.count("\n") < 10


def test_option_lot_never_below_100():
    class Api(FakeApi):
        def analytics(self, *a, **k):
            d, rows = super().analytics(*a, **k)
            return d, [dict(r, allocationRoundLot=1) for r in rows]   # brapi informando lote 1
    now = datetime(2026, 10, 8, 11, 30, tzinfo=TZ)
    daily = [d for d in _daily() if datetime.fromtimestamp(d["date"], TZ).date() < now.date()]
    s = analyzer.analyze("TEST3", daily, _m15(now, [10.2, 10.35, 10.4, 10.45, 10.5, 10.5], 600_000), now)
    opt, _ = options_selector.select(Api(), s, date(2026, 10, 8))
    assert opt["lot"] >= 100 and abs(opt["max_loss_per_lot"] - opt["ask"] * opt["lot"]) < 1e-9


def test_hello_once_per_token(monkeypatch, tmp_path):
    import main
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "h.db"))
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "tok1")
    sent = []
    monkeypatch.setattr(main.messenger, "send", lambda t: sent.append(t) or True)
    main.job_hello(); main.job_hello()
    assert len(sent) == 1
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "tok2")
    main.job_hello()
    assert len(sent) == 2


def test_daily_summary(monkeypatch, tmp_path):
    import main
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "r.db"))
    monkeypatch.setattr(main, "now", lambda: datetime(2026, 10, 23, 18, 5, tzinfo=TZ))
    sent = []
    monkeypatch.setattr(main.messenger, "send", lambda t: sent.append(t) or True)
    main.job_summary()
    assert sent == []                                   # sem ciclos hoje: nada a dizer
    database.save_stats("2026-10-23T10:30:00", "2026-10-23", 30, 10, 20, 0, 0)
    database.save_stats("2026-10-23T17:45:00", "2026-10-23", 30, 12, 18, 0, 0)
    main.job_summary()
    assert "Alertas enviados: 0" in sent[0] and "até 12/30" in sent[0] and "Dados incompletos" in sent[0]
    database.save_stats("2026-10-23T17:50:00", "2026-10-23", 30, 29, 1, 0, 0)
    main.job_summary()
    assert "Sem rompimento confirmado hoje." in sent[1]


def test_boot_status_once_per_version(monkeypatch, tmp_path):
    import main
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "b.db"))
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "abcdef1234")
    database.save_stats("2026-10-23T17:45:00", "2026-10-23", 30, 22, 8, 0, 0)
    sent = []
    monkeypatch.setattr(main.messenger, "send", lambda t: sent.append(t) or True)
    main.job_boot_status(); main.job_boot_status()
    assert len(sent) == 1 and "abcdef1" in sent[0] and "23/10 17:45 (22/30 com dado)" in sent[0] and "nenhum ainda" in sent[0]
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "9999999zzz")
    main.job_boot_status()
    assert len(sent) == 2


def test_near_breakout_and_lines():
    import report
    from datetime import datetime
    def candles(highs, lows, closes):
        return [dict(date=int(datetime(2026, 9, 1 + i, 12, tzinfo=TZ).timestamp()), high=h, low=l, close=c) for i, (h, l, c) in enumerate(zip(highs, lows, closes))]
    n = config.BREAKOUT_LOOKBACK
    up = candles([60] * n, [50] * n, [55] * (n - 1) + [59.6])         # fecha a 0,7% da maxima
    assert report.near_breakout("X", up)[0] == "up"
    down = candles([60] * n, [50] * n, [55] * (n - 1) + [50.3])       # a 0,6% da minima
    assert report.near_breakout("Y", down)[0] == "down"
    mid = candles([60] * n, [50] * n, [55] * n)
    assert report.near_breakout("Z", mid) is None
    lines = report.near_lines([("X", report.near_breakout("X", up)), ("Y", report.near_breakout("Y", down))])
    assert lines[0].startswith("▼ Y") and "máx 60,00" in lines[1] and "fechou 59,60" in lines[1]
    cont, baratos = report.iv_counts([dict(ticker="A", iv_rank=10, iv_pct=10), dict(ticker="B", iv_rank=99, iv_pct=99), dict(ticker="C", iv_rank=None, iv_pct=None)])
    assert cont == {"cara": 1, "média": 0, "barata": 1} and baratos == ["A", "B"]


def test_morning_and_heartbeat(monkeypatch, tmp_path):
    import main
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "m.db"))
    monkeypatch.setattr(config, "UNIVERSE", ["AAA3"])
    monkeypatch.setattr(main, "now", lambda: datetime(2026, 10, 26, 9, 30, tzinfo=TZ))
    monkeypatch.setattr(main, "Brapi", lambda: type("A", (), {"history": lambda self, *a, **k: []})())
    database.save_iv_status("2026-10-23", "AAA3", 0.3, 20.0, 25.0, True)
    sent = []
    monkeypatch.setattr(main.messenger, "send", lambda t: sent.append(t) or True)
    main.job_morning()
    assert "Bom dia · 26/10" in sent[0] and "0 cara · 0 média · 1 barata" in sent[0] and "Nenhum ativo perto de romper." in sent[0]
    main.job_heartbeat()
    assert "sem ciclos hoje" in sent[1]
    database.save_stats("2026-10-26T12:45:00", "2026-10-26", 30, 30, 0, 0, 0)
    main.job_heartbeat()
    assert "Ciclos: 1" in sent[2] and "30/30" in sent[2] and "Alertas hoje: 0" in sent[2]


def test_alert_shows_price_time_and_heartbeat_lag(monkeypatch, tmp_path):
    import messenger, analyzer, main
    sig = analyzer.Signal("MGLU3", "long", 13.60, 13.58, 12.98, 14.83, 0.9, 2.2, "alta", "", "2 closes", "medio")
    sig.asof = "10:45"
    txt = messenger.alert_msg(sig, dict(iv=1.0, iv_rank=50, iv_pct=60), None, "x")
    assert "preço 13,60 (das 10:45)" in txt
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "l.db"))
    monkeypatch.setattr(main, "now", lambda: datetime(2026, 10, 26, 13, 0, tzinfo=TZ))
    database.save_stats("2026-10-26T12:45:00", "2026-10-26", 30, 28, 2, 0, 0)
    database.set_meta("lag_last", "45")
    sent = []
    monkeypatch.setattr(main.messenger, "send", lambda t: sent.append(t) or True)
    main.job_heartbeat()
    assert "Atraso dos dados: ~45 min ⚠️" in sent[0]


def test_selector_explains_why_no_option():
    class Api(FakeApi):
        def chain(self, *a, **k):
            d, rows = super().chain(*a, **k)
            return d, [dict(r, bid=1.0, ask=1.6) for r in rows]          # spread de ~46%
    now = datetime(2026, 10, 8, 11, 30, tzinfo=TZ)
    daily = [d for d in _daily() if datetime.fromtimestamp(d["date"], TZ).date() < now.date()]
    s = analyzer.analyze("TEST3", daily, _m15(now, [10.2, 10.35, 10.4, 10.45, 10.5, 10.5], 600_000), now)
    opt, why = options_selector.select(Api(), s, date(2026, 10, 8))
    assert opt is None and why.startswith("spread alto (") and "séries)" in why


def test_late_alert_warning():
    import messenger, analyzer
    iv = dict(iv=0.3, iv_rank=50, iv_pct=60)
    late = analyzer.Signal("ITUB4", "short", 40.20, 41.51, 42.32, 35.97, 0.8, 2.0, "baixa", "", "2 closes", "medio")
    assert "⚠️ Já andou 3,2% além do nível" in messenger.alert_msg(late, iv, None, "x")
    ok = analyzer.Signal("ITUB4", "short", 41.20, 41.51, 42.32, 38.0, 0.8, 2.0, "baixa", "", "2 closes", "medio")
    assert "Já andou" not in messenger.alert_msg(ok, iv, None, "x")

def test_sinais_sem_opcao_ficam_fora_do_envio():
    d = "2099-01-05"
    database.save_alert(d, "ITUB4", "short", d + "T11:00", {"signal": {}, "option": None})
    database.save_alert(d, "VALE3", "long", d + "T11:05", {"signal": {}, "option": {"symbol": "X"}})
    assert database.no_option_signals(d) == [("ITUB4", "short", "")]
    assert database.count_alerts(d) == 2 and database.count_alerts(d, True) == 1


def test_fallback_ultimo_preco_e_segundo_vencimento():
    now = datetime(2026, 10, 8, 11, 30, tzinfo=TZ)
    daily = [x for x in _daily() if datetime.fromtimestamp(x["date"], TZ).date() < now.date()]
    s = analyzer.analyze("TEST3", daily, _m15(now, [10.2, 10.35, 10.4, 10.45, 10.5, 10.5], 600_000), now)
    class Api:
        def expirations(self, u, include_expired=False): return ["2026-11-20", "2026-12-18"]
        def analytics(self, u, e, date=None, side=None):
            return "2026-10-07", [dict(symbol="N1" if e == "2026-11-20" else "D1", strike=10, delta=0.5, impliedVolatility=0.3, allocationRoundLot=100)]
        def chain(self, u, e, side=None):
            if e == "2026-12-18":   # primeiro vencimento tentado (mais perto de 45 DTE): sem nada negociavel
                return "2026-10-07", [dict(symbol="D1", bid=0, ask=0, close=0, openInterest=1000, volume=200)]
            return "2026-10-07", [dict(symbol="N1", bid=0, ask=0, close=0.80, openInterest=1000, volume=200)]
    opt, err = options_selector.select(Api(), s, date(2026, 10, 26))
    assert opt and opt["symbol"] == "N1" and opt["last_only"] and opt["ask"] == 0.80


def test_job_dist_iv_envia_uma_vez(monkeypatch):
    sent = []
    monkeypatch.setattr(messenger, "send", lambda t: sent.append(t) or True)
    monkeypatch.setattr(database, "get_meta", lambda k: None)
    monkeypatch.setattr(database, "set_meta", lambda k, v: None)
    monkeypatch.setattr(database, "latest_status_date", lambda: "2026-10-26")
    monkeypatch.setattr(database, "latest_status", lambda d: [dict(iv_rank=10, iv_pct=10), dict(iv_rank=60, iv_pct=70), dict(iv_rank=90, iv_pct=95)])
    monkeypatch.setattr(config, "UNIVERSE", ["AAA"])
    monkeypatch.setattr(database, "iv_series", lambda t, w: [0.2 + (i % 10) * 0.01 for i in range(200)])
    import main
    main.job_dist_iv()
    assert len(sent) == 1 and "barata 1" in sent[0] and "cara 2 (antes 1)" in sent[0] and "Histórico" in sent[0]


def test_aviso_dia_atipico_e_ibov():
    import main
    now = datetime(2026, 10, 8, 11, 30, tzinfo=TZ)
    daily = [d for d in _daily() if datetime.fromtimestamp(d["date"], TZ).date() < now.date()]
    s = analyzer.analyze("TEST3", daily, _m15(now, [10.2, 10.35, 10.4, 10.45, 10.5, 10.5], 600_000), now)
    iv = dict(iv=0.3, iv_rank=17.4, iv_pct=14.7)
    assert "Dia atípico" not in messenger.alert_msg(s, iv, None, "x", None, -2.0)
    assert "Dia atípico: Ibovespa -15,8%" in messenger.alert_msg(s, iv, None, "x", None, -15.8)
    ts = lambda d, h: int(datetime(2026, 10, d, h, tzinfo=TZ).timestamp())
    class Api:
        def history(self, sym, r, i): return [dict(date=ts(23, 17), close=100.0), dict(date=ts(26, 12), close=85.0)]
    now = datetime(2026, 10, 26, 11, 0, tzinfo=TZ)
    assert round(main._ibov_change(Api(), now), 1) == -15.0
    class Old(Api):
        def history(self, sym, r, i): return [dict(date=ts(22, 17), close=100.0), dict(date=ts(23, 17), close=99.0)]
    assert main._ibov_change(Old(), now) is None


def test_aviso_ultimo_negocio_no_alerta():
    now = datetime(2026, 10, 8, 11, 30, tzinfo=TZ)
    daily = [d for d in _daily() if datetime.fromtimestamp(d["date"], TZ).date() < now.date()]
    s = analyzer.analyze("TEST3", daily, _m15(now, [10.2, 10.35, 10.4, 10.45, 10.5, 10.5], 600_000), now)
    iv = dict(iv=0.3, iv_rank=17.4, iv_pct=14.7)
    opt = dict(symbol="AAA", strike=10.0, expiration="2026-12-18", ask=0.8, bid=0.8, max_loss_per_lot=80.0, stop_premium=0.4, delta=0.5, dte=50)
    assert "Último negócio" not in messenger.alert_msg(s, iv, opt, "")
    assert "Último negócio, sem compra/venda" in messenger.alert_msg(s, iv, dict(opt, last_only=True), "")


def test_diagnose_e_job_diag(monkeypatch):
    import main
    class Api:
        def expirations(self, u, include_expired=False): return ["2026-11-20", "2026-11-27"]
        def analytics(self, u, e, date=None, side=None):
            return "d", [dict(symbol="A1", strike=10, delta=-0.5), dict(symbol="A2", strike=11, delta=-0.45), dict(symbol="A3", strike=12, delta=-0.1)]
        def chain(self, u, e, side=None):
            return "d", [dict(symbol="A1", bid=0, ask=0, close=0, volume=0, openInterest=0), dict(symbol="A2", bid=0.5, ask=0.52, close=0.5, volume=100, openInterest=900), dict(symbol="A3", bid=0, ask=0, close=0.1)]
    r = options_selector.diagnose(Api(), "XX3", "short", date(2026, 10, 26))
    assert len(r) == 1 and r[0]["exp"] == "2026-11-20" and r[0]["delta"] == 2 and r[0]["sem_cot"] == 1 and r[0]["ok"] == 1 and "A1" in r[0]["ex"]
    sent = []
    monkeypatch.setattr(messenger, "send", lambda t: sent.append(t) or True)
    monkeypatch.setattr(database, "get_meta", lambda k: None)
    monkeypatch.setattr(database, "set_meta", lambda k, v: None)
    monkeypatch.setattr(database, "no_option_signals", lambda d: [("XX3", "short", "")])
    monkeypatch.setattr(main, "Brapi", lambda: Api())
    monkeypatch.setattr(main, "now", lambda: datetime(2026, 10, 26, 19, 0, tzinfo=TZ))
    main.job_diag_opcoes()
    assert len(sent) == 1 and "XX3 ▼ 20/11: 2/3 | 1 | 0 | 0 | 1" in sent[0]
