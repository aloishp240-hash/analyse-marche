"""Boîte d'envoi Telegram : les scripts y déposent leurs messages, envoyer.py les envoie.

Sur GitHub Actions, envoyer.py ne tourne qu'après l'enregistrement réussi de l'état dans le dépôt : si le push
échoue, rien ne part, et le passage suivant, reparti de l'état précédent, refait les mêmes messages. Le fichier est
ignoré par git : il ne vit que le temps d'un lancement.
"""
import json
from pathlib import Path

BOITE = Path(__file__).resolve().parent.parent / "agent" / "a-envoyer.json"


def lire(chemin=BOITE):
    try:
        messages = json.loads(Path(chemin).read_text())
        return messages if isinstance(messages, list) else []
    except (OSError, ValueError):
        return []


def ecrire(messages, chemin=BOITE):
    chemin = Path(chemin)
    if not messages:
        chemin.unlink(missing_ok=True)
        return
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_text(json.dumps(messages, indent=1, ensure_ascii=False) + "\n")


def deposer(texte, chemin=BOITE):
    ecrire(lire(chemin) + [texte], chemin)
