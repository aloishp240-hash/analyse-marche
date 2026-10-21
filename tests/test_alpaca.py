import pytest

from court_terme import alpaca
from court_terme.alpaca import URL_PAPER, Client, ErreurAlpaca

CLE = "PKTESTCLE123"
SECRET = "secret-tres-prive-456"


class Reponse:
    def __init__(self, code=200, contenu=None, texte=""):
        self.status_code = code
        self._contenu = contenu
        self.text = texte

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


@pytest.fixture(autouse=True)
def sans_env(monkeypatch):
    for nom in ("ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY", "ALPACA_BASE_URL"):
        monkeypatch.delenv(nom, raising=False)
    monkeypatch.setattr(alpaca, "charger_env", lambda: {})


def client(*reponses, env=None):
    env = {"ALPACA_API_KEY_ID": CLE, "ALPACA_API_SECRET_KEY": SECRET, **(env or {})}
    return Client(env=env, session=Session(*reponses))


def test_cles_manquantes_refusees():
    with pytest.raises(ErreurAlpaca, match="ALPACA_API_KEY_ID"):
        Client(env={}, session=Session())


def test_cles_lues_dans_l_environnement_avant_le_fichier(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY_ID", CLE)
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", SECRET)
    monkeypatch.setattr(alpaca, "charger_env", lambda: {"ALPACA_API_KEY_ID": "PKAUTRE", "ALPACA_API_SECRET_KEY": "x"})
    session = Session()
    Client(session=session).compte()
    assert session.appels[0]["headers"]["APCA-API-KEY-ID"] == CLE


def test_url_reelle_refusee():
    with pytest.raises(ErreurAlpaca, match="paper"):
        client(env={"ALPACA_BASE_URL": "https://api.alpaca.markets"})


def test_url_paper_explicite_acceptee():
    session = Session()
    Client(env={"ALPACA_API_KEY_ID": CLE, "ALPACA_API_SECRET_KEY": SECRET,
                "ALPACA_BASE_URL": URL_PAPER + "/"}, session=session).compte()
    assert session.appels[0]["url"] == URL_PAPER + "/v2/account"


def test_cle_de_compte_reel_refusee():
    with pytest.raises(ErreurAlpaca, match="compte réel"):
        Client(env={"ALPACA_API_KEY_ID": "AKREELLE", "ALPACA_API_SECRET_KEY": SECRET}, session=Session())


def test_compte_appelle_le_serveur_paper_avec_les_cles():
    c = client(Reponse(contenu={"cash": "3000", "status": "ACTIVE"}))
    assert c.compte()["cash"] == "3000"
    appel = c.session.appels[0]
    assert (appel["methode"], appel["url"]) == ("GET", URL_PAPER + "/v2/account")
    assert appel["headers"] == {"APCA-API-KEY-ID": CLE, "APCA-API-SECRET-KEY": SECRET}
    assert appel["timeout"] > 0


def test_ordre_bracket_bien_forme():
    c = client(Reponse(contenu={"id": "abc"}))
    assert c.passer_bracket("NVDA", 3, entree=101.234, stop=95.0, objectif=112.5, id_client="ct-1")["id"] == "abc"
    appel = c.session.appels[0]
    assert (appel["methode"], appel["url"]) == ("POST", URL_PAPER + "/v2/orders")
    assert appel["json"] == {
        "symbol": "NVDA", "qty": "3", "side": "buy", "type": "limit", "limit_price": "101.23",
        "time_in_force": "gtc", "order_class": "bracket",
        "take_profit": {"limit_price": "112.50"}, "stop_loss": {"stop_price": "95.00"},
        "client_order_id": "ct-1",
    }


@pytest.mark.parametrize("quantite", [0, -1, 1.5])
def test_bracket_refuse_une_quantite_non_entiere(quantite):
    c = client()
    with pytest.raises(ErreurAlpaca, match="entier"):
        c.passer_bracket("NVDA", quantite, entree=100, stop=95, objectif=110, id_client="x")
    assert c.session.appels == []


@pytest.mark.parametrize("stop, objectif", [(101, 110), (95, 99), (95, 100)])
def test_bracket_refuse_des_niveaux_incoherents(stop, objectif):
    c = client()
    with pytest.raises(ErreurAlpaca, match="stop < entrée < objectif"):
        c.passer_bracket("NVDA", 1, entree=100, stop=stop, objectif=objectif, id_client="x")
    assert c.session.appels == []


def test_ordre_oto_bien_forme():
    c = client(Reponse(contenu={"id": "abc"}))
    assert c.passer_oto("NVDA", 3, stop=95.456, id_client="cp-1")["id"] == "abc"
    appel = c.session.appels[0]
    assert (appel["methode"], appel["url"]) == ("POST", URL_PAPER + "/v2/orders")
    assert appel["json"] == {
        "symbol": "NVDA", "qty": "3", "side": "buy", "type": "market", "time_in_force": "gtc",
        "order_class": "oto", "stop_loss": {"stop_price": "95.46"}, "client_order_id": "cp-1",
    }


def test_ordre_oto_validite_choisie():
    c = client(Reponse(contenu={"id": "abc"}))
    c.passer_oto("NVDA", 1, stop=10, id_client="cp-2", validite="day")
    assert c.session.appels[0]["json"]["time_in_force"] == "day"


@pytest.mark.parametrize("quantite", [0, -1, 1.5, 2.0, True, "3", None])
def test_oto_refuse_quantite_ou_stop_invalides(quantite):
    c = client()
    with pytest.raises(ErreurAlpaca, match="entier"):
        c.passer_oto("NVDA", quantite, stop=95, id_client="x")
    assert c.session.appels == []


@pytest.mark.parametrize("stop", [0, -5, 0.0, float("nan"), float("inf"), True, "95", None])
def test_oto_refuse_un_stop_invalide(stop):
    c = client()
    with pytest.raises(ErreurAlpaca, match="stop"):
        c.passer_oto("NVDA", 1, stop=stop, id_client="x")
    assert c.session.appels == []


def test_modifier_stop():
    c = client(Reponse(contenu={"id": "abc", "stop_price": "85.12"}))
    assert c.modifier_stop("abc", 85.123)["id"] == "abc"
    appel = c.session.appels[0]
    assert (appel["methode"], appel["url"]) == ("PATCH", URL_PAPER + "/v2/orders/abc")
    assert appel["json"] == {"stop_price": "85.12"}


@pytest.mark.parametrize("stop", [0, -1, float("nan"), float("inf"), True, "85"])
def test_modifier_stop_refuse_un_stop_invalide(stop):
    c = client()
    with pytest.raises(ErreurAlpaca, match="stop"):
        c.modifier_stop("abc", stop)
    assert c.session.appels == []


def test_fermer_position():
    c = client(Reponse(contenu={"id": "ordre-vente", "side": "sell"}))
    assert c.fermer_position("NVDA")["side"] == "sell"
    appel = c.session.appels[0]
    assert (appel["methode"], appel["url"]) == ("DELETE", URL_PAPER + "/v2/positions/NVDA")


def test_fermer_position_reponse_sans_corps():
    c = client(Reponse(code=204))
    assert c.fermer_position("NVDA") == {}


def test_fermer_position_erreur_du_serveur():
    c = client(Reponse(code=404, contenu={"message": "position does not exist"}))
    with pytest.raises(ErreurAlpaca, match="404"):
        c.fermer_position("NVDA")


def test_annuler_un_ordre():
    c = client(Reponse(code=204))
    c.annuler("abc")
    appel = c.session.appels[0]
    assert (appel["methode"], appel["url"]) == ("DELETE", URL_PAPER + "/v2/orders/abc")


def test_ordres_et_positions():
    c = client(Reponse(contenu=[{"id": "a"}]), Reponse(contenu=[{"symbol": "NVDA"}]))
    assert c.ordres() == [{"id": "a"}]
    assert c.session.appels[0]["params"] == {"status": "open", "nested": "true"}
    assert c.positions() == [{"symbol": "NVDA"}]
    assert c.session.appels[1]["url"] == URL_PAPER + "/v2/positions"


def test_erreur_du_serveur_sans_les_cles():
    c = client(Reponse(code=403, contenu={"message": f"forbidden {CLE} {SECRET}"}))
    with pytest.raises(ErreurAlpaca) as erreur:
        c.compte()
    texte = str(erreur.value)
    assert "403" in texte and "forbidden" in texte
    assert CLE not in texte and SECRET not in texte


def test_cles_absentes_de_la_representation_du_client():
    c = client()
    assert CLE not in repr(c) and SECRET not in repr(c)


class FauxClient:
    def __init__(self, statut_apres_annulation="canceled", echec_lecture=False):
        self.appels = []
        self.statut = "new"
        self.statut_apres_annulation = statut_apres_annulation
        self.echec_lecture = echec_lecture

    def compte(self):
        return {"status": "ACTIVE", "cash": "3000", "equity": "3000", "buying_power": "6000",
                "trading_blocked": False}

    def passer_bracket(self, ticker, quantite, entree, stop, objectif, id_client):
        self.appels.append(("bracket", ticker, quantite, entree, stop, objectif))
        return {"id": "o1", "status": "new"}

    def ordre(self, identifiant):
        if self.echec_lecture:
            raise ErreurAlpaca("panne")
        return {"id": identifiant, "status": self.statut, "order_class": "bracket", "legs": [{}, {}]}

    def annuler(self, identifiant):
        self.appels.append(("annuler", identifiant))
        self.statut = self.statut_apres_annulation


def test_verification_passe_un_ordre_loin_du_cours_puis_l_annule():
    import alpaca_test
    faux = FauxClient()
    lignes = alpaca_test.verifier(faux, "AAPL", cours=200.0, attente=0)
    bracket = faux.appels[0]
    assert bracket[:3] == ("bracket", "AAPL", 1)
    assert bracket[3] == 100.0 and bracket[4] < bracket[3] < bracket[5]
    assert faux.appels[-1] == ("annuler", "o1")
    assert any("annulé" in l for l in lignes)


def test_verification_annule_meme_si_la_relecture_echoue():
    import alpaca_test
    faux = FauxClient(echec_lecture=True)
    with pytest.raises(ErreurAlpaca):
        alpaca_test.verifier(faux, "AAPL", cours=200.0, attente=0)
    assert faux.appels[-1] == ("annuler", "o1")
