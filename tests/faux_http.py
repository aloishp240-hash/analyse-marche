"""Faux HTTP partagés : réponses préparées et session qui note les appels (aucun accès réseau)."""


class Reponse:
    def __init__(self, code=200, contenu=None, texte="", octets=b""):
        self.status_code = code
        self._contenu = contenu
        self.text = texte
        self.content = octets

    def json(self):
        if self._contenu is None:
            raise ValueError("pas de JSON")
        return self._contenu


class Session:
    """Remplace requests.Session : note les appels et renvoie des réponses préparées."""

    def __init__(self, *reponses):
        self.reponses = list(reponses) or [Reponse(contenu={})]
        self.appels = []

    def request(self, methode, url, **options):
        self.appels.append({"methode": methode, "url": url, **options})
        return self.reponses.pop(0)
