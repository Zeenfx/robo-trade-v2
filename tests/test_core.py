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
    txt = messenger.full_msg(s, dict(iv=0.3, iv_rank=17.4, iv_pct=14.7), opt, err)
    assert "Invalidação" in txt and "Manejo" in txt and "<code>AAA</code>" in txt

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
    monkeypatch.setattr(main.messenger, "send", lambda t: sent.append(t))
    main.job_check_iv()
    main.job_check_iv()
    assert len(sent) == 1
    assert "2026-09-30" in sent[0] and "A sim" in sent[0] and "2026-09-29: sem IV salva" in sent[0]
