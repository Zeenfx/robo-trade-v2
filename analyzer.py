"""Analisador de gatilhos do robô."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from screener import selecionar_opcao

logger = logging.getLogger(__name__)


@dataclass
class SinalAtivo:
    ticker: str
    direcao: str
    preco_entrada: float | None = None
    stop_loss: float | None = None
    alvo: float | None = None
    motivo: str = ""
    opcao: dict[str, Any] | None = None


def detectar_gatilho(ticker: str | None = None) -> SinalAtivo | None:
    """
    Usa o screener para encontrar uma opção com delta próximo do alvo.
    Se achar, retorna um SinalAtivo com os dados da operação simulada.
    """
    sel = selecionar_opcao(ticker)
    if not sel:
        return None

    opt = sel["option"]
    underlying = sel["underlying"]
    underlying_price = sel["underlying_price"]
    delta_est = sel["delta_est"]

    # Preço da opção (último ou bid/ask médio, se existir)
    preco_op = opt.get("lastPrice") or opt.get("regularMarketPrice")
    if preco_op is None:
        # fallback: usa último disponível ou pula
        logger.warning("Sem preço para a opção %s", opt.get("contractSymbol"))
        return None

    # Exemplo simples de stop e alvo em % configuráveis
    from config import ALVO_PCT, STOP_LOSS_PCT

    stop_loss = preco_op * (1 - STOP_LOSS_PCT)
    alvo = preco_op * (1 + ALVO_PCT)

    sinal = SinalAtivo(
        ticker=underlying,
        direcao="CALL",
        preco_entrada=preco_op,
        stop_loss=stop_loss,
        alvo=alvo,
        motivo=f"Delta≈{delta_est:.2f}, strike={opt.get('strike')}",
        opcao=opt,
    )
    logger.info(
        "Gatilho: %s CALL @ %.2f (stop=%.2f, alvo=%.2f) - %s",
        underlying,
        preco_op,
        stop_loss,
        alvo,
        sinal.motivo,
    )
    return sinal
