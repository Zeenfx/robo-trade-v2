from dataclasses import dataclass
from datetime import datetime


@dataclass
class OpcaoSelecionada:
    ticker: str
    tipo: str
    strike: float
    vencimento: datetime
    delta: float
    preco: float
    volume_dia: int
    open_interest: int
    dias_expiracao: int


def selecionar_opcao(ativo, tipo_sinal, preco_ativo, cadeia_opcoes, delta_min=0.35, delta_max=0.55, expiry_min_dias=14, expiry_max_dias=42, volume_min_dia=1, open_interest_min=0):
    hoje = datetime.now().date()
    tipo = "call" if tipo_sinal == "CALL" else "put"
    candidatas = []
    for item in cadeia_opcoes:
        if item.get("tipo", "").lower() != tipo:
            continue
        try:
            vencimento = datetime.fromisoformat(str(item["vencimento"]).replace("Z", "+00:00")).date()
        except ValueError:
            try:
                vencimento = datetime.strptime(str(item["vencimento"]), "%Y-%m-%d").date()
            except ValueError:
                continue
        dias = (vencimento - hoje).days
        delta = abs(float(item.get("delta", 0)))
        strike = float(item.get("strike", 0))
        if not (expiry_min_dias <= dias <= expiry_max_dias and delta_min <= delta <= delta_max):
            continue
        if int(item.get("volume_dia", 0)) < volume_min_dia or int(item.get("open_interest", 0)) < open_interest_min:
            continue
        if tipo == "call" and not (preco_ativo <= strike <= preco_ativo * 1.05):
            continue
        if tipo == "put" and not (preco_ativo * 0.95 <= strike <= preco_ativo):
            continue
        candidatas.append(OpcaoSelecionada(item.get("ticker", ""), tipo, strike, vencimento, delta, float(item.get("preco", 0)), int(item.get("volume_dia", 0)), int(item.get("open_interest", 0)), dias))
    return max(candidatas, key=lambda x: (x.open_interest, x.volume_dia), default=None)
