#!/usr/bin/env python3
"""Valide un candidat du jour : ordre fictif calculé par le code, mis en attente jusqu'à la fin de la session.

agent.py l'enregistre et l'annonce sur Telegram seulement si la session Claude réussit.

Usage : python outils/signal.py TICKER "justification"
Aucun prix n'est accepté : entrée, stop et objectif viennent des candidats du jour.
"""
import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

if __name__ == "__main__":  # la racine du dépôt remplace outils/, où ce fichier masquerait le module standard
    sys.path[0] = str(Path(__file__).resolve().parents[1])

from commun import NY, charger_config
from court_terme import attente
from court_terme.messages import message_signal
from court_terme.portefeuille import ajouter_ordre, charger
from positions import taux_eurusd

AGENT = Path(__file__).resolve().parents[1] / "agent"
CANDIDATS = AGENT / "candidats-du-jour.json"
ETAT = AGENT / "positions.json"
EN_ATTENTE = AGENT / "en-attente.json"


def aujourd_hui():
    return datetime.now(NY).date()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ticker")
    parser.add_argument("justification")
    args = parser.parse_args(argv)
    ticker = args.ticker.upper()
    try:
        fichier = json.loads(CANDIDATS.read_text())
    except (OSError, ValueError) as erreur:
        print(f"refusé : candidats du jour illisibles ({erreur})")
        return 0
    if fichier.get("jour") != aujourd_hui().isoformat():
        print(f"refusé : la liste des candidats date du {fichier.get('jour')}, pas d'aujourd'hui")
        return 0
    candidat = next((c for c in fichier.get("candidats", []) if c["ticker"] == ticker), None)
    if candidat is None:
        print(f"refusé : {ticker} n'est pas dans les candidats du jour")
        return 0

    p = charger_config()["court_terme"]
    etat = attente.lire(EN_ATTENTE)["etat"] or charger(ETAT, p)
    try:
        taux = taux_eurusd()
        ordre = ajouter_ordre(etat, candidat, candidat.get("industrie"), args.justification.strip(),
                              date.fromisoformat(fichier["jour"]), taux, p)
    except ValueError as motif:
        print(f"refusé : {motif}")
        return 0
    except Exception as erreur:
        print(f"refusé : taux EUR/USD indisponible ({erreur})")
        return 0
    attente.ajouter(EN_ATTENTE, message_signal(ordre, taux, p["zone_entree_atr"]), etat)
    print(f"signal enregistré : {ticker}, {ordre['quantite']:g} action(s), entrée {ordre['entree']:.2f} $ "
          "(envoyé sur Telegram à la fin de la session)")
    return 0


if __name__ == "__main__":
    main()
