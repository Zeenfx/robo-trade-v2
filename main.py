"""Robô de trade de opções - Orquestração principal (v2.0)."""
import logging
from datetime import datetime, time
from typing import Dict, List, Optional

from config import (
    UNIVERSE,
    TIMEFRAME,
    IV_HISTORY_DAYS,
    IV_RANK_MAX_BUY,
    IV_PERCENTILE_MAX_BUY,
    LOOKBACK_ROMPIMENTO,
    VOLUME_MULT,
    EMA_SHORT,
    EMA_LONG,
    OPCAO_DELTA_MIN,
    OPCAO_DELTA_MAX,
    OPCAO_EXPIRY_MIN_DIAS,
    OPCAO_EXPIRY_MAX_DIAS,
    OPCAO_VOLUME_MIN_DIA,
    OPCAO_OPEN_INTEREST_MIN,
    MINUTOS_ENTRE_ALERTAS_SAME_TICKER,
    MINIMA_VARIACAO_PERCENTUAL_PARA_NOVO_ALERTA,
    MARKET_START,
    MARKET_END,
    ROBO_VERSION,
)
from data_fetcher import get_price_data, get_iv_history, get_opcoes_cadeia
from iv_filter import filtro_volatilidade
from analyzer import detectar_gatilho, SinalAtivo
from screener import selecionar_opcao
from messenger import formatar_mensagem, enviar_telegram

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


# Controle de ruído
ultimo_alerta: Dict[str, dict] = {}


def deve_enviar_alerta(ativo: str, sinal: str, preco_atual: float) -> bool:
    agora = datetime.now()
    if ativo in ultimo_alerta:
        ultimo = ultimo_alerta[ativo]
        if ultimo.get("sinal") == sinal:
            delta_minutos = (agora - ultimo["hora"]).total_seconds() / 60
            variacao_preco = abs((preco_atual / ultimo["preco"]) - 1) * 100
            if delta_minutos < MINUTOS_ENTRE_ALERTAS_SAME_TICKER and variacao_preco < MINIMA_VARIACAO_PERCENTUAL_PARA_NOVO_ALERTA:
                logger.info(f"{ativo}: alerta suprimido")
                return False
    return True


def atualizar_ultimo_alerta(ativo: str, sinal: str, preco: float):
    ultimo_alerta[ativo] = {"hora": datetime.now(), "sinal": sinal, "preco": preco}


def processar_ativo(ativo: str) -> Optional[dict]:
    logger.info(f"Processando {ativo}...")
    
    # Camada 1: IV
    try:
        iv_historico = get_iv_history(ativo, days=IV_HISTORY_DAYS)
        iv_atual = iv_historico[-1] if iv_historico else 0
    except Exception as e:
        logger.error(f"Erro IV {ativo}: {e}")
        return None
    
    aprovado_iv, iv_rank, iv_percentile = filtro_volatilidade(iv_atual, iv_historico, IV_RANK_MAX_BUY, IV_PERCENTILE_MAX_BUY)
    if not aprovado_iv:
        logger.info(f"{ativo}: reprovado IV")
        return None
    
    # Camada 2: Gatilho
    try:
        df = get_price_data(ativo, timeframe=TIMEFRAME)
    except Exception as e:
        logger.error(f"Erro preço {ativo}: {e}")
        return None
    
    if df is None or df.empty or len(df) < 50:
        logger.warning(f"{ativo}: dados insuficientes")
        return None
    
    analise = detectar_gatilho(df, lookback=LOOKBACK_ROMPIMENTO, volume_mult=VOLUME_MULT, ema_short=EMA_SHORT, ema_long=EMA_LONG)
    if analise.sinal == SinalAtivo.AGUARDAR:
        logger.info(f"{ativo}: AGUARDAR")
        return None
    
    # Camada 3: Opção
    try:
        cadeia_opcoes = get_opcoes_cadeia(ativo)
    except Exception as e:
        logger.error(f"Erro opções {ativo}: {e}")
        cadeia_opcoes = []
    
    opcao = None
    if cadeia_opcoes:
        opcao = selecionar_opcao(ativo=ativo, tipo_sinal=analise.sinal.value, preco_ativo=analise.preco_atual, cadeia_opcoes=cadeia_opcoes, delta_min=OPCAO_DELTA_MIN, delta_max=OPCAO_DELTA_MAX, expiry_min_dias=OPCAO_EXPIRY_MIN_DIAS, expiry_max_dias=OPCAO_EXPIRY_MAX_DIAS, volume_min_dia=OPCAO_VOLUME_MIN_DIA, open_interest_min=OPCAO_OPEN_INTEREST_MIN)
    
    if not deve_enviar_alerta(ativo, analise.sinal.value, analise.preco_atual):
        return None
    
    return {"ativo": ativo, "analise": analise, "iv_rank": iv_rank, "iv_percentile": iv_percentile, "opcao": opcao}


def main():
    logger.info(f"Robô de opções v{ROBO_VERSION} iniciado | agora={datetime.now().isoformat()}")
    agora = datetime.now().time()
    if not (MARKET_START <= agora <= MARKET_END):
        logger.info(f"Fora do horário de mercado")
        return
    
    logger.info("Varredura de opções iniciada")
    resultados = []
    for ativo in UNIVERSE:
        resultado = processar_ativo(ativo)
        if resultado:
            resultados.append(resultado)
    
    for res in resultados:
        mensagem = formatar_mensagem(ativo=res["ativo"], analise=res["analise"], iv_rank=res["iv_rank"], iv_percentile=res["iv_percentile"], opcao=res["opcao"])
        if enviar_telegram(mensagem):
            atualizar_ultimo_alerta(res["ativo"], res["analise"].sinal.value, res["analise"].preco_atual)
    
    logger.info(f"Varredura de opções concluída. {len(resultados)} oportunidade(s).")


if __name__ == "__main__":
    main()
