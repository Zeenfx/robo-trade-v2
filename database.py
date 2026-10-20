"""Persistencia SQLite."""
import sqlite3, json, os
from contextlib import contextmanager
import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS iv_history(ticker TEXT, date TEXT, iv REAL, expiration TEXT, PRIMARY KEY(ticker,date));
CREATE TABLE IF NOT EXISTS watchlist(date TEXT, ticker TEXT, iv REAL, iv_rank REAL, iv_pct REAL, PRIMARY KEY(date,ticker));
CREATE TABLE IF NOT EXISTS iv_status(date TEXT, ticker TEXT, iv REAL, iv_rank REAL, iv_pct REAL, approved INTEGER, note TEXT, PRIMARY KEY(date,ticker));
CREATE TABLE IF NOT EXISTS signals(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, ticker TEXT, direction TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS runs(job TEXT, date TEXT, PRIMARY KEY(job,date));
CREATE TABLE IF NOT EXISTS iv_nodata_v2(ticker TEXT, date TEXT, PRIMARY KEY(ticker,date));
CREATE TABLE IF NOT EXISTS shadow_signals(date TEXT, ticker TEXT, direction TEXT, ts TEXT, iv REAL, iv_rank REAL, iv_pct REAL, rule_a INTEGER, rule_b INTEGER, rule_c INTEGER, payload TEXT, PRIMARY KEY(date,ticker,direction));
CREATE TABLE IF NOT EXISTS alerts(date TEXT, ticker TEXT, direction TEXT, ts TEXT, payload TEXT, PRIMARY KEY(date,ticker,direction));
"""

@contextmanager
def conn():
    d = os.path.dirname(config.DB_PATH)
    if d: os.makedirs(d, exist_ok=True)
    c = sqlite3.connect(config.DB_PATH)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    try:
        yield c
        c.commit()
    finally:
        c.close()

def save_iv(ticker, date, iv, expiration):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO iv_history VALUES(?,?,?,?)", (ticker, date, iv, expiration))

def iv_series(ticker, window):
    with conn() as c:
        rows = c.execute("SELECT iv FROM iv_history WHERE ticker=? ORDER BY date DESC LIMIT ?", (ticker, window)).fetchall()
    return [r["iv"] for r in rows][::-1]

def has_iv(ticker, date):
    with conn() as c:
        return c.execute("SELECT 1 FROM iv_history WHERE ticker=? AND date=?", (ticker, date)).fetchone() is not None

def save_iv_status(date, ticker, iv, rank, pct, approved, note=""):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO iv_status VALUES(?,?,?,?,?,?,?)", (date, ticker, iv, rank, pct, int(approved), note))
        if approved:
            c.execute("INSERT OR REPLACE INTO watchlist VALUES(?,?,?,?,?)", (date, ticker, iv, rank, pct))

def latest_watchlist():
    with conn() as c:
        r = c.execute("SELECT MAX(date) d FROM watchlist").fetchone()
        if not r or not r["d"]: return None, []
        rows = c.execute("SELECT * FROM watchlist WHERE date=?", (r["d"],)).fetchall()
    return r["d"], [dict(x) for x in rows]

def latest_status(date):
    with conn() as c:
        return [dict(x) for x in c.execute("SELECT * FROM iv_status WHERE date=? ORDER BY iv_rank", (date,)).fetchall()]

def log_signal(ts, ticker, direction, payload):
    with conn() as c:
        c.execute("INSERT INTO signals(ts,ticker,direction,payload) VALUES(?,?,?,?)", (ts, ticker, direction, json.dumps(payload, default=str)))

def already_alerted(date, ticker, direction):
    with conn() as c:
        return c.execute("SELECT 1 FROM alerts WHERE date=? AND ticker=? AND direction=?", (date, ticker, direction)).fetchone() is not None

def save_alert(date, ticker, direction, ts, payload):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO alerts VALUES(?,?,?,?,?)", (date, ticker, direction, ts, json.dumps(payload, default=str)))

def min_history(tickers):
    """Menor quantidade de pregoes de IV salvos entre os tickers."""
    with conn() as c:
        counts = {r["ticker"]: r["n"] for r in c.execute("SELECT ticker, COUNT(*) n FROM iv_history GROUP BY ticker")}
    return min((counts.get(t, 0) for t in tickers), default=0)

def mark_done(job, date):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO runs VALUES(?,?)", (job, date))

def done(job, date):
    with conn() as c:
        return c.execute("SELECT 1 FROM runs WHERE job=? AND date=?", (job, date)).fetchone() is not None

def mark_tried(ticker, date):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO iv_nodata_v2 VALUES(?,?)", (ticker, date))

def was_tried(ticker, date):
    with conn() as c:
        return c.execute("SELECT 1 FROM iv_nodata_v2 WHERE ticker=? AND date=?", (ticker, date)).fetchone() is not None

def count_iv(ticker):
    with conn() as c:
        return c.execute("SELECT COUNT(*) n FROM iv_history WHERE ticker=?", (ticker,)).fetchone()["n"]

def get_meta(k):
    with conn() as c:
        r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r["v"] if r else None

def ensure_iv_method(tag):
    """Se o metodo de calculo da IV mudou, descarta o historico antigo (para refazer o backfill). True se descartou."""
    if get_meta("iv_method") == tag:
        return False
    with conn() as c:
        for t in ("iv_history", "iv_status", "watchlist", "iv_nodata_v2"):
            c.execute(f"DELETE FROM {t}")
        c.execute("INSERT OR REPLACE INTO meta VALUES('iv_method', ?)", (tag,))
    return True


def save_shadow(date, ticker, direction, ts, iv, rank, pct, a, b, c, payload):
    """Sinal registrado em segredo (nao vai ao Telegram) para comparar as regras de IV: A (25/30), B (so aviso), C (bloqueia Rank>60)."""
    with conn() as c_:
        c_.execute("INSERT OR IGNORE INTO shadow_signals VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (date, ticker, direction, ts, iv, rank, pct, int(a), int(b), int(c), json.dumps(payload, default=str)))

def shadow_rows():
    with conn() as c_:
        return [dict(x) for x in c_.execute("SELECT * FROM shadow_signals ORDER BY date, ticker").fetchall()]

def latest_status_date():
    with conn() as c_:
        r = c_.execute("SELECT MAX(date) d FROM iv_status").fetchone()
    return r["d"] if r else None

def iv_series_until(ticker, date, window):
    """IV salva ate 'date' (inclusive), das mais antigas para as mais recentes."""
    with conn() as c_:
        rows = c_.execute("SELECT iv FROM iv_history WHERE ticker=? AND date<=? ORDER BY date DESC LIMIT ?", (ticker, date, window)).fetchall()
    return [r["iv"] for r in rows][::-1]

def has_iv_on(ticker, date):
    with conn() as c_:
        return c_.execute("SELECT 1 FROM iv_history WHERE ticker=? AND date=?", (ticker, date)).fetchone() is not None

def set_meta(k, v):
    with conn() as c_:
        c_.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, v))
