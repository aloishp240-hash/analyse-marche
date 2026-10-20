"""Repérage des candidats court terme et calcul de leurs niveaux (entrée, stop, objectif)."""
import math
from datetime import date, timedelta

import numpy as np
import pandas as pd

from commun import INDICE, atr_pct, rsi

SEANCES_3_MOIS = 63


def _perf(clotures, seances=SEANCES_3_MOIS):
    return (clotures.iloc[-1] / clotures.iloc[-seances - 1] - 1) * 100


def indicateurs(hauts, bas, clotures, volumes, clotures_indice):
    """Indicateurs de la dernière séance, ou None si l'historique est trop court ou incomplet."""
    df = pd.concat({"h": hauts, "b": bas, "c": clotures, "v": volumes}, axis=1).dropna()
    indice = clotures_indice.dropna() if clotures_indice is not None else pd.Series(dtype=float)
    if len(df) < 200 or len(indice) <= SEANCES_3_MOIS:
        return None
    c = df["c"]
    ind = {
        "cloture": float(c.iloc[-1]),
        "veille": float(c.iloc[-2]),
        "mm20": float(c.iloc[-20:].mean()),
        "mm50": float(c.iloc[-50:].mean()),
        "mm200": float(c.iloc[-200:].mean()),
        "rsi": float(rsi(c)),
        "atr": float(atr_pct(df["h"], df["b"], c) * c.iloc[-1] / 100),
        "perf_3m": float(_perf(c)),
        "perf_indice_3m": float(_perf(indice)),
        "plus_haut_20": float(df["h"].iloc[-21:-1].max()),
        "volume": float(df["v"].iloc[-1]),
        "volume_moyen_20": float(df["v"].iloc[-21:-1].mean()),
    }
    if any(math.isnan(v) for v in ind.values()):
        return None
    return ind


def configuration_a(ind, p):
    """Repli dans une tendance haussière."""
    proche = min(abs(ind["cloture"] - ind["mm20"]), abs(ind["cloture"] - ind["mm50"]))
    return (ind["cloture"] > ind["mm50"] > ind["mm200"]
            and ind["perf_3m"] > ind["perf_indice_3m"]
            and p["rsi_a_min"] <= ind["rsi"] <= p["rsi_a_max"]
            and proche <= p["proximite_mm_atr"] * ind["atr"])


def configuration_b(ind, p):
    """Cassure du plus haut des 20 séances précédentes sur volume."""
    return (ind["cloture"] > ind["plus_haut_20"]
            and ind["volume"] >= p["volume_cassure"] * ind["volume_moyen_20"]
            and ind["rsi"] < p["rsi_b_max"])


def seances_avant(jour, date_resultats):
    """Jours ouvrés dans ]jour, date_resultats] (jours fériés ignorés)."""
    return max(0, int(np.busday_count(jour + timedelta(days=1), date_resultats + timedelta(days=1))))


def exclu(ind, jour, date_resultats, p):
    """Motif d'exclusion, ou None si le titre reste éligible."""
    if date_resultats is not None and date_resultats >= jour \
            and seances_avant(jour, date_resultats) <= p["seances_sans_resultats"]:
        return f"résultats le {date_resultats}"
    if abs(ind["cloture"] - ind["veille"]) > p["variation_max_atr"] * ind["atr"]:
        return "mouvement du jour supérieur à 2 ATR"
    return None


def niveaux(cloture, atr, p):
    entree = round(cloture - p["entree_atr"] * atr, 2)
    return {"entree": entree,
            "stop": round(entree - p["stop_atr"] * atr, 2),
            "objectif": round(entree + p["objectif_atr"] * atr, 2)}


def _date_resultats(resultats, ticker):
    texte = (resultats.get(ticker) or {}).get("date")
    try:
        return date.fromisoformat(str(texte)[:10]) if texte else None
    except ValueError:
        return None


def _indicateurs_titre(hist, ticker, indice):
    try:
        return indicateurs(hist["High"][ticker], hist["Low"][ticker], hist["Close"][ticker],
                           hist["Volume"][ticker], indice)
    except KeyError:
        return None


def reperer(hist, tickers, resultats, jour, p, deja_pris):
    """Candidats du jour, triés par force relative décroissante (perf 3 mois du titre − celle de QQQ)."""
    indice = None
    if not hist.empty:
        hist = hist[hist.index.date <= jour]
        indice = hist["Close"].get(INDICE)
    candidats = []
    for ticker in tickers:
        if ticker in deja_pris or ticker == INDICE:
            continue
        ind = _indicateurs_titre(hist, ticker, indice)
        if ind is None or exclu(ind, jour, _date_resultats(resultats, ticker), p):
            continue
        if p["configurations"].get("A") and configuration_a(ind, p):
            configuration = "A"
        elif p["configurations"].get("B") and configuration_b(ind, p):
            configuration = "B"
        else:
            continue
        candidats.append({
            "ticker": ticker, "configuration": configuration,
            "cloture": round(ind["cloture"], 2), "atr": round(ind["atr"], 2), "rsi": round(ind["rsi"], 1),
            "force_relative": round(ind["perf_3m"] - ind["perf_indice_3m"], 2),
            **niveaux(ind["cloture"], ind["atr"], p),
        })
    candidats.sort(key=lambda c: c["force_relative"], reverse=True)
    return candidats[:p["candidats_max"]]
