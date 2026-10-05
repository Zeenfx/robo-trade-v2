"""
Ponto de entrada principal do robô de opções.

Fluxo:
1. Executa o screener para identificar candidatos (|variação| >= 2.5%).
2. Para cada candidato, executa análise de cadeia de opções.
3. Aplica filtro de IV conforme horário definido.
4. Envia alertas via Telegram para oportunidades validadas.
"""

from __future__ import annotations

import logging
import schedule
import time
from datetime import datetime, timedelta

from config import INTERVALO_RODADA_MINUTOS
from screener import obter_candidatos_com_detalhes, AtivoInfo
from analyzer import analisar_cadeia
from iv_filter import aplicar_filtro_iv
from messenger import enviar_alerta

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)


def executar_rodada() -> None:
    """Executa uma rodada completa do robô."""
    logger.info("=== INÍCIO DA RODADA ===")
    logger.info(f"Horário: {datetime.now().isoformat()}")

    try:
        # 1. Screener
        candidatos: list[AtivoInfo] = obter_candidatos_com_detalhes()

        if not candidatos:
            logger.info("Nenhum candidato identificado nesta rodada.")
            logger.info("=== FIM DA RODADA ===")
            return

        logger.info(f"Candidatos identificados: {[c.ticker for c in candidatos]}")

        # 2. Análise de cadeia de opções para cada candidato
        oportunidades = []
        for candidato in candidatos:
            logger.info(f"Analisando cadeia de opções para {candidato.ticker}...")
            try:
            oportunidades_cadeia = analisar_cadeia(candidato)
                if oportunidades_cadeia:
                    oportunidades.extend(oportunidades_cadeia)
                    logger.info(f"{candidato.ticker}: {len(oportunidades_cadeia)} oportunidade(s) encontrada(s).")
                else:
                    logger.info(f"{candidato.ticker}: Nenhuma oportunidade válida na cadeia.")
            except Exception as e:
                logger.exception(f"[{candidato.ticker}] Erro ao analisar cadeia: {e}")

        if not oportunidades:
            logger.info("Nenhuma oportunidade válida encontrada nesta rodada.")
            logger.info("=== FIM DA RODADA ===")
            return

        # 3. Filtro de IV
        logger.info(f"Aplicando filtro de IV a {len(oportunidades)} oportunidade(s)...")
        oportunidades_filtradas = aplicar_filtro_iv(oportunidades)

        if not oportunidades_filtradas:
            logger.info("Nenhuma oportunidade aprovada no filtro de IV.")
            logger.info("=== FIM DA RODADA ===")
            return

        logger.info(f"Oportunidades aprovadas no filtro de IV: {len(oportunidades_filtradas)}")

        # 4. Envio de alertas
        for opp in oportunidades_filtradas:
            try:
                logger.info(f"Enviando alerta para {opp.ticker_base} - {opp.simbolo_opcao}...")
                enviar_alerta(opp)
                logger.info(f"Alerta enviado com sucesso para {opp.simbolo_opcao}.")
            except Exception as e:
                logger.exception(f"[{opp.simbolo_opcao}] Erro ao enviar alerta: {e}")

        logger.info("=== FIM DA RODADA ===")

    except Exception as e:
        logger.exception(f"Erro não tratado na rodada: {e}")
        logger.info("=== FIM DA RODADA (COM ERRO) ===")


def main() -> None:
    """Inicializa o agendamento e executa o robô."""
    logger.info("Iniciando robô de opções v2...")

    # Executa uma rodada imediatamente ao iniciar
    executar_rodada()

    # Agenda próximas rodadas
    intervalo = INTERVALO_RODADA_MINUTOS or 5
    schedule.every(intervalo).minutes.do(executar_rodada)

    logger.info(f"Robô agendado para rodar a cada {intervalo} minutos.")

    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
