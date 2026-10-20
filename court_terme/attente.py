"""Signaux et informations d'une session Claude, appliqués par agent.py seulement si la session réussit.

Format : {"etat": état du portefeuille avec les nouveaux ordres (ou None), "messages": [textes Telegram]}.
"""
import json

from court_terme.portefeuille import sauver


def lire(chemin):
    try:
        return json.loads(chemin.read_text())
    except (OSError, ValueError):
        return {"etat": None, "messages": []}


def ajouter(chemin, message, etat=None):
    attente = lire(chemin)
    if etat is not None:
        attente["etat"] = etat
    attente["messages"].append(message)
    sauver(attente, chemin)
