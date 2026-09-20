"""Robô···Trade — mensagens do Telegram e atalho Profit."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

from analyzer import Analysis
from config import Config
from iv_filter import IVResult


log = logging.getLogger("robo_trade")


@dataclass
class Setup:
    analysis: Analysis
    iv: IVResult


def build_profit_link(cfg: Config, ticker: str) -> str:
    """Constroi link ou comando de atalho Profit.
    
    Em producao, usar formato real do Profit (ex.: chart, book, etc.).
    """
    return f"{cfg.profit_base_url}/chart/{ticker}"


def send_telegram_message(
    cfg: Config,
    text: str,
) -> bool:
    if not cfg.telegram_token or not cfg.telegram_chat_id:
        log.warning("Telegram nao configurado")
        return False

    url = f"https://api.telegram.org/bot{cfg.telegram_token}/sendMessage"
    payload = {
        "chat_id": cfg.telegram_chat_id,
        "text": text,
        "parse_mode": "HTML",
    }

    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        log.info("Mensagem enviada ao Telegram")
        return True
    except requests.RequestException as exc:
        log.error("Erro ao enviar mensagem ao Telegram: %s", exc)
        return False


def send_message_1(cfg: Config, setup: Setup) -> None:
    """Mensagem 1 — aviso curto."""
    a = setup.analysis
    iv = setup.iv

    text = (
        f"<b>{a.ticker}</b> — análise prioritaria\n\n"
        f"Cenario: {a.bias}\n"
        f"Timeframe principal: D1/H1/M15\n"
        f"Status: IV aprovado (Rank {iv.iv_rank:.1f}%, Percentile {iv.iv_percentile:.1f}%)"
    )

    send_telegram_message(cfg, text)


def send_message_2(cfg: Config, setup: Setup) -> None:
    """Mensagem 2 — análise completa."""
    a = setup.analysis
    iv = setup.iv
    profit_link = build_profit_link(cfg, a.ticker)

    text = (
        f"<b>{a.ticker}</b> — análise completa\n\n"
        f"Resumo:\n"
        f"• Tendencia D1: {a.trend_d1}\n"
        f"• Estrutura H1: {a.structure_h1}\n"
        f"• Gatilho M15: {a.trigger_m15}\n\n"
        f"Regiao de interesse: {a.region_of_interest}\n"
        f"Invalidacao: {a.invalidation}\n"
        f"Alvo tecnico: {a.target}\n"
        f"Espaico tecnico: {a.technical_space}\n\n"
        f"IV:\n"
        f"• IV Rank: {iv.iv_rank:.1f}%\n"
        f"• IV Percentile: {iv.iv_percentile:.1f}%\n\n"
        f"Atalho Profit:\n"
        f"{profit_link}"
    )

    send_telegram_message(cfg, text)


def send_setup_alerts(cfg: Config, setup: Setup) -> None:
    """Envia as duas mensagens para um setup aprovado."""
    send_message_1(cfg, setup)
    send_message_2(cfg, setup)