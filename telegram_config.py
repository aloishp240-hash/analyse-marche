#!/usr/bin/env python3
"""Trouve ton chat_id Telegram et envoie un message de test.

Prérequis : TELEGRAM_TOKEN rempli dans .env, et tu as envoyé un message (« /start ») à ton bot.
"""
import sys

import requests

from commun import RACINE, charger_env, envoyer_telegram

env = charger_env()
jeton = env.get("TELEGRAM_TOKEN")
if not jeton:
    sys.exit("Ajoute d'abord TELEGRAM_TOKEN=... dans le fichier .env")

if not env.get("TELEGRAM_CHAT_ID"):
    mises_a_jour = requests.get(f"https://api.telegram.org/bot{jeton}/getUpdates", timeout=20).json()
    chats = {m["message"]["chat"]["id"] for m in mises_a_jour.get("result", []) if "message" in m}
    if not chats:
        sys.exit("Aucun message reçu : envoie « /start » à ton bot sur Telegram puis relance ce script.")
    chat_id = chats.pop()
    with open(RACINE / ".env", "a") as fichier:
        fichier.write(f"\nTELEGRAM_CHAT_ID={chat_id}\n")
    print(f"chat_id {chat_id} enregistré dans .env")

envoyer_telegram("✅ Analyse marché : les alertes Nasdaq 100 arriveront ici.")
print("Message de test envoyé, vérifie Telegram.")
