#!/usr/bin/env python3
"""Fiche détaillée de chaque trade fermé du portefeuille fictif et variantes d'entrée et de sortie rejouées.

Écrit agent/analyses/trades.json ; ne modifie jamais agent/positions.json, ses statistiques ni config.json.
Les variantes servent à comprendre, pas à régler : aucun paramètre n'est changé automatiquement.

Usage : python analyse_trades.py [--json]
"""
import argparse
import json
import sys
from datetime import datetime, time, timedelta
from pathlib import Path

import yfinance as yf

from backtest import DEVISE, barre
from commun import NY, charger_config
from court_terme.analyse import VARIANTES, mesures_trade, rejouer, synthese
from court_terme.portefeuille import charger, sauver
from court_terme.preglages import parametres
from court_terme.stats import ECHANTILLON_MIN

AGENT = Path(__file__).with_name("agent")
SORTIE = AGENT / "analyses" / "trades.json"


def telecharger(tickers, debut):
    df = yf.download(sorted(tickers) + [DEVISE], start=debut, end=datetime.now(NY).date() + timedelta(days=1),
                     interval="1d", auto_adjust=False, progress=False, threads=True)
    maintenant = datetime.now(NY)
    if len(df) and df.index[-1].date() == maintenant.date() and maintenant.time() < time(16):
        df = df.iloc[:-1]  # séance en cours : barre incomplète
    taux = df["Close"][DEVISE].ffill().bfill()
    barres = {t: [b for i in range(len(df)) if (b := barre(df, t, i))] for t in tickers}
    return barres, {d.date(): float(v) for d, v in taux.items()}


def analyser(fermees, barres, taux, p):
    analyses = []
    for f in fermees:
        variantes = {nom: rejouer(f, barres.get(f["ticker"], []), taux, p, nom) for nom in VARIANTES}
        analyses.append({"officiel": mesures_trade(f), "variantes": variantes})
    return analyses


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="affiche le résultat complet")
    args = parser.parse_args()
    p = parametres(charger_config(), "amelioree")
    fermees = charger(AGENT / "positions.json", p)["fermees"]
    if not fermees:
        print("Aucun trade fermé pour l'instant : rien à analyser.")
        return 0
    debut = min(f["date_signal"] for f in fermees)
    barres, taux = telecharger({f["ticker"] for f in fermees}, datetime.fromisoformat(debut).date() - timedelta(days=5))
    analyses = analyser(fermees, barres, taux, p)
    resume = synthese(analyses)
    sauver({"genere_le": datetime.now(NY).isoformat(timespec="minutes"),
            "avertissement": "Hypothèses rejouées sur barres journalières ; le résultat officiel reste celui du "
                             "portefeuille. Aucun réglage n'est modifié automatiquement.",
            "synthese": resume, "trades": analyses}, SORTIE)
    if args.json:
        print(json.dumps(resume, ensure_ascii=False, indent=2))
    print(f"{len(fermees)} trade(s) fermé(s) analysé(s) → {SORTIE.relative_to(AGENT.parent)}")
    for nom, r in resume.items():
        if r["r_moyen"] is None:
            print(f"- {nom} : aucun trade rejoué")
            continue
        print(f"- {nom} ({r['description']}) : {r['trades']} trade(s), {r['r_moyen']:+.2f} R en moyenne, "
              f"écart {r['ecart_r_moyen']:+.2f} R face au réel, {r['non_executes']} achat(s) non fait(s)")
    if len(fermees) < ECHANTILLON_MIN:
        print(f"\nAttention : moins de {ECHANTILLON_MIN} trades. Ces écarts tiennent surtout du hasard ; "
              "ne changez aucun réglage sur cette base.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
