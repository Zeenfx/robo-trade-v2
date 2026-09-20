"""Robô···Trade — orquestracao e agendamento.

Fluxo:
- Dia D (pregao): varredura → candidatos → análise D1/H1/M15 → registro interno.
- Dia D (pos-fechamento): atualiza opcoes → calcula IV → filtra.
- Dia D+1: se setup aprovado (tecnico + IV), envia as duas mensagens no Telegram.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

from analyzer import analyze_ticker
from config import Config
from data_fetcher import BrapiClient
from database import (
    init_database,
    save_analysis,
    save_candidate,
    save_iv_result,
    save_message_log,
    save_setup,
)
from iv_filter import check_iv
from messenger import send_message_1, send_message_2, send_setup_alerts
from screener import Candidate, screen_universe


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("robo_trade")


def run_intraday_scan(cfg: Config, client: BrapiClient) -> None:
    """Varredura intradiaria: triagem e análise, sem enviar mensagens."""
    log.info("Varredura intradiaria iniciada")

    candidates = screen_universe(client, cfg)

    for cand in candidates:
        save_candidate(cfg.db_path, cand, status="triado")
        analysis = analyze_ticker(cand.ticker, cand.quote, cand.history)
        save_analysis(cfg.db_path, analysis)
        log.info(
            "%s: candidato (score=%.1f) — %s",
            cand.ticker,
            cand.score,
            cand.reason,
        )

    log.info("Varredura intradiaria concluida")


def run_post_market(cfg: Config, client: BrapiClient) -> None:
    """Pos-fechamento: atualiza opcoes e calcula IV."""
    log.info("Processamento pos-fechamento iniciado")

    # Em producao, buscar todos os candidatos do dia no banco.
    # Aqui, simplificamos buscando tickers elegiveis novamente.
    tickers = client.list_eligible_tickers()

    for ticker in tickers:
        iv = check_iv(client, ticker, cfg)
        save_iv_result(cfg.db_path, iv)

    log.info("Processamento pos-fechamento concluido")


def run_next_day_dispatch(cfg: Config, client: BrapiClient) -> None:
    """Dia D+1: envia setups aprovados (tecnico + IV)."""
    log.info("Dispatch D+1 iniciado")

    # Em producao, consultar no banco os setups aprovados no dia anterior.
    # Aqui, simulamos buscando tickers e verificando IV novamente.
    tickers = client.list_eligible_tickers()

    for ticker in tickers:
        iv = check_iv(client, ticker, cfg)
        if not iv.approved:
            continue

        # Recria análise simplificada
        try:
            quote = client.get_quote(ticker)
            history = client.get_history(ticker, days=60)
        except Exception as exc:
            log.warning("%s: erro ao buscar dados para dispatch: %s", ticker, exc)
            continue

        analysis = analyze_ticker(ticker, quote, history)

        # Envia as duas mensagens
        send_setup_alerts(cfg, type("Setup", (), {"analysis": analysis, "iv": iv})())

        save_setup(cfg.db_path, analysis, iv, messages_sent=True)
        save_message_log(cfg.db_path, ticker, "aviso_curto", "enviado")
        save_message_log(cfg.db_path, ticker, "analise_completa", "enviado")

    log.info("Dispatch D+1 concluido")


def main() -> None:
    cfg = Config.from_env()
    init_database(cfg.db_path)
    client = BrapiClient(cfg)

    now = datetime.now(cfg.tz)
    log.info("Robô···Trade iniciado | agora=%s", now.isoformat())

    # Em producao, usar agendamento real (cron, loop, etc.).
    # Aqui, executamos conforme variavel de ambiente MODE.
    mode = os.getenv("MODE", "intraday").lower()

    if mode == "intraday":
        run_intraday_scan(cfg, client)
    elif mode == "post_market":
        run_post_market(cfg, client)
    elif mode == "dispatch":
        run_next_day_dispatch(cfg, client)
    else:
        log.warning("MODE desconhecido: %s", mode)


if __name__ == "__main__":
    main()