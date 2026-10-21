import json
from datetime import date

import pytest
import requests

from inities import sec
from inities.modele import Operation, delai, depuis_dict, vers_dict
from inities.sec import ClientSec, ErreurSec
from tests.faux_http import Reponse, Session


def test_aller_retour_json():
    op = Operation("politicien", "politicien:chambre:Jackson|Jonathan|IL01", "Jonathan Jackson", "DDOG", "achat",
                   date(2026, 9, 28), date(2026, 10, 19), 50001, 100000, "chambre:20035551:1", {"proprietaire": "JT"})
    assert depuis_dict(json.loads(json.dumps(vers_dict(op)))) == op
    assert delai(op) == 21


def test_dates_en_iso_dans_le_dict():
    op = Operation("fonds", "fonds:1", "Fonds", "AAPL", "achat", date(2026, 6, 30), date(2026, 8, 14), 1, 2, "13f:x:y")
    d = vers_dict(op)
    assert d["date_operation"] == "2026-06-30" and d["date_publication"] == "2026-08-14"
    assert d["details"] == {}


def test_client_sec_entete_et_rythme(monkeypatch):
    c = ClientSec(contact="moi@exemple.org", session=Session(Reponse(octets=b"a"), Reponse(octets=b"b")), pause=0)
    assert c.texte("https://www.sec.gov/x") == "a"
    assert c.texte("https://www.sec.gov/y") == "b"
    assert c.session.appels[0]["headers"]["User-Agent"] == "analyse-marche moi@exemple.org"
    assert c.session.appels[0]["methode"] == "GET" and c.session.appels[0]["timeout"] == sec.DELAI


def test_client_sec_attend_entre_deux_requetes(monkeypatch):
    heure = [100.0]
    attentes = []
    monkeypatch.setattr(sec.time, "monotonic", lambda: heure[0])
    monkeypatch.setattr(sec.time, "sleep", lambda s: attentes.append(s))
    c = ClientSec(contact="moi@exemple.org", session=Session(Reponse(octets=b"a"), Reponse(octets=b"b")), pause=0.2)
    c.texte("https://www.sec.gov/x")
    heure[0] += 0.05
    c.texte("https://www.sec.gov/y")
    assert len(attentes) == 1 and attentes[0] == pytest.approx(0.15)


def test_client_sec_sans_contact(monkeypatch):
    monkeypatch.delenv("SEC_CONTACT", raising=False)
    monkeypatch.setattr(sec, "charger_env", lambda: {})
    with pytest.raises(ErreurSec, match="SEC_CONTACT"):
        ClientSec(session=Session())


def test_contact_lu_dans_l_environnement_avant_le_fichier(monkeypatch):
    monkeypatch.setenv("SEC_CONTACT", "env@exemple.org")
    monkeypatch.setattr(sec, "charger_env", lambda: {"SEC_CONTACT": "fichier@exemple.org"})
    c = ClientSec(session=Session(Reponse(octets=b"a")), pause=0)
    c.texte("https://www.sec.gov/x")
    assert c.session.appels[0]["headers"]["User-Agent"] == "analyse-marche env@exemple.org"


def test_erreur_sec_sans_contact_dans_le_texte():
    c = ClientSec(contact="moi@exemple.org", session=Session(Reponse(code=500, texte="moi@exemple.org")), pause=0)
    with pytest.raises(ErreurSec) as e:
        c.texte("https://www.sec.gov/x")
    assert "moi@exemple.org" not in str(e.value)
    assert "500" in str(e.value)


def test_panne_reseau_sans_contact_dans_le_texte():
    class SessionHs:
        def request(self, *args, **options):
            raise requests.ConnectionError("échec pour analyse-marche moi@exemple.org")

    c = ClientSec(contact="moi@exemple.org", session=SessionHs(), pause=0)
    with pytest.raises(ErreurSec) as e:
        c.texte("https://www.sec.gov/x")
    assert "moi@exemple.org" not in str(e.value)


def test_404_renvoie_none():
    assert ClientSec(contact="a@b.c", session=Session(Reponse(code=404)), pause=0).texte("https://x") is None


def test_json_decode_la_reponse():
    c = ClientSec(contact="a@b.c", session=Session(Reponse(octets=b'{"a": 1}'), Reponse(code=404)), pause=0)
    assert c.json("https://x") == {"a": 1}
    assert c.json("https://y") is None


def test_json_invalide_leve_erreur_sec():
    c = ClientSec(contact="a@b.c", session=Session(Reponse(octets=b"pas du json")), pause=0)
    with pytest.raises(ErreurSec):
        c.json("https://x")


def test_cache_disque(tmp_path):
    c = ClientSec(contact="a@b.c", session=Session(Reponse(octets=b"contenu")), dossier_cache=tmp_path, pause=0)
    assert c.texte("https://www.sec.gov/x?a=1", cache=True) == "contenu"
    assert c.texte("https://www.sec.gov/x?a=1", cache=True) == "contenu"
    assert len(c.session.appels) == 1
    assert len(list(tmp_path.iterdir())) == 1


def test_sans_cache_demande_chaque_fois(tmp_path):
    c = ClientSec(contact="a@b.c", session=Session(Reponse(octets=b"1"), Reponse(octets=b"2")), dossier_cache=tmp_path,
                  pause=0)
    assert c.texte("https://www.sec.gov/x") == "1"
    assert c.texte("https://www.sec.gov/x") == "2"
    assert not list(tmp_path.iterdir())


def test_texte_decode_en_utf8_sans_deviner_le_jeu_de_caracteres():
    reponse = Reponse(texte="Ã©", octets="é".encode())  # requests devinerait ISO-8859-1 sans charset
    c = ClientSec(contact="a@b.c", session=Session(reponse), pause=0)
    assert c.texte("https://x") == "é"


def test_texte_octets_invalides_remplaces():
    c = ClientSec(contact="a@b.c", session=Session(Reponse(octets=b"a\xffb")), pause=0)
    assert c.texte("https://x") == "a�b"


def test_octets_entete_rythme_et_404():
    c = ClientSec(contact="moi@exemple.org", session=Session(Reponse(octets=b"PK\x03\x04"), Reponse(code=404)), pause=0)
    assert c.octets("https://www.sec.gov/z.zip") == b"PK\x03\x04"
    assert c.octets("https://www.sec.gov/absent.zip") is None
    assert c.session.appels[0]["headers"]["User-Agent"] == "analyse-marche moi@exemple.org"
    assert c.session.appels[0]["timeout"] == sec.DELAI


def test_octets_attend_entre_deux_requetes(monkeypatch):
    heure = [100.0]
    attentes = []
    monkeypatch.setattr(sec.time, "monotonic", lambda: heure[0])
    monkeypatch.setattr(sec.time, "sleep", lambda s: attentes.append(s))
    c = ClientSec(contact="moi@exemple.org", session=Session(Reponse(octets=b"a"), Reponse(octets=b"b")), pause=0.2)
    c.octets("https://www.sec.gov/x")
    c.texte("https://www.sec.gov/y")
    assert len(attentes) == 1 and attentes[0] == pytest.approx(0.2)


def test_octets_erreurs_sans_contact():
    c = ClientSec(contact="moi@exemple.org", session=Session(Reponse(code=503)), pause=0)
    with pytest.raises(ErreurSec, match="503"):
        c.octets("https://www.sec.gov/x")

    class SessionHs:
        def request(self, *args, **options):
            raise requests.ConnectionError("échec pour analyse-marche moi@exemple.org")

    c = ClientSec(contact="moi@exemple.org", session=SessionHs(), pause=0)
    with pytest.raises(ErreurSec) as e:
        c.octets("https://www.sec.gov/x")
    assert "moi@exemple.org" not in str(e.value)


def test_octets_cache_disque(tmp_path):
    c = ClientSec(contact="a@b.c", session=Session(Reponse(octets=b"\x00\x01\xff")), dossier_cache=tmp_path, pause=0)
    assert c.octets("https://www.sec.gov/x.zip", cache=True) == b"\x00\x01\xff"
    assert c.octets("https://www.sec.gov/x.zip", cache=True) == b"\x00\x01\xff"
    assert len(c.session.appels) == 1


def test_json_en_cache_invalide_est_supprime_puis_retelecharge(tmp_path):
    c = ClientSec(contact="a@b.c", session=Session(Reponse(octets=b"pas du json"), Reponse(octets=b'{"a": 1}')),
                  dossier_cache=tmp_path, pause=0)
    with pytest.raises(ErreurSec):
        c.json("https://x", cache=True)
    assert not list(tmp_path.iterdir())
    assert c.json("https://x", cache=True) == {"a": 1}
    assert len(c.session.appels) == 2
    assert len(list(tmp_path.iterdir())) == 1


def test_json_sans_cache_invalide_ne_touche_pas_au_cache(tmp_path):
    c = ClientSec(contact="a@b.c", session=Session(Reponse(octets=b"pas du json")), dossier_cache=tmp_path, pause=0)
    with pytest.raises(ErreurSec):
        c.json("https://x")
    assert not list(tmp_path.iterdir())
