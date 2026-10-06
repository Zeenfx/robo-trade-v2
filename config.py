"""Configurações do robô de opções."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Config:
    BRAPI_TOKEN: str
    ATIVOS: list[str]
    UNIVERSE: list[str]
    DELTA_ALVO: float
    EXPIRACAO_DIAS: int
    LOTE: int
    LOTE_OPCOES: int
    STOP_LOSS_PCT: float
    ALVO_PCT: float


def load_config() -> Config:
    """Carrega configurações a partir de variáveis de ambiente."""
    ativos_str = os.getenv("ATIVOS", "PETR4;VALE3;BOVA11")
    ativos = [a.strip() for a in ativos_str.split(";") if a.strip()]

    # UNIVERSE = mesmos ativos por padrão; pode ser sobrescrito via env
    universe_str = os.getenv("UNIVERSE", ativos_str)
    universe = [a.strip() for a in universe_str.split(";") if a.strip()]

    return Config(
        BRAPI_TOKEN=os.getenv("BRAPI_TOKEN", ""),
        ATIVOS=ativos,
        UNIVERSE=universe,
        DELTA_ALVO=float(os.getenv("DELTA_ALVO", "0.3")),
        EXPIRACAO_DIAS=int(os.getenv("EXPIRACAO_DIAS", "30")),
        LOTE=int(os.getenv("LOTE", "100")),
        LOTE_OPCOES=int(os.getenv("LOTE_OPCOES", "1")),
        STOP_LOSS_PCT=float(os.getenv("STOP_LOSS_PCT", "0.3")),
        ALVO_PCT=float(os.getenv("ALVO_PCT", "0.5")),
    )


# Compatibilidade: exporta o que o main/screener importam
_cfg = load_config()
BRAPI_TOKEN = _cfg.BRAPI_TOKEN
ATIVOS = _cfg.ATIVOS
UNIVERSE = _cfg.UNIVERSE
DELTA_ALVO = _cfg.DELTA_ALVO
EXPIRACAO_DIAS = _cfg.EXPIRACAO_DIAS
LOTE = _cfg.LOTE
LOTE_OPCOES = _cfg.LOTE_OPCOES
STOP_LOSS_PCT = _cfg.STOP_LOSS_PCT
ALVO_PCT = _cfg.ALVO_PCT
