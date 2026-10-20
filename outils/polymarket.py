#!/usr/bin/env python3
"""Paris en cours sur Polymarket : probabilité de chaque issue, variation sur 7 jours, volume.

Usage : python outils/polymarket.py "RECHERCHE"   (ticker, nom de société ou thème : NVDA, Tesla, Fed…)
"""
import argparse
import json
import sys
from pathlib import Path

if __name__ == "__main__":  # la racine du dépôt remplace outils/, où signal.py masquerait le module standard
    sys.path[0] = str(Path(__file__).resolve().parents[1])

import requests

from commun import NAVIGATEUR

URL = "https://gamma-api.polymarket.com/public-search"
EVENEMENTS_MAX = 6
ISSUES_MAX = 4
VOLUME_MIN = 1000  # $ : en dessous, la probabilité ne reflète l'avis que de quelques parieurs


def probabilite_oui(marche):
    try:
        return float(json.loads(marche.get("outcomePrices") or "[]")[0])
    except (ValueError, IndexError, TypeError):
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("recherche")
    recherche = parser.parse_args(argv).recherche
    try:
        reponse = requests.get(URL, headers=NAVIGATEUR, timeout=15,
                               params={"q": recherche, "limit_per_type": 15, "events_status": "active"})
        reponse.raise_for_status()
        evenements = reponse.json().get("events") or []
    except Exception as erreur:
        print(f"(Polymarket indisponible : {erreur})")
        return 0
    evenements = sorted((e for e in evenements if not e.get("closed") and (e.get("volume") or 0) >= VOLUME_MIN),
                        key=lambda e: e.get("volume") or 0, reverse=True)[:EVENEMENTS_MAX]
    if not evenements:
        print(f"(aucun pari actif sur « {recherche} »)")
    for e in evenements:
        print(f"- {e.get('title')} | échéance {(e.get('endDate') or '')[:10]} · volume {e.get('volume') or 0:,.0f} $")
        issues = [(probabilite_oui(m), m) for m in e.get("markets") or [] if not m.get("closed")]
        issues = sorted((i for i in issues if i[0] is not None), key=lambda i: i[0], reverse=True)
        for proba, m in issues[:ISSUES_MAX]:
            variation = m.get("oneWeekPriceChange")
            suffixe = f" ({variation * 100:+.0f} pts sur 7 j)" if variation and abs(variation) >= 0.005 else ""
            print(f"  {m.get('groupItemTitle') or m.get('question')} : {proba * 100:.0f} %{suffixe}")
    return 0


if __name__ == "__main__":
    main()
