"""Cliente simples para enviar mensagens ao Telegram."""

from __future__ import annotations

import logging
import os

import requests

from analyzer import SinalAtivo

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


def enviar_sinal(sinal: SinalAtivo) -> bool:
    """Envia mensagem de sinal para o Telegram. Retorna True se sucesso."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram: token ou chat_id não configurados.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    texto = (
        f"🔔 *Sinal de Opção*\n"
        f"Ativo: {sinal.ticker}\n"
        f"Direção: {sinal.direcao}\n"
        f"Entrada: {sinal.preco_entrada:.3f}\n"
        f"Stop: {sinal.stop_loss:.3f}\n"
        f"Alvo: {sinal.alvo:.3f}\n"
        f"Motivo: {sinal.motivo}\n"
    )
    if sinal.opcao:
        opt = sinal.opcao
        texto += (
            f"Opção: {opt.get('contractSymbol', 'N/A')}\n"
            f"Strike: {opt.get('strike', 'N/A')}\n"
            f"Last: {opt.get('lastPrice', 'N/A')}\n"
        )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": texto,
        "parse_mode": "Markdown",
    }

    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        logger.info("Telegram: sinal enviado com sucesso.")
        return True
    except Exception as e:
        logger.error("Telegram: erro ao enviar sinal: %s", e)
        return False
