"""Cours journaliers et nombre d'actions (yfinance), gardés sur disque pour ne pas les redemander."""
import ast
import json
import logging
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

SEANCES_3M = 63
SEANCES_6M = 126
COLONNES = ["Open", "High", "Low", "Close", "Volume"]
# Yahoo limite le débit : petits lots, pauses, et nouvelles tentatives après une attente de plus en plus longue.
LOT_COURS = 50
FILS_COURS = 2
PAUSE_LOTS = 2.0
ATTENTES = (30, 60, 120)  # secondes avant chacune des 3 nouvelles tentatives d'un lot
LOT_ACTIONS = 20
FILS_ACTIONS = 2
PAUSE_ACTIONS = 1.0


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


def _nom_yahoo(ticker):
    """Yahoo écrit le point de classe avec un tiret (BRK.B → BRK-B) ; partout ailleurs on garde le ticker d'origine."""
    return ticker.replace(".", "-")


def _est_limite(texte):
    texte = str(texte).lower().replace(" ", "")
    return "ratelimit" in texte or "toomanyrequests" in texte


class _GuetteLimite(logging.Handler):
    """Repère, pendant un yf.download, les messages de limite de débit du logger « yfinance ». Avec yfinance 1.x,
    download ne lève rien dans ce cas : il renvoie un tableau vide et journalise `['AAA', 'BBB']: YFRateLimitError(...)`."""

    def __init__(self, fil=None):
        super().__init__(level=logging.WARNING)
        self.noms, self.tout, self.fil = set(), False, fil

    def emit(self, record):
        if self.fil is not None and record.thread != self.fil:
            return  # message d'un autre fil
        try:
            message = record.getMessage()
        except Exception:
            return
        # yfinance prévient aussi « Crumb fetch rate-limited ..., continuing without crumb » puis réussit : pas une limite.
        if not _est_limite(message) or "continuing without crumb" in message:
            return
        debut = re.match(r"\s*(\[[^\]]*\])\s*:", message)
        try:
            noms = ast.literal_eval(debut.group(1)) if debut else None
        except (ValueError, SyntaxError):
            noms = None
        if isinstance(noms, list) and all(isinstance(n, str) for n in noms):
            self.noms.update(n.upper() for n in noms)
        else:
            self.tout = True  # message de limite sans liste de titres : tout ce qui manque est suspect


def _essai(tickers, debut, fin):
    """(cours trouvés, tickers dont Yahoo a limité le débit). Une exception compte pour tous les tickers ; sinon la
    limite est lue dans les messages du logger yfinance, car download ne la lève pas."""
    noms = list(dict.fromkeys(_nom_yahoo(t) for t in tickers))
    guet, journal = _GuetteLimite(), logging.getLogger("yfinance")
    journal.addHandler(guet)
    try:
        brut = yf.download(noms, start=debut.isoformat(), end=(fin + timedelta(days=1)).isoformat(),
                           auto_adjust=True, progress=False, threads=FILS_COURS, group_by="ticker")
    except Exception:
        return {}, set(tickers)
    finally:
        journal.removeHandler(guet)
    chez_yahoo = {}
    if brut is not None and not brut.empty:
        chez_yahoo = {n: df for n in noms if (df := _nettoyer(_extraire(brut, n, len(noms) == 1))) is not None}
    trouves = {t: chez_yahoo[_nom_yahoo(t)] for t in tickers if _nom_yahoo(t) in chez_yahoo}
    limites = {t for t in tickers if t not in trouves and (guet.tout or _nom_yahoo(t).upper() in guet.noms)}
    return trouves, limites


def _telecharger(tickers, debut, fin, attendre=None):
    """(cours des tickers qui ont des données, tickers toujours limités par Yahoo). Ce qui a été limité est redemandé
    après 30, 60 puis 120 secondes ; un ticker sans données reste simplement absent, sans exception."""
    attendre = attendre or time.sleep
    resultat, a_faire = {}, list(tickers)
    for tentative in range(len(ATTENTES) + 1):
        trouves, limites = _essai(a_faire, debut, fin)
        resultat.update(trouves)
        a_faire = [t for t in a_faire if t in limites]
        if not a_faire or tentative == len(ATTENTES):
            break
        attendre(ATTENTES[tentative])
    return resultat, set(a_faire)


def charger(tickers, debut, fin, dossier, attendre=None, taille_lot=LOT_COURS, progres=None, limites=None):
    """Cours ajustés (dividendes et divisions) de chaque ticker entre debut et fin ; absent du dict s'il n'a pas de données.

    Une période déjà en cache n'est pas redemandée ; un ticker sans données n'est pas mis en cache. Les titres
    manquants sont demandés par lots de `taille_lot`, avec une pause entre les lots, et chaque lot est mis en cache dès
    qu'il arrive : un arrêt en cours de route ne perd pas les lots déjà reçus. `attendre(secondes)` remplace
    time.sleep dans les tests ; `progres(lots faits, nombre de lots)` est appelé après chaque lot. Les titres que Yahoo
    limite encore après les 3 reprises sont absents du résultat ET ajoutés à l'ensemble `limites` (s'il est donné) :
    c'est différent d'un titre pour lequel Yahoo n'a simplement pas de données."""
    attendre = attendre or time.sleep
    dossier = Path(dossier)
    tickers = list(dict.fromkeys(tickers))
    resultat, manquants = {}, []
    for ticker in tickers:
        entree = _lire_cache(dossier, ticker)
        if entree is not None and entree["debut"] <= debut and entree["fin"] >= fin:
            resultat[ticker] = entree["cours"]
        else:
            manquants.append(ticker)
    lots = [manquants[i:i + taille_lot] for i in range(0, len(manquants), taille_lot)]
    for numero, lot in enumerate(lots):
        if numero:
            attendre(PAUSE_LOTS)
        dossier.mkdir(parents=True, exist_ok=True)
        trouves, encore_limites = _telecharger(lot, debut, fin, attendre)
        if limites is not None:
            limites.update(encore_limites)
        for ticker, df in trouves.items():
            pd.to_pickle({"debut": debut, "fin": fin, "cours": df}, _fichier(dossier, ticker))
            resultat[ticker] = df
        if progres is not None:
            progres(numero + 1, len(lots))
    coupe = {t: resultat[t].loc[pd.Timestamp(debut):pd.Timestamp(fin)] for t in tickers if t in resultat}
    return {t: df for t, df in coupe.items() if not df.empty}


_LIMITE = object()  # valeur renvoyée quand Yahoo limite le débit : à redemander plus tard


def _actions(ticker):
    """Nombre d'actions, None s'il manque, _LIMITE si Yahoo limite le débit (exception, ou message d'erreur du logger
    yfinance lu pendant cet appel, dans ce fil seulement)."""
    guet, journal = _GuetteLimite(fil=threading.get_ident()), logging.getLogger("yfinance")
    journal.addHandler(guet)
    try:
        valeur = float(yf.Ticker(_nom_yahoo(ticker)).fast_info["shares"])
    except Exception as erreur:
        limite = type(erreur).__name__ == "YFRateLimitError" or _est_limite(erreur) or guet.tout or bool(guet.noms)
        return _LIMITE if limite else None
    finally:
        journal.removeHandler(guet)
    if math.isfinite(valeur) and valeur > 0:
        return valeur
    return _LIMITE if guet.tout or guet.noms else None


def actions_en_circulation(tickers, dossier, attendre=None, taille_lot=LOT_ACTIONS, limites=None):
    """Nombre d'actions actuel de chaque ticker (yfinance) ; absent du dict si la valeur manque.

    C'est une valeur d'aujourd'hui, donc la seule donnée de la mesure qui ne date pas de la publication. Deux fils
    seulement, une pause entre les lots, des nouvelles tentatives (30, 60, 120 s) sur limite de débit, et
    actions.json est réécrit après chaque lot. Les titres encore limités après les reprises sont absents du résultat
    et ajoutés à `limites` (s'il est donné)."""
    attendre = attendre or time.sleep
    fichier = Path(dossier) / "actions.json"
    try:
        connues = json.loads(fichier.read_text())
        if not isinstance(connues, dict):
            raise ValueError
    except (OSError, ValueError):
        connues = {}
    tickers = list(dict.fromkeys(tickers))
    manquants = [t for t in tickers if t not in connues]
    for debut_lot in range(0, len(manquants), taille_lot):
        if debut_lot:
            attendre(PAUSE_ACTIONS)
        lot = manquants[debut_lot:debut_lot + taille_lot]
        for tentative in range(len(ATTENTES) + 1):
            with ThreadPoolExecutor(FILS_ACTIONS) as pool:
                valeurs = list(pool.map(_actions, lot))
            for ticker, valeur in zip(lot, valeurs):
                if valeur is not None and valeur is not _LIMITE:
                    connues[ticker] = valeur
            lot = [t for t, v in zip(lot, valeurs) if v is _LIMITE]
            if not lot or tentative == len(ATTENTES):
                break
            attendre(ATTENTES[tentative])
        if limites is not None:
            limites.update(lot)
        Path(dossier).mkdir(parents=True, exist_ok=True)
        fichier.write_text(json.dumps(connues, indent=1))
    return {t: connues[t] for t in tickers if t in connues}
