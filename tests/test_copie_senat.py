import json
from datetime import date
from pathlib import Path

import pytest
import requests

from inities import senat
from inities.senat import (AccordNonAccepte, ErreurSenat, lire_declaration, lire_recherche, ouvrir_session,
                           rechercher, telecharger)
from tests.faux_http import Reponse, Session

DONNEES = Path(__file__).parent / "donnees" / "copie"
UUID = "3f2b8c1e-5d4a-4e7b-9a10-1c2d3e4f5a6b"
ENTREE = {"uuid": UUID, "nom": "Doe", "prenom": "Jane", "date_publication": date(2026, 10, 12), "papier": False}
CONFIG = {"copie": {"senat_accepte": True}}
ACCUEIL = '<html><form><input type="hidden" name="csrfmiddlewaretoken" value="jeton-test"></form></html>'


def _recherche():
    return json.loads((DONNEES / "senat-recherche.json").read_text())


def _page():
    return (DONNEES / "senat-ptr.html").read_text()


def _session_ouverte():
    return Session(Reponse(texte=ACCUEIL), Reponse(texte="ok"))


def test_refuse_sans_accord():
    for config in ({"copie": {"senat_accepte": False}}, {"copie": {}}, {}, {"copie": {"senat_accepte": "true"}}):
        session = Session()
        with pytest.raises(AccordNonAccepte):
            ouvrir_session(config, session=session)
        assert session.appels == []


def test_ouvrir_session_accepte_l_accord_du_site():
    session = _session_ouverte()
    assert ouvrir_session(CONFIG, session=session) is session
    lecture, accord = session.appels
    assert (lecture["methode"], lecture["url"]) == ("GET", "https://efdsearch.senate.gov/search/home/")
    assert (accord["methode"], accord["url"]) == ("POST", "https://efdsearch.senate.gov/search/home/")
    assert accord["data"] == {"prohibition_agreement": "1", "csrfmiddlewaretoken": "jeton-test"}
    assert lecture["headers"]["User-Agent"] == "analyse-marche"


def test_ouvrir_session_sans_jeton_ou_refus_leve_une_erreur():
    with pytest.raises(ErreurSenat, match="jeton"):
        ouvrir_session(CONFIG, session=Session(Reponse(texte="<html></html>")))
    with pytest.raises(ErreurSenat, match="503"):
        ouvrir_session(CONFIG, session=Session(Reponse(code=503)))
    with pytest.raises(ErreurSenat, match="403"):
        ouvrir_session(CONFIG, session=Session(Reponse(texte=ACCUEIL), Reponse(code=403)))


def test_recherche_marque_le_papier():
    lignes = lire_recherche(_recherche())
    assert [l["papier"] for l in lignes] == [False, True]
    assert lignes[0] == {"uuid": UUID, "nom": "Doe", "prenom": "Jane", "date_publication": date(2026, 10, 12),
                         "papier": False}


def test_recherche_ignore_les_lignes_incomplete():
    donnees = {"data": [["A", "B", "B, A", "pas de lien", "10/12/2026"],
                        ["A", "B", "B, A", '<a href="/search/view/ptr/../../etc/">x</a>', "10/12/2026"],
                        ["A", "B", "B, A", f'<a href="/search/view/ptr/{UUID}/">x</a>', "pas une date"],
                        ["trop court"]]}
    assert lire_recherche(donnees) == []


def test_declaration():
    ops = lire_declaration(_page(), ENTREE)
    assert [(o.ticker, o.sens, o.montant_min) for o in ops] == [("NVDA", "achat", 15001), ("AAPL", "vente", 1001)]


def test_declaration_details():
    ops = lire_declaration(_page(), ENTREE)
    assert ops[0].famille == "politicien" and ops[0].personne == "Jane Doe"
    assert ops[0].personne_id == "politicien:senat:Doe|Jane"
    assert (ops[0].montant_min, ops[0].montant_max) == (15001, 50000)
    assert (ops[0].date_operation, ops[0].date_publication) == (date(2026, 9, 29), date(2026, 10, 12))
    assert [o.source_id for o in ops] == [f"senat:{UUID}:1", f"senat:{UUID}:2"]
    assert [o.details for o in ops] == [{"proprietaire": "Spouse"}, {"proprietaire": "Self"}]   # le commentaire est ignoré


def test_declaration_sans_tableau_illisible():
    assert lire_declaration("<html><body>Please sign in</body></html>", ENTREE) is None
    assert lire_declaration("", ENTREE) is None
    sans_colonne = "<table><thead><tr><th>#</th><th>Ticker</th></tr></thead><tbody><tr><td>1</td><td>X</td></tr></tbody></table>"
    assert lire_declaration(sans_colonne, ENTREE) is None


def test_declaration_vide_est_lisible():
    page = _page().split("<tbody>")[0] + "<tbody></tbody></table>"
    assert lire_declaration(page, ENTREE) == []


def test_declaration_ignore_les_lignes_defectueuses():
    ligne = "<tr><td>1</td><td>{date}</td><td>Self</td><td>{ticker}</td><td>X</td><td>Stock</td><td>Purchase</td><td>{montant}</td><td></td></tr>"
    lignes = [ligne.format(date="09/29/2026", ticker="OK", montant="$1,001 - $15,000"),
              ligne.format(date="pas une date", ticker="BAD1", montant="$1,001 - $15,000"),
              ligne.format(date="09/29/2026", ticker="BAD2", montant="Over $50,000,000"),
              ligne.format(date="09/29/2026", ticker="ticker invalide", montant="$1,001 - $15,000"),
              ligne.format(date="09/29/2026", ticker="", montant="$1,001 - $15,000")]
    page = _page().split("<tbody>")[0] + "<tbody>" + "".join(lignes) + "</tbody></table>"
    assert [o.ticker for o in lire_declaration(page, ENTREE)] == ["OK"]


def test_rechercher_envoie_le_formulaire():
    session = Session(Reponse(contenu=_recherche()))
    lignes = rechercher(session, date(2026, 10, 1), date(2026, 10, 15))
    assert [l["uuid"] for l in lignes][0] == UUID and len(lignes) == 2
    appel = session.appels[0]
    assert (appel["methode"], appel["url"]) == ("POST", "https://efdsearch.senate.gov/search/report/data/")
    assert appel["data"]["report_types"] == "[11]"
    assert appel["data"]["submitted_start_date"] == "10/01/2026 00:00:00"
    assert appel["data"]["submitted_end_date"] == "10/15/2026 23:59:59"
    assert appel["data"]["start"] == 0


def test_rechercher_envoie_le_jeton_csrf_de_la_session():
    session = Session(Reponse(contenu={"data": []}))
    session.cookies = {"csrftoken": "jeton-cookie"}
    rechercher(session, date(2026, 10, 1), date(2026, 10, 15))
    assert session.appels[0]["headers"]["X-CSRFToken"] == "jeton-cookie"
    assert session.appels[0]["data"]["csrfmiddlewaretoken"] == "jeton-cookie"


def test_rechercher_pagine(monkeypatch):
    monkeypatch.setattr(senat, "LONGUEUR_PAGE", 1)
    ligne2 = {"data": [_recherche()["data"][1]]}
    vide = {"data": []}
    session = Session(Reponse(contenu={"data": [_recherche()["data"][0]]}), Reponse(contenu=ligne2),
                      Reponse(contenu=vide))
    lignes = rechercher(session, date(2026, 10, 1), date(2026, 10, 15))
    assert [l["papier"] for l in lignes] == [False, True]
    assert [a["data"]["start"] for a in session.appels] == [0, 1, 2]


def test_rechercher_reponse_invalide():
    with pytest.raises(ErreurSenat, match="500"):
        rechercher(Session(Reponse(code=500)), date(2026, 10, 1), date(2026, 10, 15))
    with pytest.raises(ErreurSenat, match="JSON"):
        rechercher(Session(Reponse(texte="<html>")), date(2026, 10, 1), date(2026, 10, 15))


def test_telecharger_refuse_sans_accord(tmp_path):
    session = Session()
    with pytest.raises(AccordNonAccepte):
        telecharger({"copie": {"senat_accepte": False}}, date(2026, 10, 1), date(2026, 10, 15), tmp_path,
                    session=session)
    assert session.appels == []


def test_telecharger_met_les_pages_en_cache_et_compte_le_papier(tmp_path):
    session = Session(Reponse(texte=ACCUEIL), Reponse(texte="ok"), Reponse(contenu=_recherche()),
                      Reponse(texte=_page()))
    ops, lues, illisibles = telecharger(CONFIG, date(2026, 10, 1), date(2026, 10, 15), tmp_path, session=session)
    assert len(ops) == 2 and lues == 1 and illisibles == 1       # la déclaration papier est illisible
    urls = [a["url"] for a in session.appels]
    assert urls[-1] == f"https://efdsearch.senate.gov/search/view/ptr/{UUID}/"
    assert not any("paper" in u for u in urls)                    # le papier n'est pas téléchargé
    assert (tmp_path / f"{UUID}.html").exists()
    # second passage : la page vient du cache
    session2 = Session(Reponse(texte=ACCUEIL), Reponse(texte="ok"), Reponse(contenu=_recherche()))
    ops2, lues2, _ = telecharger(CONFIG, date(2026, 10, 1), date(2026, 10, 15), tmp_path, session=session2)
    assert ops2 == ops and lues2 == 1 and len(session2.appels) == 3


def test_page_illisible_n_est_pas_mise_en_cache(tmp_path):
    session = Session(Reponse(texte=ACCUEIL), Reponse(texte="ok"), Reponse(contenu=_recherche()),
                      Reponse(texte="<html>Please sign in</html>"))
    assert telecharger(CONFIG, date(2026, 10, 1), date(2026, 10, 15), tmp_path, session=session) == ([], 0, 2)
    assert not (tmp_path / f"{UUID}.html").exists()
    session = Session(Reponse(texte=ACCUEIL), Reponse(texte="ok"), Reponse(contenu=_recherche()), Reponse(code=404))
    assert telecharger(CONFIG, date(2026, 10, 1), date(2026, 10, 15), tmp_path, session=session) == ([], 0, 2)


def _deux_declarations():
    donnees = _recherche()
    autre = "9b8a7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"
    donnees["data"][1][3] = f'<a href="/search/view/ptr/{autre}/">Periodic Transaction Report</a>'
    return donnees, autre


def test_telecharger_continue_apres_une_erreur_reseau(tmp_path):
    donnees, autre = _deux_declarations()

    class SessionCapricieuse:
        def request(self, methode, url, **options):
            if url.endswith("/search/home/"):
                return Reponse(texte=ACCUEIL)
            if url.endswith("/report/data/"):
                return Reponse(contenu=donnees)
            if url.endswith(f"/{UUID}/"):
                raise requests.ConnectionError("coupure")
            return Reponse(texte=_page())

    ops, lues, illisibles = telecharger(CONFIG, date(2026, 10, 1), date(2026, 10, 15), tmp_path,
                                        session=SessionCapricieuse())
    assert (lues, illisibles) == (1, 1) and len(ops) == 2
    assert {o.source_id.split(":")[1] for o in ops} == {autre}


def test_telecharger_attrape_toute_erreur_d_analyse(tmp_path, monkeypatch):
    donnees, autre = _deux_declarations()
    vrai = senat.lire_declaration

    def piegee(html, entree):
        if entree["uuid"] == UUID:
            raise RecursionError("page piégée")
        return vrai(html, entree)

    monkeypatch.setattr(senat, "lire_declaration", piegee)
    session = Session(Reponse(texte=ACCUEIL), Reponse(texte="ok"), Reponse(contenu=donnees),
                      Reponse(texte=_page()), Reponse(texte=_page()))
    ops, lues, illisibles = telecharger(CONFIG, date(2026, 10, 1), date(2026, 10, 15), tmp_path, session=session)
    assert (lues, illisibles) == (1, 1) and {o.source_id.split(":")[1] for o in ops} == {autre}


def test_page_404_est_introuvable_et_une_autre_erreur_est_une_panne(tmp_path):
    assert senat._page(Session(Reponse(code=404)), ENTREE, tmp_path) == (None, False)
    for code in (429, 500, 503):
        with pytest.raises(ErreurSenat, match=str(code)):
            senat._page(Session(Reponse(code=code)), ENTREE, tmp_path)
