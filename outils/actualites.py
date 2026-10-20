#!/usr/bin/env python3
"""Derniers titres d'actualité d'un titre (Yahoo Finance et Google Actualités).

Usage : python outils/actualites.py TICKER
"""
import argparse
import sys
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree

if __name__ == "__main__":  # la racine du dépôt remplace outils/, où signal.py masquerait le module standard
    sys.path[0] = str(Path(__file__).resolve().parents[1])

import requests

from commun import NAVIGATEUR, NY
from fiche import actualites

URL_GOOGLE = "https://news.google.com/rss/search?q={}+stock&hl=en-US&gl=US&ceid=US:en"


def google(ticker, nombre=6):
    try:
        reponse = requests.get(URL_GOOGLE.format(ticker), headers=NAVIGATEUR, timeout=15)
        reponse.raise_for_status()
        articles = list(ElementTree.fromstring(reponse.content).iter("item"))
        articles.sort(key=lambda a: parsedate_to_datetime(a.findtext("pubDate")), reverse=True)
        lignes = [f"  - {parsedate_to_datetime(a.findtext('pubDate')).astimezone(NY):%m-%d %H:%M} NY | "
                  f"{a.findtext('title')}" for a in articles[:nombre]]
        return lignes or ["  (aucune actualité)"]
    except Exception as erreur:
        return [f"  (Google Actualités indisponible : {erreur})"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ticker")
    ticker = parser.parse_args(argv).ticker.upper()
    print(f"Yahoo Finance — {ticker} :")
    print("\n".join(actualites(ticker)))
    print(f"Google Actualités — {ticker} :")
    print("\n".join(google(ticker)))
    return 0


if __name__ == "__main__":
    main()
