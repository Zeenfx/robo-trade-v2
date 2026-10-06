"""Ponto de entrada do robô de opções (versão mínima, só para validar deploy)."""

from __future__ import annotations

import logging
from datetime import datetime

from analyzer import SinalAtivo

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> None:
    logger.info("Robô iniciado (versão mínima de validação).")
    logger.info("Horário: %s", datetime.utcnow().isoformat() + "Z")
    logger.info("Nenhum gatilho será emitido nesta versão.")


if __name__ == "__main__":
    main()
