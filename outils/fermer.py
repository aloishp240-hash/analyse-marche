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

AGENT = Path(__file__).resolve().parents[1] / "agent"
ETAT = AGENT / "positions.json"
EN_ATTENTE = AGENT / "en-attente.json"


def aujourd_hui():
    return datetime.now(NY).date()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ticker")
    parser.add_argument("justification")
    args = parser.parse_args(argv)
    ticker, justification = args.ticker.upper(), args.justification.strip()
    etat = attente.lire(EN_ATTENTE)["etat"] or charger(ETAT, charger_config()["court_terme"])

    ordre = next((o for o in etat["ordres"] if o["ticker"] == ticker), None)
    if ordre is not None:
        etat["ordres"].remove(ordre)
        attente.ajouter(EN_ATTENTE, f"❎ ORDRE ANNULÉ — {titre(ordre)}\n\nPourquoi Claude annule :\n"
                                    f"{detailler(justification)}", etat)
        print(f"ordre {ticker} annulé (envoyé sur Telegram à la fin de la session)")
        return 0

    position = next((e for e in etat["positions"] if e["ticker"] == ticker), None)
    if position is None:
        print(f"refusé : {ticker} n'est ni en ordre ni en position")
        return 0
    if position.get("sortie_demandee"):
        print(f"refusé : la vente de {ticker} est déjà demandée")
        return 0
    position["sortie_demandee"] = aujourd_hui().isoformat()
    position["justification_sortie"] = justification
    attente.ajouter(EN_ATTENTE, f"🟠 VENTE DÉCIDÉE — {titre(position)} : vendre à l'ouverture de la prochaine séance "
                                f"(15h30 à Paris)\n\nPourquoi Claude vend :\n{detailler(justification)}", etat)
    print(f"vente de {ticker} enregistrée : exécutée à l'ouverture de la prochaine séance "
          "(envoyée sur Telegram à la fin de la session)")
    return 0


if __name__ == "__main__":
    main()
