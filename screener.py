"""Triagem: so ativos da watchlist de IV; ranking de sinais."""
import config, database

def watchlist():
    d, rows = database.latest_watchlist()
    return d, {r["ticker"]: r for r in rows}

def rank_signals(signals):
    """Prioriza por participacao (VR) e depois proximidade do nivel. Limita a N por ciclo."""
    return sorted(signals, key=lambda s: s.vr, reverse=True)[:config.MAX_CANDIDATES_PER_CYCLE]
