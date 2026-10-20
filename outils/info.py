#!/usr/bin/env python3
"""Information sur un titre en ordre ou en position, envoyée sur Telegram sans toucher au portefeuille.

Usage : python outils/info.py TICKER "texte"
"""
import argparse
import sys
from pathlib import Path

if __name__ == "__main__":  # la racine du dépôt remplace outils/, où signal.py masquerait le module standard
    sys.path[0] = str(Path(__file__).resolve().parents[1])

from commun import charger_config, envoyer_telegram
from court_terme.portefeuille import charger

ETAT = Path(__file__).resolve().parents[1] / "agent" / "positions.json"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ticker")
    parser.add_argument("texte")
    args = parser.parse_args(argv)
    ticker = args.ticker.upper()
    etat = charger(ETAT, charger_config()["court_terme"])
    if ticker not in {e["ticker"] for e in etat["ordres"] + etat["positions"]}:
        print(f"refusé : {ticker} n'est ni en ordre ni en position")
        return 0
    try:
        envoyer_telegram(f"ℹ️ {ticker} — {args.texte.strip()}")
    except Exception as erreur:
        print(f"(Telegram indisponible : {erreur})")
        return 0
    print("information envoyée")
    return 0


if __name__ == "__main__":
    main()
