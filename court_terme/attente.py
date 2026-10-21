"""Signaux et informations d'une session Claude, appliqués par agent.py seulement si la session réussit.

Format : {"etat": état du portefeuille avec les nouveaux ordres (ou None), "messages": [textes Telegram],
"refus": [{"ticker", "outil", "motif"}] (demandes de Claude refusées par le code), "fermetures": nombre}.
"""
import json

from court_terme.portefeuille import sauver


def lire(chemin):
    try:
        return json.loads(chemin.read_text())
    except (OSError, ValueError):
        return {"etat": None, "messages": []}


def noter_refus(chemin, ticker, outil, motif):
    attente = lire(chemin)
    attente.setdefault("refus", []).append({"ticker": ticker, "outil": outil, "motif": motif})
    sauver(attente, chemin)


def ajouter(chemin, message, etat=None, fermeture=False):
    attente = lire(chemin)
    if etat is not None:
        attente["etat"] = etat
    attente["messages"].append(message)
    if fermeture:
        attente["fermetures"] = attente.get("fermetures", 0) + 1
    sauver(attente, chemin)
