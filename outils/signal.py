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
from court_terme.preglages import parametres
from court_terme.validation import TexteRefuse, justification_signal, ticker as lire_ticker
from positions import taux_eurusd

AGENT = Path(__file__).resolve().parents[1] / "agent"
CANDIDATS = AGENT / "candidats-du-jour.json"
ETAT = AGENT / "positions.json"
EN_ATTENTE = AGENT / "en-attente.json"


def aujourd_hui():
    return datetime.now(NY).date()


def refuser(ticker, motif):
    """Refus affiché à Claude et gardé pour l'évaluation de la session."""
    print(f"refusé : {motif}")
    attente.noter_refus(EN_ATTENTE, ticker, "signal", motif)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ticker")
    parser.add_argument("justification")
    args = parser.parse_args(argv)
    try:
        ticker = lire_ticker(args.ticker)
        justification = justification_signal(args.justification)
    except TexteRefuse as motif:
        return refuser(args.ticker[:12], str(motif))
    try:
        fichier = json.loads(CANDIDATS.read_text())
    except (OSError, ValueError) as erreur:
        return refuser(ticker, f"candidats du jour illisibles ({erreur})")
    if fichier.get("jour") != aujourd_hui().isoformat():
        return refuser(ticker, f"la liste des candidats date du {fichier.get('jour')}, pas d'aujourd'hui")
    # Échec prudent : seule une liste de blocages explicitement vide autorise une entrée
    if fichier.get("entrees_bloquees") != []:
        return refuser(ticker, "nouvelles entrées bloquées aujourd'hui : "
                       + " ; ".join(fichier.get("entrees_bloquees") or ["contrôles du jour absents"]))
    candidat = next((c for c in fichier.get("candidats", []) if c["ticker"] == ticker), None)
    if candidat is None:
        return refuser(ticker, f"{ticker} n'est pas dans les candidats du jour")

    p = parametres(charger_config())
    etat = attente.lire(EN_ATTENTE)["etat"] or charger(ETAT, p)
    if etat.get("suspension"):
        return refuser(ticker, f"nouvelles entrées suspendues : {etat['suspension']['motif']}")
    try:
        taux = taux_eurusd()
    except Exception as erreur:
        return refuser(ticker, f"taux EUR/USD indisponible ({erreur})")
    try:
        ordre = ajouter_ordre(etat, candidat, candidat.get("industrie"), justification,
                              date.fromisoformat(fichier["jour"]), taux, p)
    except ValueError as motif:
        return refuser(ticker, str(motif))
    attente.ajouter(EN_ATTENTE, message_signal(ordre, taux, p["zone_entree_atr"], p["validite_ordre_seances"]), etat)
    print(f"signal enregistré : {ticker}, {ordre['quantite']:g} action(s), entrée {ordre['entree']:.2f} $ "
          "(envoyé sur Telegram à la fin de la session)")
    return 0


if __name__ == "__main__":
    main()
