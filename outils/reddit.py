#!/usr/bin/env python3
"""Discussions de la semaine sur r/stocks, r/wallstreetbets et r/investing.

Usage : python outils/reddit.py TICKER
Nécessite REDDIT_CLIENT_ID et REDDIT_CLIENT_SECRET (secrets GitHub ou .env).
"""
import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

if __name__ == "__main__":  # la racine du dépôt remplace outils/, où signal.py masquerait le module standard
    sys.path[0] = str(Path(__file__).resolve().parents[1])

import requests

from commun import NY, charger_env

URL_JETON = "https://www.reddit.com/api/v1/access_token"
URL_RECHERCHE = "https://oauth.reddit.com/r/stocks+wallstreetbets+investing/search"
ENTETES = {"User-Agent": "analyse-marche/1.0"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ticker")
    ticker = parser.parse_args(argv).ticker.upper()
    env = charger_env()
    identifiant = os.environ.get("REDDIT_CLIENT_ID") or env.get("REDDIT_CLIENT_ID")
    secret = os.environ.get("REDDIT_CLIENT_SECRET") or env.get("REDDIT_CLIENT_SECRET")
    if not identifiant or not secret:
        print("Reddit non configuré (REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET absents)")
        return 0
    try:
        reponse = requests.post(URL_JETON, auth=(identifiant, secret), data={"grant_type": "client_credentials"},
                                headers=ENTETES, timeout=15)
        reponse.raise_for_status()
        jeton = reponse.json()["access_token"]
        reponse = requests.get(URL_RECHERCHE, headers={**ENTETES, "Authorization": f"bearer {jeton}"}, timeout=15,
                               params={"q": ticker, "restrict_sr": 1, "sort": "new", "t": "week", "limit": 15})
        reponse.raise_for_status()
        posts = [enfant["data"] for enfant in reponse.json()["data"]["children"]]
    except Exception as erreur:
        print(f"(Reddit indisponible : {erreur})")
        return 0
    if not posts:
        print(f"(aucune discussion sur {ticker} cette semaine)")
    for d in posts:
        date = datetime.fromtimestamp(d["created_utc"], NY)
        print(f"- {date:%m-%d} r/{d['subreddit']} | {d['title']} | score {d['score']} · "
              f"{d['num_comments']} commentaires")
        extrait = " ".join((d.get("selftext") or "").split())[:200]
        if extrait:
            print(f"  {extrait}")
    return 0


if __name__ == "__main__":
    main()
