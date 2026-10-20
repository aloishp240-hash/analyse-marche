#!/usr/bin/env python3
"""Engouement sur Reddit et 4chan d'après ApeWisdom : rang, mentions, upvotes.

Usage : python outils/engouement.py TICKER [TICKER ...]
"""
import argparse
import sys
from pathlib import Path

if __name__ == "__main__":  # la racine du dépôt remplace outils/, où signal.py masquerait le module standard
    sys.path[0] = str(Path(__file__).resolve().parents[1])

import requests

from commun import NAVIGATEUR

URL = "https://apewisdom.io/api/v1.0/filter/all-stocks/page/{}"
PAGES_MAX = 8


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("tickers", nargs="+")
    cherches = [t.upper() for t in parser.parse_args(argv).tickers]
    trouves, page, pages = {}, 1, 1
    try:
        while page <= min(pages, PAGES_MAX) and len(trouves) < len(cherches):
            reponse = requests.get(URL.format(page), headers=NAVIGATEUR, timeout=15)
            reponse.raise_for_status()
            donnees = reponse.json()
            pages = int(donnees.get("pages") or 1)
            trouves.update({r["ticker"]: r for r in donnees.get("results", []) if r.get("ticker") in cherches})
            page += 1
    except Exception as erreur:
        print(f"(ApeWisdom indisponible : {erreur})")
        if not trouves:
            return 0
    for ticker in cherches:
        r = trouves.get(ticker)
        if r:
            print(f"{ticker} : rang {r.get('rank')} · {r.get('mentions')} mentions sur 24 h "
                  f"({r.get('mentions_24h_ago')} la veille) · {r.get('upvotes')} upvotes")
        else:
            print(f"{ticker} : non classé (absent des {page - 1} premières pages)")
    return 0


if __name__ == "__main__":
    main()
