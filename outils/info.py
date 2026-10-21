#!/usr/bin/env python3
"""Information sur un titre en ordre ou en position, envoyée sur Telegram à la fin de la session.

Liens retirés et texte limité : il peut reprendre du contenu lu en ligne.

Usage : python outils/info.py TICKER "texte"
"""
import argparse
import sys
from pathlib import Path

if __name__ == "__main__":  # la racine du dépôt remplace outils/, où signal.py masquerait le module standard
    sys.path[0] = str(Path(__file__).resolve().parents[1])

from commun import charger_config
from court_terme import attente
from court_terme.messages import titre
from court_terme.portefeuille import charger
from court_terme.preglages import parametres
from court_terme.validation import TexteRefuse, nettoyer as nettoyer_texte, ticker as lire_ticker

AGENT = Path(__file__).resolve().parents[1] / "agent"
ETAT = AGENT / "positions.json"
EN_ATTENTE = AGENT / "en-attente.json"
LONGUEUR_MAX = 400


def nettoyer(texte):
    return nettoyer_texte(texte, LONGUEUR_MAX, une_ligne=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ticker")
    parser.add_argument("texte")
    args = parser.parse_args(argv)
    try:
        ticker = lire_ticker(args.ticker)
    except TexteRefuse as motif:
        print(f"refusé : {motif}")
        return 0
    etat = attente.lire(EN_ATTENTE)["etat"] or charger(ETAT, parametres(charger_config()))
    suivi = next((e for e in etat["ordres"] + etat["positions"] if e["ticker"] == ticker), None)
    if suivi is None:
        print(f"refusé : {ticker} n'est ni en ordre ni en position")
        return 0
    attente.ajouter(EN_ATTENTE, f"ℹ️ {titre(suivi)} — {nettoyer(args.texte)}")
    print("information enregistrée (envoyée sur Telegram à la fin de la session)")
    return 0


if __name__ == "__main__":
    main()
