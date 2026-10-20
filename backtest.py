"""Backtest de la stratégie court terme sur l'historique du Nasdaq 100, sans Claude.

Usage : python backtest.py [--annees 3] [--configurations A|B|AB]
"""
import argparse
import bisect
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pandas as pd
import yfinance as yf

from commun import INDICE, charger_config, charger_tickers, fondamentaux
from court_terme.portefeuille import ajouter_ordre, capital_courant
from court_terme.simulation import avancer
from court_terme.stats import statistiques
from court_terme.strategie import reperer

DEVISE = "EURUSD=X"
FENETRE = 260  # séances passées à reperer : assez pour la MM200 et la performance sur 3 mois


def dates_resultats(tickers):
    """Dates de résultats passées et à venir par titre (triées) ; titres en échec à part."""
    def un_ticker(ticker):
        try:
            df = yf.Ticker(ticker).get_earnings_dates(limit=20)
            return ticker, sorted({d.date() for d in df.index}) if df is not None else None
        except Exception:
            return ticker, None

    with ThreadPoolExecutor(8) as pool:
        dates = dict(pool.map(un_ticker, tickers))
    return {t: d for t, d in dates.items() if d}, [t for t, d in dates.items() if not d]


def prochains(dates, jour):
    resultats = {}
    for ticker, liste in dates.items():
        i = bisect.bisect_left(liste, jour)
        if i < len(liste):
            resultats[ticker] = {"date": liste[i].isoformat()}
    return resultats


def barre(df, ticker, i):
    valeurs = {cle: df[champ][ticker].iloc[i] for cle, champ in
               (("ouverture", "Open"), ("haut", "High"), ("bas", "Low"), ("cloture", "Close"))}
    if any(pd.isna(v) for v in valeurs.values()):
        return None
    return {"jour": df.index[i].date(), "terminee": True, **{k: float(v) for k, v in valeurs.items()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--annees", type=int, default=3)
    parser.add_argument("--configurations", default="A", choices=["A", "B", "AB"])
    args = parser.parse_args()

    p = charger_config()["court_terme"]
    p["configurations"] = {c: c in args.configurations for c in "AB"}
    tickers = charger_tickers()

    print("Biais à garder en tête :")
    print("- la composition actuelle du Nasdaq 100 est appliquée au passé (les titres sortis de l'indice manquent) ;")
    print("- les dates de résultats passées viennent de Yahoo et sont approximatives ;")
    print("- chaque candidat est pris sans validation par Claude ; industries actuelles appliquées au passé.\n")

    df = yf.download(tickers + [INDICE, DEVISE], period=f"{args.annees + 1}y", interval="1d",
                     auto_adjust=False, progress=False, threads=True)
    df = df[df["Close"][INDICE].notna()]
    taux = df["Close"][DEVISE].ffill().bfill()
    industries = {t: f.get("industry") for t, f in fondamentaux(tickers).items()}
    dates, sans_dates = dates_resultats(tickers)
    if sans_dates:
        print(f"Dates de résultats indisponibles pour {len(sans_dates)} titres (pas d'exclusion) : "
              f"{' '.join(sans_dates)}\n")

    debut = max(FENETRE, len(df) - args.annees * 252)
    etat = {"capital_initial": p["capital_initial"], "debut": None, "ordres": [], "positions": [], "fermees": []}
    compte = Counter()
    for i in range(debut, len(df)):
        jour = df.index[i].date()
        suivis = {e["ticker"] for e in etat["ordres"] + etat["positions"]}
        barres = {t: [b] for t in suivis if (b := barre(df, t, i))}
        compte.update(e["type"] for e in avancer(etat, barres, float(taux.iloc[i]), p))
        fenetre = df.iloc[i - FENETRE + 1:i + 1]
        suivis = {e["ticker"] for e in etat["ordres"] + etat["positions"]}
        for candidat in reperer(fenetre, tickers, prochains(dates, jour), jour, p, suivis):
            try:
                ajouter_ordre(etat, candidat, industries.get(candidat["ticker"]), "backtest", jour,
                              float(taux.iloc[i]), p)
                compte["signal"] += 1
            except ValueError:
                compte["refus"] += 1

    s = statistiques(etat["fermees"])
    indice = df["Close"][INDICE]
    perf_indice = (indice.iloc[-1] / indice.iloc[debut - 1] - 1) * 100
    perf = (capital_courant(etat) / p["capital_initial"] - 1) * 100
    print(f"Période : {df.index[debut].date()} → {df.index[-1].date()} · configurations {args.configurations}")
    print(f"Signaux : {compte['signal']} · refusés par les plafonds : {compte['refus']} · "
          f"ordres expirés : {compte['expire']}")
    print(f"Positions fermées : {s['nombre']} (stop {compte['stop']}, objectif {compte['objectif']}, "
          f"temps {compte['temps']}) · encore ouvertes : {len(etat['positions'])}")
    if s["nombre"]:
        print(f"Taux de réussite : {s['taux_reussite']} % · gain moyen {s['gain_moyen']:+.2f} € · "
              f"perte moyenne {s['perte_moyenne'] or 0:+.2f} €")
        print(f"Espérance par position : {s['esperance']:+.2f} € · total réalisé {s['resultat_total']:+.2f} €")
    print(f"Capital : {capital_courant(etat):.2f} € ({perf:+.2f} %) · QQQ sur la période : {perf_indice:+.2f} %")


if __name__ == "__main__":
    main()
