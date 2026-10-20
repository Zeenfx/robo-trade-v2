"""Persistencia SQLite."""
import sqlite3, json, os
from contextlib import contextmanager
import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS iv_history(ticker TEXT, date TEXT, iv REAL, expiration TEXT, PRIMARY KEY(ticker,date));
CREATE TABLE IF NOT EXISTS watchlist(date TEXT, ticker TEXT, iv REAL, iv_rank REAL, iv_pct REAL, PRIMARY KEY(date,ticker));
CREATE TABLE IF NOT EXISTS iv_status(date TEXT, ticker TEXT, iv REAL, iv_rank REAL, iv_pct REAL, approved INTEGER, note TEXT, PRIMARY KEY(date,ticker));
CREATE TABLE IF NOT EXISTS signals(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, ticker TEXT, direction TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS runs(job TEXT, date TEXT, PRIMARY KEY(job,date));
CREATE TABLE IF NOT EXISTS iv_nodata_v2(ticker TEXT, date TEXT, PRIMARY KEY(ticker,date));
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
