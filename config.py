"""Robô···Trade — configuração por variaveis de ambiente."""

from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo


@dataclass
class Config:
    # Brapi
    brapi_base_url: str
    brapi_token: str

    # Telegram
    telegram_token: str
    telegram_chat_id: str

    # Universo e limites
    max_candidates_per_cycle: int
    price_change_pct: float
    gap_pct: float
    range_multiplier: float
    volume_multiplier: float

    # IV
    iv_rank_max: float
    iv_percentile_max: float

    # Horarios
    tz: ZoneInfo
    pre_market_run: bool
    intraday_interval_minutes: int
    post_market_run: bool

    # Database
    db_path: str

    # Profit
    profit_base_url: str

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            brapi_base_url=os.getenv("BRAPI_BASE_URL", "https://brapi.dev/api"),
            brapi_token=os.getenv("BRAPI_TOKEN", ""),
            telegram_token=os.getenv("TELEGRAM_TOKEN", ""),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
            max_candidates_per_cycle=int(os.getenv("MAX_CANDIDATES_PER_CYCLE", "5")),
            price_change_pct=float(os.getenv("PRICE_CHANGE_PCT", "2.5")),
            gap_pct=float(os.getenv("GAP_PCT", "1.5")),
            range_multiplier=float(os.getenv("RANGE_MULTIPLIER", "1.8")),
            volume_multiplier=float(os.getenv("VOLUME_MULTIPLIER", "1.5")),
            iv_rank_max=float(os.getenv("IV_RANK_MAX", "25")),
            iv_percentile_max=float(os.getenv("IV_PERCENTILE_MAX", "30")),
            tz=ZoneInfo("America/Sao_Paulo"),
            pre_market_run=os.getenv("PRE_MARKET_RUN", "true").lower() == "true",
            intraday_interval_minutes=int(os.getenv("INTRADAY_INTERVAL_MINUTES", "15")),
            post_market_run=os.getenv("POST_MARKET_RUN", "true").lower() == "true",
            db_path=os.getenv("DB_PATH", "robo_trade.db"),
            profit_base_url=os.getenv("PROFIT_BASE_URL", "https://profit.profit.com.br"),
        )