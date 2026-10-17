#!/usr/bin/env python3
"""Fiche compacte d'un ou plusieurs titres, pour l'agent d'analyse.

Usage : python fiche.py AAPL NVDA ...
Affiche cours, variations, force relative, moyennes mobiles, RSI, MACD, ATR, Bollinger, pivots,
volume, valorisation, dette, croissance, avis des analystes, prochains résultats, alertes du jour
et dernières actualités.
"""
import json
import sys
from datetime import datetime

from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

from datetime import timedelta

import pandas as pd
import requests
import yfinance as yf

from commun import (ALERTES, INDICE, NAVIGATEUR, NY, atr_pct, donnees_intraday, fondamentaux,
                    historique_journalier, macd, prochains_resultats, rsi)

URL_ACTUALITES = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={}&region=US&lang=en-US"
DUREE_SEANCE_MIN = 390
CONSENSUS = {"strong_buy": "achat fort", "buy": "achat", "hold": "conserver", "underperform": "sous-performance",
             "sell": "vente", "strong_sell": "vente forte"}
ACTIONS_ANALYSTES = {"up": "relève", "down": "abaisse", "init": "initie", "main": "maintient", "reit": "réitère"}


def md(valeur):
    """Montant en milliards de dollars, ou « n.d. » si Yahoo ne le fournit pas."""
    return f"{valeur / 1e9:,.1f} Md$".replace(",", " ") if valeur is not None else "n.d."


def nombre(valeur, format_="{:.1f}", suffixe=""):
    return format_.format(valeur) + suffixe if valeur is not None else "n.d."


def pct(valeur):
    """Fraction Yahoo (0.25) affichée en pourcentage signé."""
    return f"{valeur * 100:+.1f} %" if valeur is not None else "n.d."


def lignes_fondamentaux(prix, f):
    lignes = [f"Secteur : {f.get('sector') or 'n.d.'} / {f.get('industry') or 'n.d.'}",
              f"Valorisation : capitalisation {md(f.get('marketCap'))} | PER {nombre(f.get('trailingPE'))} "
              f"(attendu sur 12 mois {nombre(f.get('forwardPE'))}) | cours/CA {nombre(f.get('priceToSalesTrailing12Months'))}"]

    dette, tresorerie, ebitda = f.get("totalDebt"), f.get("totalCash"), f.get("ebitda")
    if dette is not None and tresorerie is not None:
        nette = dette - tresorerie
        ratio = f" | dette nette/EBITDA {nette / ebitda:.1f}" if ebitda and ebitda > 0 else ""
        lecture = " (plus de trésorerie que de dette)" if nette < 0 else ""
        d_e = f.get("debtToEquity")
        lignes.append(f"Dette : totale {md(dette)} | trésorerie {md(tresorerie)} | nette {md(nette)}{lecture}{ratio}"
                      f" | dette/capitaux propres {nombre(d_e, '{:.0f}', ' %')}")

    lignes.append(f"Croissance sur un an (dernier trimestre) : CA {pct(f.get('revenueGrowth'))} | BPA "
                  f"{pct(f.get('earningsGrowth'))} | marge nette {pct(f.get('profitMargins')).lstrip('+')}"
                  f" | flux de trésorerie disponible {md(f.get('freeCashflow'))}")

    if f.get("numberOfAnalystOpinions"):
        objectif = f.get("targetMeanPrice")
        ecart = f" ({(objectif / prix - 1) * 100:+.1f} % par rapport au cours)" if objectif else ""
        lignes.append(f"Analystes ({f['numberOfAnalystOpinions']}) : consensus "
                      f"{CONSENSUS.get(f.get('recommendationKey'), f.get('recommendationKey'))} "
                      f"(note {nombre(f.get('recommendationMean'), '{:.1f}')}/5, 1 = achat fort) | "
                      f"objectif moyen {nombre(objectif, '{:.2f}', ' $')}{ecart}")
    return lignes


def changements_analystes(ticker, jours=30, nombre_max=5):
    """Derniers changements de note ou d'objectif de cours des analystes."""
    try:
        changements = yf.Ticker(ticker).upgrades_downgrades
        if changements is None or changements.empty:
            return []
        recents = changements[changements.index >= pd.Timestamp.now() - timedelta(days=jours)].iloc[:nombre_max]
        lignes = []
        for date, c in recents.iterrows():
            objectif = ""
            if c["currentPriceTarget"]:
                change = c["priorPriceTarget"] and c["priorPriceTarget"] != c["currentPriceTarget"]
                avant = f"{c['priorPriceTarget']:.0f} → " if change else ""
                objectif = f", objectif {avant}{c['currentPriceTarget']:.0f} $"
            note = c["ToGrade"] if c["FromGrade"] in ("", c["ToGrade"]) else f"{c['FromGrade']} → {c['ToGrade']}"
            lignes.append(f"  - {date:%m-%d} {c['Firm']} {ACTIONS_ANALYSTES.get(c['Action'], c['Action'])} {note}{objectif}")
        return lignes
    except Exception:
        return []


def actualites(ticker, nombre=6):
    """Derniers titres du flux RSS Yahoo Finance (gratuit, sans clé)."""
    try:
        reponse = requests.get(URL_ACTUALITES.format(ticker), headers=NAVIGATEUR, timeout=15)
        reponse.raise_for_status()
        articles = ElementTree.fromstring(reponse.content).iter("item")
        lignes = []
        for article in list(articles)[:nombre]:
            date = parsedate_to_datetime(article.findtext("pubDate")).astimezone(NY)
            lignes.append(f"  - {date:%m-%d %H:%M} NY | {article.findtext('title')} | {article.findtext('link')}")
        return lignes or ["  (aucune actualité)"]
    except Exception as erreur:
        return [f"  (actualités indisponibles : {erreur})"]


def alertes_du_jour(ticker):
    fichiers = sorted(ALERTES.glob("*.jsonl"))
    if not fichiers:
        return []
    return [json.loads(l) for l in fichiers[-1].read_text().splitlines() if json.loads(l)["ticker"] == ticker]


def cours(ticker, hist, close_i):
    """Prix actuel et clôtures des séances précédentes (sans compter deux fois la séance en cours)."""
    serie = close_i[ticker].dropna() if ticker in close_i.columns else pd.Series(dtype=float)
    clotures = hist["Close"][ticker].dropna()
    if not serie.empty:
        clotures = clotures[clotures.index.date < serie.index[-1].date()]
    prix = serie.iloc[-1] if not serie.empty else clotures.iloc[-1]
    return prix, clotures


def fiche(ticker, hist, close_i, vol_i, resultats, fonda):
    prix, clotures = cours(ticker, hist, close_i)
    hauts, bas = hist["High"][ticker].loc[clotures.index], hist["Low"][ticker].loc[clotures.index]
    avec_jour = pd.concat([clotures, pd.Series([prix])], ignore_index=True)
    variation = lambda jours: (prix / clotures.iloc[-jours] - 1) * 100

    lignes = [
        f"== {ticker} — {prix:.2f} $ ==",
        f"Variation : jour {variation(1):+.1f} % | 5 j {variation(5):+.1f} % | 1 mois {variation(21):+.1f} % "
        f"| 1 an {variation(len(clotures)):+.1f} %",
    ]

    # Force relative : écart de performance avec l'indice, en points
    if ticker != INDICE:
        prix_i, clotures_i = cours(INDICE, hist, close_i)
        perf_i = lambda jours: (prix_i / clotures_i.iloc[-jours] - 1) * 100
        lignes.append(f"Force relative vs {INDICE} : jour {variation(1) - perf_i(1):+.1f} pts | "
                      f"1 mois {variation(21) - perf_i(21):+.1f} pts | 3 mois {variation(63) - perf_i(63):+.1f} pts "
                      f"({INDICE} jour {perf_i(1):+.1f} %)")

    atr = atr_pct(hauts, bas, clotures)
    ligne_macd, signal = macd(avec_jour)
    histo = ligne_macd.iloc[-1] - signal.iloc[-1]
    lignes += [
        f"MM50 {avec_jour.iloc[-50:].mean():.2f} | MM200 {avec_jour.iloc[-200:].mean():.2f} | RSI14 {rsi(avec_jour):.0f}",
        f"MACD {ligne_macd.iloc[-1]:.2f} | signal {signal.iloc[-1]:.2f} | histogramme {histo:+.2f} "
        f"({'haussier' if histo > 0 else 'baissier'})",
        f"ATR14 {atr:.1f} % (variation journalière habituelle) ; aujourd'hui {abs(variation(1)) / atr:.1f}× l'ATR",
    ]

    # Bandes de Bollinger 20 jours ±2 écarts-types
    mm20, ecart = avec_jour.rolling(20).mean(), avec_jour.rolling(20).std()
    largeurs = 4 * ecart / mm20 * 100
    pct_b = (prix - (mm20.iloc[-1] - 2 * ecart.iloc[-1])) / (4 * ecart.iloc[-1])
    compression = " — compression (largeur proche de son plus bas sur 6 mois)" \
        if largeurs.iloc[-1] <= 1.1 * largeurs.iloc[-120:].min() else ""
    lignes.append(f"Bollinger 20 : %B {pct_b:.2f} (0 = bande basse, 1 = bande haute) | largeur {largeurs.iloc[-1]:.1f} %{compression}")

    # Points pivots classiques calculés sur la séance précédente
    h, l, c = hauts.iloc[-1], bas.iloc[-1], clotures.iloc[-1]
    p = (h + l + c) / 3
    lignes.append(f"Pivots (veille) : S2 {p - (h - l):.2f} | S1 {2 * p - h:.2f} | P {p:.2f} | "
                  f"R1 {2 * p - l:.2f} | R2 {p + (h - l):.2f}")

    plus_haut, plus_bas = hauts.iloc[-252:].max(), bas.iloc[-252:].min()
    lignes.append(f"Plus haut / bas 52 semaines : {plus_haut:.2f} / {plus_bas:.2f} "
                  f"(à {(prix / plus_haut - 1) * 100:+.1f} % du plus haut)")

    serie = close_i[ticker].dropna()
    vol_moyen = hist["Volume"][ticker].loc[clotures.index].iloc[-20:].mean()
    if not serie.empty and vol_moyen > 0:
        derniere = serie.index[-1].tz_convert(NY)
        ecoule = (derniere - derniere.replace(hour=9, minute=30, second=0, microsecond=0)).total_seconds() / 60 + 5
        fraction = min(max(ecoule, 5) / DUREE_SEANCE_MIN, 1)
        lignes.append(f"Volume du jour : {vol_i[ticker].sum() / (vol_moyen * fraction):.1f}× la normale "
                      f"(rapporté à la part de séance écoulée)")

    if fonda.get(ticker):
        lignes += lignes_fondamentaux(prix, fonda[ticker])
    changements = changements_analystes(ticker)
    if changements:
        lignes.append("Changements d'avis des analystes (30 j) :")
        lignes += changements

    prochain = resultats.get(ticker)
    if prochain:
        bpa = f", BPA attendu {prochain['bpa_attendu']:.2f} $" if prochain.get("bpa_attendu") else ""
        lignes.append(f"Prochains résultats : {prochain['date']}{bpa}")

    alertes = alertes_du_jour(ticker)
    if alertes:
        lignes.append("Alertes du jour :")
        lignes += [f"  - {a['heure_ny']} NY : {a['message']}" for a in alertes]
    lignes.append("Actualités :")
    lignes += actualites(ticker)
    return "\n".join(lignes)


def main():
    tickers = [t.upper() for t in sys.argv[1:]]
    if not tickers:
        sys.exit(__doc__)
    tous = tickers + [INDICE] if INDICE not in tickers else tickers
    hist = historique_journalier(tous, cache=False)
    _, close_i, vol_i = donnees_intraday(tous)
    try:
        resultats = prochains_resultats(tickers, cache=False)
    except Exception:
        resultats = {}
    try:
        fonda = fondamentaux(tickers, cache=False)
    except Exception:
        fonda = {}
    print(f"Données au {datetime.now(NY):%Y-%m-%d %H:%M} heure de New York\n")
    for ticker in tickers:
        try:
            print(fiche(ticker, hist, close_i, vol_i, resultats, fonda) + "\n")
        except Exception as erreur:
            print(f"== {ticker} — données indisponibles ({erreur}) ==\n")


if __name__ == "__main__":
    main()
