"""Ponto de entrada do robô de opções (v3 – com Telegram)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from analyzer import SinalAtivo, detectar_gatilho
from config import (
    ATIVOS,
    ALVO_PCT,
    BRAPI_TOKEN,
    DELTA_ALVO,
    EXPIRACAO_DIAS,
    HORARIO_FIM,
    HORARIO_INICIO,
    INTERVALO_MIN,
    LOTE,
    LOTE_OPCOES,
    MODO,
    STOP_LOSS_PCT,
    TESTE_MODE,
    UNIVERSE,
)
from telegram_client import enviar_sinal

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> None:
    logger.info("Robô de opções iniciado (v3).")
    logger.info("Horário UTC: %s", datetime.now(timezone.utc).isoformat())
    logger.info("MODO=%s, TESTE_MODE=%s", MODO, TESTE_MODE)
    logger.info("ATIVOS=%s", ATIVOS)
    logger.info("UNIVERSE=%s", UNIVERSE)
    logger.info("DELTA_ALVO=%s, EXPIRACAO_DIAS=%s", DELTA_ALVO, EXPIRACAO_DIAS)
    logger.info("LOTE=%s, LOTE_OPCOES=%s", LOTE, LOTE_OPCOES)
    logger.info("STOP_LOSS_PCT=%s, ALVO_PCT=%s", STOP_LOSS_PCT, ALVO_PCT)
    logger.info("HORARIO_INICIO=%s, HORARIO_FIM=%s, INTERVALO_MIN=%s", HORARIO_INICIO, HORARIO_FIM, INTERVALO_MIN)
    logger.info("BRAPI_TOKEN configurado=%s", bool(BRAPI_TOKEN))

    ticker = ATIVOS[0] if ATIVOS else None
    logger.info("Analisando gatilho para %s", ticker)
    sinal: SinalAtivo | None = detectar_gatilho(ticker)

    if sinal:
        logger.info(
            "SINAL: %s %s @ %.2f (stop=%.2f, alvo=%.2f) - %s",
            sinal.ticker,
            sinal.direcao,
            sinal.preco_entrada or 0,
            sinal.stop_loss or 0,
            sinal.alvo or 0,
            sinal.motivo,
        )
        if sinal.opcao:
            logger.info(
                "Opção: %s, strike=%.2f, lastPrice=%.2f",
                sinal.opcao.get("contractSymbol"),
                sinal.opcao.get("strike"),
                sinal.opcao.get("lastPrice"),
            )
        logger.info("Enviando sinal para o Telegram...")
        enviar_sinal(sinal)
    else:
        logger.info("Nenhum gatilho encontrado para %s nesta execução.", ticker)

    logger.info("Fim da execução (v3).")


if __name__ == "__main__":
    main()
