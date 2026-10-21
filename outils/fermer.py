#!/usr/bin/env python3
"""Ferme un titre suivi avant son stop ou son objectif, quand la thèse n'est plus valable ou pour faire de la place.

Un ordre pas encore exécuté est annulé ; une position est vendue à l'ouverture de la séance suivante
(positions.py), sans prix fixé par Claude. Comme signal.py, rien n'est appliqué ni envoyé avant la fin de la session.

Usage : python outils/fermer.py TICKER "justification"
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

if __name__ == "__main__":  # la racine du dépôt remplace outils/, où signal.py masquerait le module standard
    sys.path[0] = str(Path(__file__).resolve().parents[1])

from commun import NY, charger_config
from court_terme import attente
from court_terme.messages import detailler, titre
from court_terme.portefeuille import charger
from court_terme.etats import ANNULE, noter
from court_terme.preglages import parametres
from court_terme.simulation import clore_ordre
from court_terme.validation import TexteRefuse, justification_fermeture, ticker as lire_ticker

AGENT = Path(__file__).resolve().parents[1] / "agent"
ETAT = AGENT / "positions.json"
EN_ATTENTE = AGENT / "en-attente.json"


def aujourd_hui():
    return datetime.now(NY).date()


def refuser(ticker, motif):
    print(f"refusé : {motif}")
    attente.noter_refus(EN_ATTENTE, ticker, "fermer", motif)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ticker")
    parser.add_argument("justification")
    args = parser.parse_args(argv)
    try:
        ticker = lire_ticker(args.ticker)
        justification = justification_fermeture(args.justification)
    except TexteRefuse as motif:
        return refuser(args.ticker[:12], str(motif))
    p = parametres(charger_config())
    en_attente = attente.lire(EN_ATTENTE)
    etat = en_attente["etat"] or charger(ETAT, p)
    maxi = p.get("fermetures_max_session")
    if maxi is not None and en_attente.get("fermetures", 0) >= maxi:
        return refuser(ticker, f"déjà {maxi} fermetures dans cette session, c'est le maximum")
    jour = aujourd_hui()

    ordre = next((o for o in etat["ordres"] if o["ticker"] == ticker), None)
    if ordre is not None:
        clore_ordre(etat, ordre, ANNULE, jour, motif="annulé par Claude", justification=justification)
        attente.ajouter(EN_ATTENTE, f"❎ ORDRE ANNULÉ — {titre(ordre)}\n\nPourquoi Claude annule :\n"
                                    f"{detailler(justification)}", etat, fermeture=True)
        print(f"ordre {ticker} annulé (envoyé sur Telegram à la fin de la session)")
        return 0

    position = next((e for e in etat["positions"] if e["ticker"] == ticker), None)
    if position is None:
        return refuser(ticker, f"{ticker} n'est ni en ordre ni en position")
    if position.get("sortie_demandee"):
        return refuser(ticker, f"la vente de {ticker} est déjà demandée")
    position["sortie_demandee"] = jour.isoformat()
    position["justification_sortie"] = justification
    noter(position, "vente_demandee", jour, justification=justification)
    attente.ajouter(EN_ATTENTE, f"🟠 VENTE DÉCIDÉE — {titre(position)} : vendre à l'ouverture de la prochaine séance "
                                f"(15h30 à Paris)\n\nPourquoi Claude vend :\n{detailler(justification)}", etat,
                    fermeture=True)
    print(f"vente de {ticker} enregistrée : exécutée à l'ouverture de la prochaine séance "
          "(envoyée sur Telegram à la fin de la session)")
    return 0


if __name__ == "__main__":
    main()
