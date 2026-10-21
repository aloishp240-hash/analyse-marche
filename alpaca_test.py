#!/usr/bin/env python3
"""Vérifie la connexion au compte Alpaca paper (fictif).

Affiche le compte, passe un ordre bracket d'achat d'une action à la moitié du cours (il ne peut pas être
exécuté), vérifie qu'Alpaca l'a reçu, puis l'annule. Usage : python alpaca_test.py [TICKER]
"""
import sys
import time
from datetime import datetime

import yfinance as yf

from court_terme.alpaca import URL_PAPER, Client, ErreurAlpaca


def verifier(client, ticker, cours, attente=1.0):
    compte = client.compte()
    lignes = [f"Compte paper ({URL_PAPER}) : statut {compte['status']}, liquidités {compte['cash']} $, "
              f"valeur {compte['equity']} $, achats bloqués : {compte['trading_blocked']}"]
    entree = round(cours * 0.5, 2)
    stop, objectif = round(entree * 0.95, 2), round(entree * 1.10, 2)
    ordre = client.passer_bracket(ticker, 1, entree=entree, stop=stop, objectif=objectif,
                                  id_client=f"test-{datetime.now():%Y%m%d-%H%M%S}")
    try:
        lignes.append(f"Ordre de test envoyé : achat de 1 {ticker} à {entree} $ (cours {cours:.2f} $), "
                      f"stop {stop} $, objectif {objectif} $")
        recu = client.ordre(ordre["id"])
        lignes.append(f"Alpaca l'a reçu : statut {recu['status']}, type {recu['order_class']}, "
                      f"{len(recu.get('legs') or [])} ordres liés (objectif et stop)")
    finally:
        client.annuler(ordre["id"])
    for _ in range(5):
        time.sleep(attente)
        statut = client.ordre(ordre["id"])["status"]
        if statut == "canceled":
            break
    lignes.append(f"Ordre de test annulé (statut final : {statut})")
    return lignes


def main():
    ticker = (sys.argv[1] if len(sys.argv) > 1 else "AAPL").upper()
    try:
        client = Client()
        cours = float(yf.Ticker(ticker).fast_info["last_price"])
        for ligne in verifier(client, ticker, cours):
            print(ligne)
    except ErreurAlpaca as erreur:
        sys.exit(f"Échec : {erreur}")


if __name__ == "__main__":
    main()
