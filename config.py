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
    INTERVALO_MIN: int
    HORARIO_INICIO: str
    HORARIO_FIM: str
    MODO: str
    TESTE_MODE: bool


def load_config() -> Config:
    """Carrega configurações a partir de variáveis de ambiente."""
    ativos_str = os.getenv("ATIVOS", "PETR4;VALE3;BOVA11")
    ativos = [a.strip() for a in ativos_str.split(";") if a.strip()]

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
        INTERVALO_MIN=int(os.getenv("INTERVALO_MIN", "5")),
        HORARIO_INICIO=os.getenv("HORARIO_INICIO", "10:00"),
        HORARIO_FIM=os.getenv("HORARIO_FIM", "17:00"),
        MODO=os.getenv("MODO", "opcoes"),
        TESTE_MODE=os.getenv("TESTE_MODE", "false").lower() == "true",
    )


# Compatibilidade: exporta tudo que main.py e screener.py importam
_cfg = load_config()

# Tokens / chaves
BRAPI_TOKEN = _cfg.BRAPI_TOKEN

# Listas de ativos
ATIVOS = _cfg.ATIVOS
UNIVERSE = _cfg.UNIVERSE

# Parâmetros de operação
DELTA_ALVO = _cfg.DELTA_ALVO
EXPIRACAO_DIAS = _cfg.EXPIRACAO_DIAS
LOTE = _cfg.LOTE
LOTE_OPCOES = _cfg.LOTE_OPCOES
STOP_LOSS_PCT = _cfg.STOP_LOSS_PCT
ALVO_PCT = _cfg.ALVO_PCT

# Tempo e horário
INTERVALO_MIN = _cfg.INTERVALO_MIN
HORARIO_INICIO = _cfg.HORARIO_INICIO
HORARIO_FIM = _cfg.HORARIO_FIM

# Modo de operação
MODO = _cfg.MODO
TESTE_MODE = _cfg.TESTE_MODE
