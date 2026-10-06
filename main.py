"""Ponto de entrada do robô de opções (v1 – valida configs)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> None:
    logger.info("Robô de opções iniciado.")
    logger.info("Horário UTC: %s", datetime.now(timezone.utc).isoformat())
    logger.info("MODO=%s, TESTE_MODE=%s", MODO, TESTE_MODE)
    logger.info("ATIVOS=%s", ATIVOS)
    logger.info("UNIVERSE=%s", UNIVERSE)
    logger.info("DELTA_ALVO=%s, EXPIRACAO_DIAS=%s", DELTA_ALVO, EXPIRACAO_DIAS)
    logger.info("LOTE=%s, LOTE_OPCOES=%s", LOTE, LOTE_OPCOES)
    logger.info("STOP_LOSS_PCT=%s, ALVO_PCT=%s", STOP_LOSS_PCT, ALVO_PCT)
    logger.info("HORARIO_INICIO=%s, HORARIO_FIM=%s, INTERVALO_MIN=%s", HORARIO_INICIO, HORARIO_FIM, INTERVALO_MIN)
    logger.info("BRAPI_TOKEN configurado=%s", bool(BRAPI_TOKEN))
    logger.info("Fim da execução (v1).")


if __name__ == "__main__":
    main()
