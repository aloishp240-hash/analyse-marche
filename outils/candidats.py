#!/usr/bin/env python3
"""Candidats du jour repérés par le code, avec leurs niveaux. Usage : python outils/candidats.py"""
import json
import sys
from pathlib import Path

if __name__ == "__main__":  # la racine du dépôt remplace outils/, où signal.py masquerait le module standard
    sys.path[0] = str(Path(__file__).resolve().parents[1])

from court_terme.messages import LIBELLES, titre

CANDIDATS = Path(__file__).resolve().parents[1] / "agent" / "candidats-du-jour.json"


def main(argv=None):
    try:
        fichier = json.loads(CANDIDATS.read_text())
    except (OSError, ValueError) as erreur:
        print(f"(candidats indisponibles : {erreur})")
        return 0
    candidats = fichier.get("candidats", [])
    print(f"Candidats du {fichier.get('jour')} : {len(candidats)}")
    for c in candidats:
        print(f"- {titre(c)} · {LIBELLES.get(c['configuration'], c['configuration'])} · "
              f"{c.get('industrie') or 'industrie inconnue'}")
        print(f"  clôture {c['cloture']:.2f} $ · RSI {c['rsi']:.0f} · ATR {c['atr']:.2f} $ · "
              f"force relative {c['force_relative']:+.1f} pts sur 3 mois vs QQQ")
        print(f"  entrée {c['entree']:.2f} $ · stop {c['stop']:.2f} $ · objectif {c['objectif']:.2f} $")
    return 0


if __name__ == "__main__":
    main()
