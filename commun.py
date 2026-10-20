"""Fonctions partagées : configuration, tickers, données de marché, indicateurs, Telegram."""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

RACINE = Path(__file__).resolve().parent
DONNEES = RACINE / "donnees"
ALERTES = RACINE / "alertes"
NY = ZoneInfo("America/New_York")
PARIS = ZoneInfo("Europe/Paris")
FICHIER_TICKERS = RACINE / "tickers.txt"
URL_NASDAQ100 = "https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies"
NAVIGATEUR = {"User-Agent": "Mozilla/5.0 (analyse-marche)"}
INDICE = "QQQ"  # ETF qui réplique le Nasdaq 100, sert de référence pour la force relative


def charger_config():
    return json.loads((RACINE / "config.json").read_text())


def charger_env():
    env = {}
    fichier = RACINE / ".env"
    if fichier.exists():
        for ligne in fichier.read_text().splitlines():
            if "=" in ligne and not ligne.lstrip().startswith("#"):
                cle, valeur = ligne.split("=", 1)
                env[cle.strip()] = valeur.strip().strip('"')
    return env


def charger_tickers():
    """Composition du Nasdaq 100, rafraîchie depuis Wikipédia tous les 30 jours."""
    if FICHIER_TICKERS.exists():
        age = datetime.now() - datetime.fromtimestamp(FICHIER_TICKERS.stat().st_mtime)
        if age < timedelta(days=30):
            return FICHIER_TICKERS.read_text().split()
    try:
        page = requests.get(URL_NASDAQ100, headers=NAVIGATEUR, timeout=20)
        page.raise_for_status()
        for table in pd.read_html(StringIO(page.text)):
            colonne = next((c for c in table.columns if str(c) in ("Ticker", "Symbol")), None)
            if colonne is not None and len(table) > 90:
                tickers = sorted(table[colonne].astype(str).str.replace(".", "-", regex=False))
                FICHIER_TICKERS.write_text("\n".join(tickers) + "\n")
                return tickers
        raise ValueError("table des composants introuvable")
    except Exception as erreur:
        if FICHIER_TICKERS.exists():
            print(f"Rafraîchissement des tickers impossible ({erreur}), ancienne liste conservée")
            return FICHIER_TICKERS.read_text().split()
        raise


def historique_journalier(tickers, cache=True, inclure_jour=False):
    """Clôtures, plus hauts, plus bas et volumes sur un an, hors séance du jour (cache quotidien).

    inclure_jour=True garde la séance du jour (après la clôture) et ne passe pas par le cache."""
    aujourd_hui = datetime.now(NY).date()
    fichier = DONNEES / f"journalier-v2-{aujourd_hui}.pkl"
    cache = cache and not inclure_jour
    if cache and fichier.exists():
        return pd.read_pickle(fichier)
    df = yf.download(tickers, period="1y", interval="1d", auto_adjust=False,
                     progress=False, threads=True)[["Close", "High", "Low", "Volume"]]
    if not inclure_jour:
        df = df[df.index.date < aujourd_hui]
    if cache:
        DONNEES.mkdir(exist_ok=True)
        for ancien in DONNEES.glob("journalier-*.pkl"):
            ancien.unlink()
        df.to_pickle(fichier)
    return df


def donnees_intraday(tickers):
    """Ouvertures, clôtures et volumes des bougies de 5 min de la séance en cours (ou de la dernière)."""
    df = yf.download(tickers, period="1d", interval="5m", auto_adjust=False,
                     progress=False, threads=True)
    return df["Open"], df["Close"].ffill(), df["Volume"].fillna(0)


def par_titre_quotidien(nom, tickers, cache, fonction):
    """Applique fonction(ticker) à chaque titre (8 en parallèle) ; résultat mis en cache une fois par jour."""
    aujourd_hui = datetime.now(NY).date()
    fichier = DONNEES / f"{nom}-{aujourd_hui}.json"
    if fichier.exists():
        connus = json.loads(fichier.read_text())
        if cache or all(t in connus for t in tickers):
            return connus

    def protege(ticker):
        try:
            return fonction(ticker)
        except Exception:
            return None

    with ThreadPoolExecutor(8) as pool:
        resultats = {t: r for t, r in zip(tickers, pool.map(protege, tickers)) if r}
    if cache:
        DONNEES.mkdir(exist_ok=True)
        for ancien in DONNEES.glob(f"{nom}-*.json"):
            ancien.unlink()
        fichier.write_text(json.dumps(resultats, indent=1))
    return resultats


def prochains_resultats(tickers, cache=True):
    """Prochaine date de résultats et BPA attendu (consensus) par ticker."""
    def un_ticker(ticker):
        calendrier = yf.Ticker(ticker).calendar or {}
        dates = calendrier.get("Earnings Date") or []
        if dates:
            return {"date": str(dates[0]), "bpa_attendu": calendrier.get("Earnings Average")}

    return par_titre_quotidien("resultats", tickers, cache, un_ticker)


CHAMPS_FONDAMENTAUX = [
    "sector", "industry", "marketCap", "trailingPE", "forwardPE", "priceToSalesTrailing12Months",
    "totalDebt", "totalCash", "ebitda", "debtToEquity", "revenueGrowth", "earningsGrowth", "profitMargins",
    "freeCashflow", "recommendationKey", "recommendationMean", "targetMeanPrice", "numberOfAnalystOpinions",
]


def fondamentaux(tickers, cache=True):
    """Secteur, valorisation, dette, croissance et consensus des analystes (Yahoo) par ticker."""
    def un_ticker(ticker):
        infos = yf.Ticker(ticker).info or {}
        return {champ: infos.get(champ) for champ in CHAMPS_FONDAMENTAUX}

    return par_titre_quotidien("fondamentaux", tickers, cache, un_ticker)


def rsi(clotures, periode=14):
    """RSI de Wilder sur la dernière valeur de la série."""
    delta = clotures.diff().dropna()
    moy_gains = delta.clip(lower=0).ewm(alpha=1 / periode, adjust=False).mean().iloc[-1]
    moy_pertes = (-delta.clip(upper=0)).ewm(alpha=1 / periode, adjust=False).mean().iloc[-1]
    if moy_pertes == 0:
        return 100.0
    return 100 - 100 / (1 + moy_gains / moy_pertes)


def atr_pct(hauts, bas, clotures, periode=14):
    """ATR de Wilder (volatilité moyenne d'une séance), en % de la dernière clôture."""
    precedente = clotures.shift()
    vrai = pd.concat([hauts - bas, (hauts - precedente).abs(), (bas - precedente).abs()], axis=1).max(axis=1)
    return vrai.ewm(alpha=1 / periode, adjust=False).mean().iloc[-1] / clotures.iloc[-1] * 100


def macd(clotures, rapide=12, lente=26, signal=9):
    """Séries MACD et ligne de signal."""
    ligne = clotures.ewm(span=rapide, adjust=False).mean() - clotures.ewm(span=lente, adjust=False).mean()
    return ligne, ligne.ewm(span=signal, adjust=False).mean()


def envoyer_telegram(texte):
    env = charger_env()
    # Sur GitHub, les identifiants viennent des secrets du dépôt (variables d'environnement)
    jeton = os.environ.get("TELEGRAM_TOKEN") or env.get("TELEGRAM_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID") or env.get("TELEGRAM_CHAT_ID")
    if not jeton or not chat:
        raise RuntimeError("TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID manquant (.env ou secrets GitHub)")
    morceaux, courant = [], ""
    for ligne in texte.splitlines(keepends=True):
        if len(courant) + len(ligne) > 4000:
            morceaux.append(courant)
            courant = ""
        courant += ligne
    morceaux.append(courant)
    for morceau in morceaux:
        reponse = requests.post(f"https://api.telegram.org/bot{jeton}/sendMessage",
                                data={"chat_id": chat, "text": morceau,
                                      "disable_web_page_preview": True}, timeout=20)
        reponse.raise_for_status()
