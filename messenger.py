import logging
import requests
from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)


def formatar_mensagem(ativo, analise, iv_rank, iv_percentile, opcao=None):
    if opcao is None:
        return ""
    titulo = f"{ativo} — OPORTUNIDADE {analise.sinal.value}"
    entrada = opcao.preco * 1.10
    stop = opcao.preco * 0.75
    alvo1 = opcao.preco * 1.45
    alvo2 = opcao.preco * 1.80
    link = f"https://profitchart.com.br/chart?ticker={opcao.ticker}&timeframe=5m"
    return "\n".join([titulo, "", f"Preço {ativo}: R$ {analise.preco_atual:.2f}", f"Gatilho: {analise.motivo}.", f"IV Rank: {iv_rank:.0f}% | IV Percentile: {iv_percentile:.0f}%", "", f"Opção sugerida: {opcao.ticker} ({opcao.tipo}, strike R$ {opcao.strike:.2f}, vencimento {opcao.vencimento.strftime('%d/%m/%Y')})", f"Delta: {opcao.delta:.2f}", f"Preço da opção: R$ {opcao.preco:.2f}", f"Entrada: até R$ {entrada:.2f}", f"Stop: abaixo de R$ {stop:.2f}", f"Alvo 1: R$ {alvo1:.2f}", f"Alvo 2: R$ {alvo2:.2f}", "", f"Profit opção: {link}"])


def enviar_telegram(mensagem):
    if not mensagem:
        return False
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID ausentes")
        return False
    try:
        resposta = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": TELEGRAM_CHAT_ID, "text": mensagem}, timeout=20)
        resposta.raise_for_status()
        logger.info("Mensagem enviada ao Telegram")
        return True
    except Exception as exc:
        logger.error("Erro ao enviar Telegram: %s", exc)
        return False
