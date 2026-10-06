"""Analisador de gatilhos do robô."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class SinalAtivo:
    ticker: str
    direcao: str
    preco_entrada: float | None = None
    stop_loss: float | None = None
    alvo: float | None = None
    motivo: str = ""


def detectar_gatilho(*args: Any, **kwargs: Any) -> SinalAtivo | None:
    """
    Interface usada pelo main.py.

    Enquanto os dados técnicos não forem suficientes para emitir um sinal,
    retorna None sem derrubar o cron.
    """
    return None
