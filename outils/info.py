#!/usr/bin/env python3
"""Information sur un titre en ordre ou en position, envoyée sur Telegram à la fin de la session.

Liens retirés et texte limité : il peut reprendre du contenu lu en ligne.

Usage : python outils/info.py TICKER "texte"
"""
import argparse
import re
import sys
from pathlib import Path

if __name__ == "__main__":  # la racine du dépôt remplace outils/, où signal.py masquerait le module standard
    sys.path[0] = str(Path(__file__).resolve().parents[1])

from commun import charger_config
from court_terme import attente
from court_terme.portefeuille import charger

AGENT = Path(__file__).resolve().parents[1] / "agent"
ETAT = AGENT / "positions.json"
EN_ATTENTE = AGENT / "en-attente.json"
LONGUEUR_MAX = 400


def nettoyer(texte):
    texte = re.sub(r"(https?://|www\.)\S+", "[lien retiré]", " ".join(texte.split()))
    return texte if len(texte) <= LONGUEUR_MAX else texte[:LONGUEUR_MAX - 1] + "…"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ticker")
    parser.add_argument("texte")
    args = parser.parse_args(argv)
    ticker = args.ticker.upper()
    etat = attente.lire(EN_ATTENTE)["etat"] or charger(ETAT, charger_config()["court_terme"])
    if ticker not in {e["ticker"] for e in etat["ordres"] + etat["positions"]}:
        print(f"refusé : {ticker} n'est ni en ordre ni en position")
        return 0
    attente.ajouter(EN_ATTENTE, f"ℹ️ {ticker} — {nettoyer(args.texte)}")
    print("information enregistrée (envoyée sur Telegram à la fin de la session)")
    return 0


if __name__ == "__main__":
    main()
