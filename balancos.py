"""Calendario de balanços do 3T26 (datas de divulgacao; fontes: InfoMoney e B3, out/2026). So avisa; nao bloqueia nada.
ABEV3 e CSNA3 ficaram sem data publicada: cadastre pelo Telegram com 'balanco ABEV3 05/11'."""
import re
from datetime import date, datetime
import database

DATAS = {
    "BBDC4": "2026-10-22", "WEGE3": "2026-10-21", "GGBR4": "2026-10-26", "VALE3": "2026-10-29", "USIM5": "2026-10-30",
    "ITUB4": "2026-11-03", "RADL3": "2026-11-03", "PRIO3": "2026-11-04", "AXIA3": "2026-11-04",
    "MGLU3": "2026-11-05", "RENT3": "2026-11-05", "SUZB3": "2026-11-05", "SBSP3": "2026-11-05", "ALOS3": "2026-11-05", "LREN3": "2026-11-05",
    "ITSA4": "2026-11-09", "PETR4": "2026-11-10", "BPAC11": "2026-11-10", "EMBR3": "2026-11-10",
    "BBAS3": "2026-11-11", "ENEV3": "2026-11-11", "RAIL3": "2026-11-11", "EQTL3": "2026-11-11",
    "B3SA3": "2026-11-12", "CYRE3": "2026-11-12", "CMIG4": "2026-11-12", "CSAN3": "2026-11-13",
}


def data_de(ticker):
    """Data do proximo balanço conhecida (cadastro manual vale mais que a lista), ou None."""
    t = ticker.upper()
    try:
        v = database.get_meta("balanco:" + t)
    except Exception:
        v = None
    return v or DATAS.get(t)


def entre(ticker, ini, fim):
    """Data do balanço se cair entre ini e fim (inclusive); senao None."""
    d = data_de(ticker)
    if not d:
        return None
    dd = datetime.strptime(d, "%Y-%m-%d").date()
    return dd if ini <= dd <= fim else None


def cadastrar(ticker, texto, today):
    m = re.match(r"^(\d{1,2})/(\d{1,2})$", texto.strip())
    if not m:
        return "Use: balanco ABEV3 05/11"
    dia, mes = int(m.group(1)), int(m.group(2))
    ano = today.year if (mes, dia) >= (today.month, today.day) else today.year + 1
    try:
        d = date(ano, mes, dia)
    except ValueError:
        return "Data inválida. Use: balanco ABEV3 05/11"
    database.set_meta("balanco:" + ticker.upper(), str(d))
    return f"✅ Balanço de {ticker.upper()} anotado: {d.strftime('%d/%m')}."


def texto(ticker, today, fim):
    """Linha de aviso para o alerta, ou ''."""
    d = entre(ticker, today, fim)
    if not d:
        return ""
    if d == today:
        return f"⚠️ Balanço de {ticker} HOJE (confira o horário)"
    return f"⚠️ Balanço de {ticker} em {d.strftime('%d/%m')}, dentro do prazo"
