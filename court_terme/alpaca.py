"""Connexion au compte Alpaca **paper** (fictif) : compte, positions, ordres bracket, annulation.

Seule l'adresse paper est acceptée : une adresse de compte réel ou une clé de compte réel (préfixe « AK »)
bloque le démarrage. Les clés viennent des variables d'environnement (secrets GitHub) puis de .env, et
n'apparaissent jamais dans un message.
"""
import math
import os

import requests

from commun import charger_env

URL_PAPER = "https://paper-api.alpaca.markets"
DELAI = 15  # secondes


class ErreurAlpaca(Exception):
    pass


def _reglage(nom, env):
    return os.environ.get(nom) or env.get(nom)


def _verifier_stop(stop):
    """Le stop doit être un nombre fini qui reste positif une fois arrondi à 2 décimales."""
    if isinstance(stop, bool) or not isinstance(stop, (int, float)) or not math.isfinite(stop) \
            or round(stop, 2) <= 0:
        raise ErreurAlpaca(f"le stop doit être un nombre fini supérieur à 0 ({stop!r})")


class Client:
    def __init__(self, env=None, session=None):
        env = charger_env() if env is None else env
        url = (_reglage("ALPACA_BASE_URL", env) or URL_PAPER).rstrip("/")
        if url != URL_PAPER:
            raise ErreurAlpaca(f"refusé : seul le compte paper ({URL_PAPER}) est autorisé")
        cle, secret = _reglage("ALPACA_API_KEY_ID", env), _reglage("ALPACA_API_SECRET_KEY", env)
        if not cle or not secret:
            raise ErreurAlpaca("ALPACA_API_KEY_ID ou ALPACA_API_SECRET_KEY manquant (.env ou secrets GitHub)")
        if cle.startswith("AK"):
            raise ErreurAlpaca("refusé : cette clé est celle d'un compte réel, il faut une clé paper (PK…)")
        self.url = url
        self.session = session or requests.Session()
        self._entetes = {"APCA-API-KEY-ID": cle, "APCA-API-SECRET-KEY": secret}

    def __repr__(self):
        return f"Client({self.url})"

    def _appel(self, methode, chemin, **options):
        try:
            reponse = self.session.request(methode, self.url + chemin, headers=self._entetes, timeout=DELAI,
                                           **options)
        except requests.RequestException as erreur:
            raise ErreurAlpaca(self._masquer(f"Alpaca injoignable : {erreur}")) from None
        if reponse.status_code >= 400:
            try:
                detail = reponse.json().get("message", "")
            except (ValueError, AttributeError):
                detail = reponse.text[:200]
            raise ErreurAlpaca(self._masquer(f"Alpaca a répondu {reponse.status_code} : {detail}"))
        if reponse.status_code == 204:
            return None
        return reponse.json()

    def _masquer(self, texte):
        for valeur in self._entetes.values():
            texte = texte.replace(valeur, "***")
        return texte

    def compte(self):
        return self._appel("GET", "/v2/account")

    def positions(self):
        return self._appel("GET", "/v2/positions")

    def ordres(self, statut="open"):
        return self._appel("GET", "/v2/orders", params={"status": statut, "nested": "true"})

    def ordre(self, identifiant):
        return self._appel("GET", f"/v2/orders/{identifiant}", params={"nested": "true"})

    def passer_bracket(self, ticker, quantite, entree, stop, objectif, id_client, validite="gtc"):
        """Achat à cours limité `entree` ; à l'exécution, Alpaca pose l'objectif et le stop ensemble."""
        if not isinstance(quantite, int) or quantite < 1:
            raise ErreurAlpaca("un ordre bracket demande un nombre entier d'actions (au moins 1)")
        if not stop < entree < objectif:
            raise ErreurAlpaca(f"niveaux incohérents, il faut stop < entrée < objectif ({stop}, {entree}, {objectif})")
        return self._appel("POST", "/v2/orders", json={
            "symbol": ticker, "qty": str(quantite), "side": "buy", "type": "limit",
            "limit_price": f"{entree:.2f}", "time_in_force": validite, "order_class": "bracket",
            "take_profit": {"limit_price": f"{objectif:.2f}"}, "stop_loss": {"stop_price": f"{stop:.2f}"},
            "client_order_id": id_client,
        })

    def passer_oto(self, ticker, quantite, stop, id_client, validite="gtc"):
        """Achat au marché ; à l'exécution, Alpaca pose le stop lié (sans objectif de gain)."""
        if isinstance(quantite, bool) or not isinstance(quantite, int) or quantite < 1:
            raise ErreurAlpaca("un ordre oto demande un nombre entier d'actions (au moins 1)")
        _verifier_stop(stop)
        return self._appel("POST", "/v2/orders", json={
            "symbol": ticker, "qty": str(quantite), "side": "buy", "type": "market",
            "time_in_force": validite, "order_class": "oto", "stop_loss": {"stop_price": f"{stop:.2f}"},
            "client_order_id": id_client,
        })

    def passer_stop(self, ticker, quantite, stop, id_client, validite="gtc"):
        """Ordre stop de vente seul, pour reposer la protection d'une position dont le stop a été annulé."""
        if isinstance(quantite, bool) or not isinstance(quantite, int) or quantite < 1:
            raise ErreurAlpaca("un ordre stop demande un nombre entier d'actions (au moins 1)")
        _verifier_stop(stop)
        return self._appel("POST", "/v2/orders", json={
            "symbol": ticker, "qty": str(quantite), "side": "sell", "type": "stop",
            "time_in_force": validite, "stop_price": f"{stop:.2f}", "client_order_id": id_client,
        })

    def modifier_stop(self, identifiant, stop):
        """Déplace le prix d'un ordre stop déjà posé."""
        _verifier_stop(stop)
        return self._appel("PATCH", f"/v2/orders/{identifiant}", json={"stop_price": f"{stop:.2f}"})

    def fermer_position(self, ticker):
        """Vend toute la position au marché ; Alpaca répond avec l'ordre de vente (ou rien : {})."""
        return self._appel("DELETE", f"/v2/positions/{ticker}") or {}

    def annuler(self, identifiant):
        self._appel("DELETE", f"/v2/orders/{identifiant}")
