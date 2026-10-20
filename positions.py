#!/usr/bin/env python3
"""Suivi du portefeuille fictif court terme : exécutions simulées, alertes Telegram, bilan du vendredi.

Lancé toutes les 5 minutes par GitHub Actions. Ne fait rien hors séance US, sauf pour enregistrer la
séance terminée (jusqu'à 17h30 New York) et le bilan du vendredi (à partir de 16h15).
Usage : python positions.py [--dry-run]
  --dry-run  affiche les messages sans les envoyer ni sauver l'état
"""
import argparse
import copy
import sys
from datetime import datetime, time
from pathlib import Path

import pandas as pd
import yfinance as yf

from commun import INDICE, NY, charger_config, envoyer_telegram
from court_terme.messages import message_evenement
from court_terme.portefeuille import charger, sauver
from court_terme.simulation import avancer
from court_terme.stats import bilan_hebdo
from surveillance import CLOTURE, FIN_BILAN_GITHUB, seance_ouverte

ETAT = Path(__file__).resolve().parent / "agent" / "positions.json"
DEVISE = "EURUSD=X"
HEURE_BILAN = time(16, 15)
CHAMPS = (("ouverture", "Open"), ("haut", "High"), ("bas", "Low"), ("cloture", "Close"))


def doit_faire_bilan(maintenant, etat):
    return (maintenant.weekday() == 4 and maintenant.time() >= HEURE_BILAN
            and etat.get("dernier_bilan") != maintenant.date().isoformat())


def a_suivre(maintenant):
    """Séance ouverte, ou peu après la clôture pour enregistrer la séance terminée."""
    return seance_ouverte(maintenant) or (maintenant.weekday() < 5 and CLOTURE <= maintenant.time() < FIN_BILAN_GITHUB)


def barres_du_jour(tickers, maintenant):
    """Séances terminées du dernier mois, plus la barre du jour agrégée depuis les bougies de 5 min."""
    aujourd_hui = maintenant.date()
    journalier = yf.download(tickers, period="1mo", interval="1d", auto_adjust=False, progress=False)
    journalier = journalier[journalier.index.date < aujourd_hui]
    intraday = yf.download(tickers, period="1d", interval="5m", auto_adjust=False, progress=False)
    index = intraday.index.tz_convert(NY) if intraday.index.tz else intraday.index.tz_localize(NY)
    intraday = intraday[index.date == aujourd_hui]
    terminee = maintenant.time() >= CLOTURE

    barres = {}
    for ticker in tickers:
        liste = []
        for jour, ligne in journalier.xs(ticker, axis=1, level=1).iterrows():
            if not ligne[[c for _, c in CHAMPS]].isna().any():
                liste.append({"jour": jour.date(), **{k: float(ligne[c]) for k, c in CHAMPS}, "terminee": True})
        bougies = intraday.xs(ticker, axis=1, level=1).dropna(subset=["Open", "High", "Low", "Close"]) \
            if ticker in intraday.columns.get_level_values(1) else pd.DataFrame()
        if not bougies.empty:
            liste.append({"jour": aujourd_hui, "ouverture": float(bougies["Open"].iloc[0]),
                          "haut": float(bougies["High"].max()), "bas": float(bougies["Low"].min()),
                          "cloture": float(bougies["Close"].iloc[-1]), "terminee": terminee})
        barres[ticker] = liste
    return barres


def dernier_cours(symbole, periode="5d"):
    clotures = yf.download(symbole, period=periode, interval="1d", auto_adjust=False, progress=False)["Close"]
    return clotures[symbole].dropna() if isinstance(clotures, pd.DataFrame) else clotures.dropna()


def taux_eurusd():
    """Dollars pour un euro, à la dernière clôture."""
    return float(dernier_cours(DEVISE).iloc[-1])


def perf_indice(debut):
    """Performance de QQQ sur la semaine (depuis la clôture du vendredi précédent) et depuis `debut`."""
    clotures = dernier_cours(INDICE, "1y")
    jours = clotures.index.date
    derniere = clotures.iloc[-1]
    lundi = jours[-1] - pd.Timedelta(days=jours[-1].weekday())
    avant = clotures[jours < lundi]
    semaine = (derniere / avant.iloc[-1] - 1) * 100 if len(avant) else 0.0
    depuis = clotures[jours <= pd.Timestamp(debut).date()] if debut else clotures.iloc[0:0]
    depuis_debut = (derniere / depuis.iloc[-1] - 1) * 100 if len(depuis) else 0.0
    return float(semaine), float(depuis_debut)


def envoyer(texte, dry_run):
    if dry_run:
        print(texte + "\n")
        return
    try:
        envoyer_telegram(texte)
    except Exception as erreur:
        print(f"Échec de l'envoi Telegram : {erreur}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    maintenant = datetime.now(NY)
    p = charger_config()["court_terme"]
    etat = charger(ETAT, p)
    bilan = doit_faire_bilan(maintenant, etat)
    if not a_suivre(maintenant) and not bilan:
        return
    avant = copy.deepcopy(etat)

    suivis = sorted({e["ticker"] for e in etat["ordres"] + etat["positions"]})
    barres = barres_du_jour(suivis, maintenant) if suivis else {}
    taux = taux_eurusd()
    for evt in avancer(etat, barres, taux, p):
        envoyer(message_evenement(evt), args.dry_run)

    if bilan:
        cours = {t: b[-1]["cloture"] for t, b in barres.items() if b}
        semaine, depuis_debut = perf_indice(etat.get("debut"))
        envoyer(bilan_hebdo(etat, semaine, depuis_debut, taux, cours), args.dry_run)
        etat["dernier_bilan"] = maintenant.date().isoformat()

    if etat != avant and not args.dry_run:
        sauver(etat, ETAT)


if __name__ == "__main__":
    main()
