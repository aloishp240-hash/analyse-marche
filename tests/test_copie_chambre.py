import io
import zipfile
from datetime import date
from pathlib import Path

import pytest
import requests

from inities import chambre
from inities.chambre import ErreurChambre, lire_declaration, lire_index, telecharger
from tests.faux_http import Reponse, Session

DONNEES = Path(__file__).parent / "donnees" / "copie"
ENTREE_JACKSON = {"doc_id": "20035551", "nom": "Jackson", "prenom": "Jonathan", "etat": "IL01", "annee": 2026,
                  "date_publication": date(2026, 10, 19)}


def test_index_garde_les_declarations_de_transactions():
    entrees = lire_index((DONNEES / "chambre-index.xml").read_bytes())
    assert [e["doc_id"] for e in entrees] == ["20035551"]
    assert entrees[0]["date_publication"] == date(2026, 10, 19) and entrees[0]["etat"] == "IL01"
    assert entrees[0]["nom"] == "Jackson" and entrees[0]["prenom"] == "Jonathan" and entrees[0]["annee"] == 2026


def test_declaration_reelle():
    ops = lire_declaration((DONNEES / "chambre-20035551.pdf").read_bytes(), ENTREE_JACKSON)
    achats = [(o.ticker, o.montant_min, o.montant_max) for o in ops if o.sens == "achat"]
    assert achats == [("DDOG", 50001, 100000), ("EL", 50001, 100000), ("GH", 15001, 50000)]
    assert {o.ticker for o in ops if o.sens == "vente"} == {"TYG", "VSAT", "WMT"}
    assert all(o.date_operation in (date(2026, 9, 28), date(2026, 9, 4)) for o in ops)
    assert all(o.date_publication == date(2026, 10, 19) for o in ops)   # date de l'index, pas « Notification Date »
    assert ops[0].personne_id == "politicien:chambre:Jackson|Jonathan|IL01"
    assert len({o.source_id for o in ops}) == len(ops)


def test_declaration_reelle_details():
    ops = lire_declaration((DONNEES / "chambre-20035551.pdf").read_bytes(), ENTREE_JACKSON)
    assert ops[0].famille == "politicien" and ops[0].personne == "Jonathan Jackson"
    assert ops[0].source_id == "chambre:20035551:1" and ops[-1].source_id == "chambre:20035551:6"
    assert [o.details.get("proprietaire") for o in ops] == ["JT", "JT", "JT", "SP", "JT", "JT"]


def test_pdf_scanne_illisible():
    assert lire_declaration((DONNEES / "chambre-scan.pdf").read_bytes(), ENTREE_JACKSON) is None


def test_pdf_corrompu_illisible():
    assert lire_declaration(b"pas un pdf", ENTREE_JACKSON) is None


def test_ventes_partielles_et_autres_types(monkeypatch):
    texte = ("JT Alpha Corp (ABC) [ST]\nS (partial) 01/05/2026 01/07/2026 $1,001 -\n$15,000\n"
             "F S: New\nJT Beta Corp (BR.B) [ST]\nE 01/05/2026 01/07/2026 $1,001 - $15,000\nF S: New\n"
             "DC Gamma Corp (GAM) [ST]\nP 02/05/2026 02/07/2026 $15,001 - $50,000\nF S: New\n"
             "JT Berkshire Hathaway Inc. (BRK-B) [ST]\nP 03/05/2026 03/07/2026 $1,001 - $15,000\nF S: New\n"
             "Obligation Delta (DEL) [GS]\nP 02/05/2026 02/07/2026 $15,001 - $50,000\n")
    monkeypatch.setattr(chambre, "_texte_pdf", lambda pdf: texte)
    ops = lire_declaration(b"x", ENTREE_JACKSON)
    assert [(o.ticker, o.sens, o.date_operation, o.details["proprietaire"]) for o in ops] == [
        ("ABC", "vente", date(2026, 1, 5), "JT"), ("GAM", "achat", date(2026, 2, 5), "DC"),
        ("BRK-B", "achat", date(2026, 3, 5), "JT")]


def _zip_index():
    tampon = io.BytesIO()
    with zipfile.ZipFile(tampon, "w") as z:
        z.writestr("2026FD.txt", "ignoré")
        z.writestr("2026FD.xml", (DONNEES / "chambre-index.xml").read_bytes())
    return tampon.getvalue()


def test_telecharger_met_les_pdf_en_cache(tmp_path):
    pdf = (DONNEES / "chambre-20035551.pdf").read_bytes()
    session = Session(Reponse(octets=_zip_index()), Reponse(octets=pdf))
    ops, lues, illisibles = telecharger([2026], tmp_path, session=session)
    assert len(ops) == 6 and lues == 1 and illisibles == 0
    assert session.appels[0]["url"] == "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/2026FD.zip"
    assert session.appels[1]["url"] == "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20035551.pdf"
    assert session.appels[0]["headers"] == {"User-Agent": "analyse-marche"}
    # second passage : le PDF vient du cache, seul l'index est redemandé
    session2 = Session(Reponse(octets=_zip_index()))
    ops2, lues2, _ = telecharger([2026], tmp_path, session=session2)
    assert ops2 == ops and lues2 == 1 and len(session2.appels) == 1


def test_telecharger_compte_les_illisibles(tmp_path):
    scan = (DONNEES / "chambre-scan.pdf").read_bytes()
    session = Session(Reponse(octets=_zip_index()), Reponse(octets=scan))
    assert telecharger([2026], tmp_path, session=session) == ([], 0, 1)
    session = Session(Reponse(octets=_zip_index()), Reponse(code=404))
    assert telecharger([2026], tmp_path / "autre", session=session) == ([], 0, 1)


def test_telecharger_continue_apres_une_erreur_reseau(tmp_path):
    xml = (DONNEES / "chambre-index.xml").read_bytes().replace(b"</FinancialDisclosure>", b"""
  <Member><Last>Autre</Last><First>Anne</First><FilingType>P</FilingType><StateDst>TX01</StateDst>
    <Year>2026</Year><FilingDate>10/20/2026</FilingDate><DocID>20035552</DocID></Member>
</FinancialDisclosure>""")
    tampon = io.BytesIO()
    with zipfile.ZipFile(tampon, "w") as z:
        z.writestr("2026FD.xml", xml)
    pdf = (DONNEES / "chambre-20035551.pdf").read_bytes()

    class SessionCapricieuse:
        def request(self, methode, url, **options):
            if url.endswith("FD.zip"):
                return Reponse(octets=tampon.getvalue())
            if url.endswith("/20035551.pdf"):
                return Reponse(octets=pdf)
            raise requests.ConnectionError("coupure")

    ops, lues, illisibles = telecharger([2026], tmp_path, session=SessionCapricieuse())
    assert (lues, illisibles) == (1, 1)
    assert len(ops) == 6 and {o.source_id.split(":")[1] for o in ops} == {"20035551"}


def test_telecharger_attrape_toute_erreur_de_lecture_pdf(tmp_path, monkeypatch):
    monkeypatch.setattr(chambre, "_texte_pdf", lambda pdf: (_ for _ in ()).throw(RecursionError("pdf piégé")))
    session = Session(Reponse(octets=_zip_index()), Reponse(octets=b"%PDF"))
    assert telecharger([2026], tmp_path, session=session) == ([], 0, 1)


def test_index_invalide_leve_une_erreur_claire(tmp_path):
    with pytest.raises(ErreurChambre, match="zip"):
        telecharger([2026], tmp_path, session=Session(Reponse(octets=b"pas un zip")))
    tampon = io.BytesIO()
    with zipfile.ZipFile(tampon, "w") as z:
        z.writestr("2026FD.txt", "sans xml")
    with pytest.raises(ErreurChambre, match="XML"):
        telecharger([2026], tmp_path, session=Session(Reponse(octets=tampon.getvalue())))
    with pytest.raises(ErreurChambre, match="XML"):
        lire_index(b"<pas du xml")


def test_membre_sans_date_ignore():
    xml = (b"<FinancialDisclosure><Member><Last>A</Last><First>B</First><FilingType>P</FilingType>"
           b"<StateDst>TX01</StateDst><Year>2026</Year><DocID>1</DocID></Member></FinancialDisclosure>")
    assert lire_index(xml) == []


def test_pdf_404_est_introuvable_et_une_autre_erreur_est_une_panne(tmp_path):
    assert chambre._pdf(Session(Reponse(code=404)), ENTREE_JACKSON, tmp_path) is None
    for code in (429, 500, 503):
        with pytest.raises(ErreurChambre, match=str(code)):
            chambre._pdf(Session(Reponse(code=code)), ENTREE_JACKSON, tmp_path)
