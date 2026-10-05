"""Envio de mensagens para o Telegram com formato de opções."""
import logging
from datetime import datetime
from typing import Optional

import requests

from config import TELEGRAM_CHAT_ID, TELEGRAM_TOKEN
from analyzer import ResultadoAnalise
from screener import OpcaoSelecionada

logger = logging.getLogger(__name__)


def formatar_mensagem(
    ativo: str,
    analise: ResultadoAnalise,
    iv_rank: float,
    iv_percentile: float,
    opcao: Optional[OpcaoSelecionada] = None,
) -> str:
    """
    Formata mensagem para Telegram.
    
    Formato CALL:
    PETR4 — OPORTUNIDADE CALL
    Preço PETR4: R$ 56,54
    Gatilho: rompeu máxima de 20 períodos com volume 1,8× a média.
    Tendência: preço > EMA20 > EMA50 | MACD subindo.
    IV Rank: 35% | IV Percentile: 32% (opção barata)
    
    Opção sugerida: PETRJ58 (call, strike 58, vencimento 16/10/2026)
    Delta estimado: ~0,45
    Preço da opção: R$ 1,10
    Entrada: até R$ 1,20
    Stop: abaixo de R$ 0,85
    Alvo 1: R$ 1,60
    Alvo 2: R$ 2,00
    Risco/Retorno: ~1:2,5
    
    Profit opção: https://profitchart.com.br/chart?ticker=PETRJ58&timeframe=5m
    """
    sinal = analise.sinal.value
    
    # Cabeçalho
    if sinal == "CALL":
        titulo = f"{ativo} — OPORTUNIDADE CALL"
    elif sinal == "PUT":
        titulo = f"{ativo} — OPORTUNIDADE PUT"
    else:
        titulo = f"{ativo} — AGUARDAR"
    
    linhas = [titulo, ""]
    
    # Preço e gatilho
    linhas.append(f"Preço {ativo}: R$ {analise.preco_atual:.2f}")
    linhas.append(f"Gatilho: {analise.motivo}.")
    linhas.append(f"Tendência: {analise.tendencia} | momentum {analise.momentum}.")
    linhas.append(f"IV Rank: {iv_rank:.0f}% | IV Percentile: {iv_percentile:.0f}% {'(opção barata)' if iv_rank <= 40 and iv_percentile <= 40 else ''}")
    
    # Opção (se houver)
    if opcao and sinal in ["CALL", "PUT"]:
        linhas.append("")
        linhas.append(f"Opção sugerida: {opcao.ticker} ({opcao.tipo}, strike {opcao.strike:.2f}, vencimento {opcao.vencimento.strftime('%d/%m/%Y')})")
        linhas.append(f"Delta estimado: ~{opcao.delta:.2f}")
        linhas.append(f"Preço da opção: R$ {opcao.preco:.2f}")
        
        # Calcular entrada, stop, alvos
        entrada = opcao.preco * 1.10  # até 10% acima
        stop = opcao.preco * 0.75     # 25% abaixo
        alvo1 = opcao.preco * 1.45    # 45% acima
        alvo2 = opcao.preco * 1.80    # 80% acima
        
        linhas.append(f"Entrada: até R$ {entrada:.2f}")
        linhas.append(f"Stop: abaixo de R$ {stop:.2f}")
        linhas.append(f"Alvo 1: R$ {alvo1:.2f}")
        linhas.append(f"Alvo 2: R$ {alvo2:.2f}")
        
        risco = opcao.preco - stop
        retorno = alvo1 - opcao.preco
        rr = retorno / risco if risco else 0
        linhas.append(f"Risco/Retorno: ~1:{rr:.1f}")
        
        # Link do Profit da OPÇÃO (não do ativo)
        linhas.append("")
        timeframe = "5m"
        link_profit = f"https://profitchart.com.br/chart?ticker={opcao.ticker}&timeframe={timeframe}"
        linhas.append(f"Profit opção: {link_profit}")
    else:
        # Sem opção: link do ativo mesmo assim
        linhas.append("")
        timeframe = "5m"
        link_profit = f"https://profitchart.com.br/chart?ticker={ativo}&timeframe={timeframe}"
        linhas.append(f"Profit ativo: {link_profit}")
    
    return "\n".join(linhas)


def enviar_telegram(mensagem: str) -> bool:
    """Envia mensagem para o Telegram."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID não configurados")
        return False
    
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mensagem,
        "parse_mode": "HTML",
    }
    
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        logger.info("Mensagem enviada ao Telegram")
        return True
    except Exception as e:
        logger.error(f"Erro ao enviar mensagem ao Telegram: {e}")
        return False
