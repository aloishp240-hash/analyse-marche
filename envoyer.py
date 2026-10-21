#!/usr/bin/env python3
"""Envoie sur Telegram les messages de la boîte d'envoi (agent/a-envoyer.json), puis la vide.

Lancé par GitHub Actions seulement après l'enregistrement réussi de l'état dans le dépôt.
Un message dont l'envoi échoue reste dans la boîte.
Usage : python envoyer.py [--dry-run]
  --dry-run  affiche les messages sans les envoyer ni vider la boîte
"""
import argparse
import sys

from commun import envoyer_telegram
from court_terme import boite


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    messages = boite.lire()
    if args.dry_run:
        for texte in messages:
            print(texte + "\n")
        return 0
    restants = []
    for texte in messages:
        try:
            envoyer_telegram(texte)
        except Exception as erreur:
            print(f"Échec de l'envoi Telegram : {erreur}", file=sys.stderr)
            restants.append(texte)
    boite.ecrire(restants)
    return 1 if restants else 0


if __name__ == "__main__":
    sys.exit(main())
