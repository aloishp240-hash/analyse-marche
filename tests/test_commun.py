import pytest
import requests

import commun


def test_erreur_telegram_sans_jeton(monkeypatch):
    # Le message d'erreur de requests contient l'URL, donc le jeton : il ne doit jamais sortir
    monkeypatch.setenv("TELEGRAM_TOKEN", "123:SECRET")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")

    def post(url, **kwargs):
        raise requests.ConnectionError(f"Max retries exceeded with url: {url}")

    monkeypatch.setattr(commun.requests, "post", post)
    with pytest.raises(RuntimeError) as erreur:
        commun.envoyer_telegram("bonjour")
    assert "SECRET" not in str(erreur.value) and erreur.value.__cause__ is None
