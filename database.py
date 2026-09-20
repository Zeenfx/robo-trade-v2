"""Robô···Trade — persistencia em SQLite."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from analyzer import Analysis
from config import Config
from iv_filter import IVResult
from screener import Candidate


def init_database(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                ticker TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS analyses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                ticker TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS iv_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                ticker TEXT NOT NULL,
                iv_rank REAL,
                iv_percentile REAL,
                approved INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS setups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                ticker TEXT NOT NULL,
                analysis_payload TEXT NOT NULL,
                iv_payload TEXT NOT NULL,
                messages_sent INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sent_at TEXT NOT NULL,
                ticker TEXT NOT NULL,
                message_type TEXT NOT NULL,
                content TEXT NOT NULL
            )
            """
        )


def save_candidate(db_path: str, candidate: Candidate, status: str = "triado") -> None:
    created_at = datetime.now().isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO candidates (created_at, ticker, payload, status)
            VALUES (?, ?, ?, ?)
            """,
            (
                created_at,
                candidate.ticker,
                json.dumps(asdict(candidate), ensure_ascii=False),
                status,
            ),
        )


def save_analysis(db_path: str, analysis: Analysis) -> None:
    created_at = datetime.now().isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO analyses (created_at, ticker, payload)
            VALUES (?, ?, ?)
            """,
            (
                created_at,
                analysis.ticker,
                json.dumps(asdict(analysis), ensure_ascii=False),
            ),
        )


def save_iv_result(db_path: str, iv: IVResult) -> None:
    created_at = datetime.now().isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO iv_results (created_at, ticker, iv_rank, iv_percentile, approved)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                created_at,
                iv.ticker,
                iv.iv_rank,
                iv.iv_percentile,
                1 if iv.approved else 0,
            ),
        )


def save_setup(db_path: str, analysis: Analysis, iv: IVResult, messages_sent: bool) -> None:
    created_at = datetime.now().isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO setups (created_at, ticker, analysis_payload, iv_payload, messages_sent)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                created_at,
                analysis.ticker,
                json.dumps(asdict(analysis), ensure_ascii=False),
                json.dumps(asdict(iv), ensure_ascii=False),
                1 if messages_sent else 0,
            ),
        )


def save_message_log(
    db_path: str,
    ticker: str,
    message_type: str,
    content: str,
) -> None:
    sent_at = datetime.now().isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO messages (sent_at, ticker, message_type, content)
            VALUES (?, ?, ?, ?)
            """,
            (sent_at, ticker, message_type, content),
        )