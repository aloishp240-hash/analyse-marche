#!/usr/bin/env python3
"""Suspension des nouvelles entrées du portefeuille fictif court terme : voir où elle en est, ou la lever.

La suspension est posée par le code (forte baisse du capital réalisé, état incohérent) ; seule une personne la lève,
après vérification. Ce script n'est pas dans les outils autorisés à Claude.
Usage : python suspension.py [etat|lever]
"""
import argparse
from datetime import datetime

from commun import NY, charger_config
from court_terme.portefeuille import charger, sauver
from court_terme.preglages import parametres
from court_terme.securite import baisse_realisee_pct, lever_suspension
from positions import ETAT


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", nargs="?", choices=["etat", "lever"], default="etat")
    args = parser.parse_args(argv)
    p = parametres(charger_config())
    etat = charger(ETAT, p)
    suspension = etat.get("suspension")
    print(f"Recul du capital réalisé depuis son plus haut : {baisse_realisee_pct(etat):.1f} % "
          f"(suspension à {p.get('suspension_baisse_pct')} %)")
    if not suspension:
        print("Aucune suspension en cours.")
        return 0
    print(f"Entrées suspendues depuis le {suspension['depuis']} : {suspension['motif']}")
    if args.action == "lever":
        lever_suspension(etat, datetime.now(NY).date())
        sauver(etat, ETAT)
        print("Suspension levée : le seuil de baisse repart d'aujourd'hui. Pense à commiter agent/positions.json.")
    return 0


if __name__ == "__main__":
    main()
