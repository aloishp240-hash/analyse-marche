"""Client HTTP pour les sites de la SEC : User-Agent avec contact, au plus 5 requêtes par seconde, cache disque.

Le contact (SEC_CONTACT) vient des variables d'environnement puis de .env et n'apparaît jamais dans un message.
"""
import hashlib
import json as jsonlib
import os
import time
from pathlib import Path

import requests

from commun import RACINE, charger_env

DELAI = 30  # secondes
CACHE_PAR_DEFAUT = RACINE / "donnees" / "cache_sec"


class ErreurSec(Exception):
    pass


def _reglage(nom, env):
    return os.environ.get(nom) or env.get(nom)


class ClientSec:
    def __init__(self, contact=None, session=None, dossier_cache=None, pause=0.2):
        contact = contact or _reglage("SEC_CONTACT", charger_env())
        if not contact:
            raise ErreurSec("SEC_CONTACT manquant (.env ou secrets GitHub) : la SEC exige un contact")
        self._contact = contact
        self.session = session or requests.Session()
        self.dossier_cache = Path(dossier_cache) if dossier_cache else CACHE_PAR_DEFAUT
        self.pause = pause
        self._dernier = None
        self._entetes = {"User-Agent": f"analyse-marche {contact}"}

    def __repr__(self):
        return "ClientSec()"

    def _masquer(self, texte):
        return texte.replace(self._contact, "***")

    def _fichier_cache(self, url):
        return self.dossier_cache / hashlib.sha256(url.encode()).hexdigest()[:32]

    def _attendre(self):
        if self._dernier is not None:
            reste = self.pause - (time.monotonic() - self._dernier)
            if reste > 0:
                time.sleep(reste)

    def octets(self, url, cache=False):
        """Contenu brut de la page, ou None si elle n'existe pas (404)."""
        fichier = self._fichier_cache(url) if cache else None
        if fichier is not None and fichier.exists():
            return fichier.read_bytes()
        self._attendre()
        try:
            reponse = self.session.request("GET", url, headers=self._entetes, timeout=DELAI)
        except requests.RequestException as erreur:
            raise ErreurSec(self._masquer(f"SEC injoignable : {erreur}")) from None
        finally:
            self._dernier = time.monotonic()
        if reponse.status_code == 404:
            return None
        if reponse.status_code >= 400:
            raise ErreurSec(f"la SEC a répondu {reponse.status_code} pour {url}")
        if fichier is not None:
            fichier.parent.mkdir(parents=True, exist_ok=True)
            fichier.write_bytes(reponse.content)
        return reponse.content

    def texte(self, url, cache=False):
        """Contenu de la page décodé en UTF-8, ou None si elle n'existe pas (404)."""
        contenu = self.octets(url, cache=cache)
        return None if contenu is None else contenu.decode("utf-8", errors="replace")

    def json(self, url, cache=False):
        contenu = self.texte(url, cache=cache)
        if contenu is None:
            return None
        try:
            return jsonlib.loads(contenu)
        except ValueError:
            if cache:  # un contenu illisible ne doit pas rester en cache : le passage suivant retélécharge
                self._fichier_cache(url).unlink(missing_ok=True)
            raise ErreurSec(f"réponse de la SEC illisible pour {url}") from None
