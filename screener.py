"""
Screener de ações para o robô de opções.

Regras:
- Calcula variação percentual intradiária: (preco_atual - fechamento_anterior) / fechamento_anterior * 100.
- Considera candidato qualquer ativo com |variação| >= 2.5%.
- Registra logs detalhados por ativo: ticker, preço atual, fechamento anterior, variação %, decisão e motivo.
- Não descarta candidato por IV antes do horário definido nas regras de filtro.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time
from typing import List, Optional, Tuple

from config import (
    ATIVOS,
    FECHAMENTO_ANTERIOR,
    PRECOS_ATUAIS,
    VOLUME_ATUAL,
    VOLUME_MEDIO,
)
from data_fetcher import get_previous_close, get_current_price, get_current_volume, get_average_volume

logger = logging.getLogger(__name__)


@dataclass
class AtivoInfo:
    ticker: str
    preco_atual: float
    fechamento_anterior: float
    variacao_pct: float
    volume_atual: float
    volume_medio: float
    eh_candidato: bool
    motivo: str


def calcular_variacao(preco_atual: float, fechamento_anterior: float) -> float:
    """Calcula variação percentual entre preço atual e fechamento anterior."""
    if fechamento_anterior <= 0:
        return 0.0
    return ((preco_atual - fechamento_anterior) / fechamento_anterior) * 100.0


def avaliar_ativo(ticker: str) -> Optional[AtivoInfo]:
    """
    Avalia um único ativo e retorna informações completas.
    """
    try:
        # Obtém dados de mercado
        preco_atual = get_current_price(ticker)
        fechamento_anterior = get_previous_close(ticker)
        volume_atual = get_current_volume(ticker)
        volume_medio = get_average_volume(ticker)

        if preco_atual is None or fechamento_anterior is None:
            logger.warning(f"[{ticker}] Dados incompletos: preco={preco_atual}, fechamento={fechamento_anterior}")
            return None

        variacao = calcular_variacao(preco_atual, fechamento_anterior)

        # Regra de candidato: |variação| >= 2.5%
        eh_candidato = abs(variacao) >= 2.5

        if eh_candidato:
            motivo = f"Variação absoluta >= 2.5% (atual: {variacao:.2f}%)"
        else:
            motivo = f"Variação abaixo do threshold (atual: {variacao:.2f}%, necessário: >= 2.5%)"

        info = AtivoInfo(
            ticker=ticker,
            preco_atual=preco_atual,
            fechamento_anterior=fechamento_anterior,
            variacao_pct=variacao,
            volume_atual=volume_atual or 0.0,
            volume_medio=volume_medio or 0.0,
            eh_candidato=eh_candidato,
            motivo=motivo,
        )

        logger.info(
            f"[{ticker}] preco={preco_atual:.2f} | fechamento={fechamento_anterior:.2f} | "
            f"variacao={variacao:.2f}% | candidato={eh_candidato} | {motivo}"
        )

        return info

    except Exception as e:
        logger.exception(f"[{ticker}] Erro ao avaliar ativo: {e}")
        return None


def screener() -> List[AtivoInfo]:
    """
    Executa o screener sobre todos os ativos configurados.
    Retorna lista de AtivoInfo (incluindo não-candidatos para log completo).
    """
    logger.info("=== INÍCIO DO SCREENER ===")

    candidatos: List[AtivoInfo] = []

    for ticker in ATIVOS:
        info = avaliar_ativo(ticker)
        if info is not None:
            candidatos.append(info)

    candidatos_reais = [a for a in candidatos if a.eh_candidato]
    nao_candidatos = [a for a in candidatos if not a.eh_candidato]

    logger.info(f"Total de ativos avaliados: {len(candidatos)}")
    logger.info(f"Candidatos identificados: {len(candidatos_reais)}")
    logger.info(f"Não-candidatos: {len(nao_candidatos)}")

    if candidatos_reais:
        tickers_candidatos = [a.ticker for a in candidatos_reais]
        logger.info(f"Candidatos: {tickers_candidatos}")
    else:
        logger.info("Nenhum candidato identificado nesta rodada.")

    logger.info("=== FIM DO SCREENER ===")

    return candidatos


def obter_candidatos_com_detalhes() -> List[AtivoInfo]:
    """
    Função auxiliar para main.py obter apenas os candidatos reais.
    """
    todos = screener()
    return [a for a in todos if a.eh_candidato]
