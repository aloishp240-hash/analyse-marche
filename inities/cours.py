"""Cours journaliers et nombre d'actions (yfinance), gardés sur disque pour ne pas les redemander."""
import json
import math
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

SEANCES_3M = 63
SEANCES_6M = 126
COLONNES = ["Open", "High", "Low", "Close", "Volume"]


def _fichier(dossier, ticker):
    return Path(dossier) / f"cours-{re.sub(r'[^A-Za-z0-9._-]', '_', ticker)}.pkl"


def _lire_cache(dossier, ticker):
    """Entrée {"debut", "fin", "cours"} du cache, ou None si elle manque ou est illisible."""
    fichier = _fichier(dossier, ticker)
    if not fichier.exists():
        return None
    try:
        entree = pd.read_pickle(fichier)
        if isinstance(entree["cours"], pd.DataFrame) and entree["debut"] and entree["fin"]:
            return entree
    except Exception:
        pass
    fichier.unlink(missing_ok=True)
    return None


def _nettoyer(df):
    """Les cinq colonnes, index de dates sans heure ni fuseau, séances sans clôture écartées ; None si rien."""
    if df is None or any(c not in df.columns for c in COLONNES):
        return None
    df = df[COLONNES].dropna(subset=["Close"])
    index = pd.DatetimeIndex(df.index)
    if index.tz is not None:
        index = index.tz_localize(None)
    df = df.set_axis(index.normalize())
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return None if df.empty else df


def _extraire(brut, ticker, seul):
    if not isinstance(brut.columns, pd.MultiIndex):
        return brut if seul else None
    for niveau in range(brut.columns.nlevels):
        if ticker in brut.columns.get_level_values(niveau):
            return brut.xs(ticker, axis=1, level=niveau)
    return None


def _telecharger(tickers, debut, fin):
    try:
        brut = yf.download(tickers, start=debut.isoformat(), end=(fin + timedelta(days=1)).isoformat(),
                           auto_adjust=True, progress=False, threads=True, group_by="ticker")
    except Exception:
        return {}
    if brut is None or brut.empty:
        return {}
    return {t: df for t in tickers if (df := _nettoyer(_extraire(brut, t, len(tickers) == 1))) is not None}


def charger(tickers, debut, fin, dossier):
    """Cours ajustés (dividendes et divisions) de chaque ticker entre debut et fin ; absent du dict s'il n'a pas de données.

    Une période déjà en cache n'est pas redemandée ; un ticker sans données n'est pas mis en cache."""
    dossier = Path(dossier)
    tickers = list(dict.fromkeys(tickers))
    resultat, manquants = {}, []
    for ticker in tickers:
        entree = _lire_cache(dossier, ticker)
        if entree is not None and entree["debut"] <= debut and entree["fin"] >= fin:
            resultat[ticker] = entree["cours"]
        else:
            manquants.append(ticker)
    if manquants:
        dossier.mkdir(parents=True, exist_ok=True)
        for ticker, df in _telecharger(manquants, debut, fin).items():
            pd.to_pickle({"debut": debut, "fin": fin, "cours": df}, _fichier(dossier, ticker))
            resultat[ticker] = df
    coupe = {t: resultat[t].loc[pd.Timestamp(debut):pd.Timestamp(fin)] for t in tickers if t in resultat}
    return {t: df for t, df in coupe.items() if not df.empty}


def _actions(ticker):
    try:
        valeur = float(yf.Ticker(ticker).fast_info["shares"])
    except Exception:
        return None
    return valeur if math.isfinite(valeur) and valeur > 0 else None


def actions_en_circulation(tickers, dossier):
    """Nombre d'actions actuel de chaque ticker (yfinance) ; absent du dict si la valeur manque.

    C'est une valeur d'aujourd'hui, donc la seule donnée de la mesure qui ne date pas de la publication."""
    fichier = Path(dossier) / "actions.json"
    try:
        connues = json.loads(fichier.read_text())
        if not isinstance(connues, dict):
            raise ValueError
    except (OSError, ValueError):
        connues = {}
    tickers = list(dict.fromkeys(tickers))
    manquants = [t for t in tickers if t not in connues]
    if manquants:
        with ThreadPoolExecutor(8) as pool:
            for ticker, valeur in zip(manquants, pool.map(_actions, manquants)):
                if valeur is not None:
                    connues[ticker] = valeur
        Path(dossier).mkdir(parents=True, exist_ok=True)
        fichier.write_text(json.dumps(connues, indent=1))
    return {t: connues[t] for t in tickers if t in connues}
