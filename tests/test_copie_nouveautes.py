import io
import json
import zipfile
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from inities import chambre, cours, fonds, nouveautes, senat
from inities.modele import Operation
from inities.nouveautes import (charger_vus, collecter, enregistrer_vus, est_doublon, jours_a_lire)
from inities.sec import ClientSec
from tests.faux_http import Reponse, Session

DONNEES = Path(__file__).parent / "donnees" / "copie"
CONTACT = "moi@exemple.org"
AUJOURD_HUI = date(2026, 10, 21)  # mercredi
P = {"senat_accepte": False, "politicien_montant_min": 15000, "dirigeant_montant_min": 100000,
     "groupe_dirigeants_jours": 30, "fonds_poids_min_pct": 3, "capitalisation_min": 2000000000}
CONFIG = {"copie": P}
CONFIG_SENAT = {"copie": {**P, "senat_accepte": True}}
ACCESSION = "0001772409-26-000009"
CHEMIN = f"edgar/data/1772409/{ACCESSION}.txt"
URL_INDEX_20 = "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR4/form.20261020.idx"
URL_DEPOT = "https://www.sec.gov/Archives/" + CHEMIN
JACKSON = "politicien:chambre:Jackson|Jonathan|IL01"
DOE = "politicien:senat:Doe|Jane"
PERSHING = "fonds:1336528"
SUIVIS_FONDS = {"personnes": [{"personne_id": "fonds:1135730", "personne": "Coatue", "famille": "fonds"}]}
SANS_POSITION = {"positions": [], "historique": []}


def vus_vide(dernier="2026-10-19"):
    return {"dernier_passage": dernier, "vus": [], "achats_dirigeants": []}


def suivis_de(*ids):
    return {"personnes": [{"personne_id": i, "personne": i, "famille": "x"} for i in ids]}


def position(personne_id, ticker):
    return {"personne_id": personne_id, "ticker": ticker, "source_id": f"x:{ticker}"}


class Site:
    """Remplace requests.Session : répond selon l'URL, note les URL demandées, 404 sinon."""

    def __init__(self, reponses=None):
        self.reponses = reponses or {}
        self.urls = []

    def request(self, methode, url, **options):
        self.urls.append(url)
        reponse = self.reponses.get(url)
        if isinstance(reponse, Exception):
            raise reponse
        return Reponse(code=404) if reponse is None else reponse


def client(site, tmp_path):
    return ClientSec(contact=CONTACT, session=site, dossier_cache=tmp_path / "cache", pause=0)


def depot_form4():
    xml = (DONNEES / "form4-achat.xml").read_text(encoding="utf-8")
    return f"<SEC-DOCUMENT>\n<XML>\n{xml}\n</XML>\n</SEC-DOCUMENT>".encode()


def index_form4(*chemins):
    lignes = "\n".join(f"4   Societe   1   20261020    {c}" for c in chemins)
    return ("Form Type   Company Name   CIK\n-----\n" + lignes + "\n").encode()


def site_form4(**en_plus):
    reponses = {URL_INDEX_20: Reponse(octets=index_form4(CHEMIN)), URL_DEPOT: Reponse(octets=depot_form4())}
    reponses.update(en_plus)
    return Site(reponses)


class Marche:
    """Faux yfinance : nombre d'actions et dernier cours par ticker, titres limités."""

    def __init__(self):
        self.actions, self.cours, self.limites, self.appels = {}, {}, set(), []

    def fournir(self, tickers, defaut):
        return {t: v for t in tickers if (v := self.actions.get(t, defaut)) is not None}


@pytest.fixture
def marche(monkeypatch):
    m = Marche()

    def actions(tickers, dossier, attendre=None, taille_lot=20, limites=None):
        m.appels.append(("actions", list(tickers)))
        if limites is not None:
            limites.update(t for t in tickers if t in m.limites)
        return {t: m.actions.get(t, 1e9) for t in tickers if t not in m.limites and m.actions.get(t, 1e9) is not None}

    def charger(tickers, debut, fin, dossier, attendre=None, taille_lot=50, progres=None, limites=None):
        m.appels.append(("cours", list(tickers)))
        index = pd.date_range(end=pd.Timestamp(fin), periods=3, freq="B")
        return {t: pd.DataFrame({"Close": [m.cours.get(t, 100.0)] * 3}, index=index) for t in tickers
                if t not in m.limites}

    monkeypatch.setattr(cours, "actions_en_circulation", actions)
    monkeypatch.setattr(cours, "charger", charger)
    return m


def zip_chambre():
    tampon = io.BytesIO()
    with zipfile.ZipFile(tampon, "w") as archive:
        archive.writestr("2026FD.xml", (DONNEES / "chambre-index.xml").read_bytes())
    return tampon.getvalue()


def site_chambre():
    return Site({
        "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/2026FD.zip": Reponse(octets=zip_chambre()),
        "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20035551.pdf":
            Reponse(octets=(DONNEES / "chambre-20035551.pdf").read_bytes())})


def passer(tmp_path, suivis, vus, site_sec=None, config=CONFIG, etat=SANS_POSITION, **options):
    site_sec = site_sec if site_sec is not None else Site()
    return collecter(suivis, vus, AUJOURD_HUI, config, client(site_sec, tmp_path), etat=etat,
                     dossier_cache=tmp_path / "copie", **options)


def operation(famille, personne_id, ticker, sens="achat", montant=200000, source="13f:ACC:CUSIP", **details):
    return Operation(famille, personne_id, personne_id, ticker, sens, date(2026, 9, 30), date(2026, 10, 20),
                     montant, montant, source, details)


# --- jours à lire ---

def test_jours_a_lire():
    assert jours_a_lire(date(2026, 10, 16), date(2026, 10, 19)) == []  # lundi après vendredi
    assert jours_a_lire(date(2026, 10, 19), date(2026, 10, 21)) == [date(2026, 10, 20)]  # mercredi après lundi
    assert jours_a_lire(None, date(2026, 10, 19)) == [date(2026, 10, 16)]  # premier passage : veille ouvrée
    assert jours_a_lire(None, date(2026, 10, 21)) == [date(2026, 10, 20)]


def test_jours_a_lire_saute_les_week_ends_et_se_limite_a_sept():
    assert jours_a_lire(date(2026, 10, 15), date(2026, 10, 20)) == [date(2026, 10, 16), date(2026, 10, 19)]
    jours = jours_a_lire(date(2026, 8, 1), date(2026, 10, 21))
    assert len(jours) == 7 and jours[-1] == date(2026, 10, 20) and all(j.weekday() < 5 for j in jours)
    assert jours_a_lire(date(2026, 10, 21), date(2026, 10, 21)) == []


# --- Form 4 ---

def test_deja_vu_ignore(tmp_path, marche):
    suivis = SUIVIS_FONDS
    site = site_form4()
    achats, ventes, notes, vus = passer(tmp_path, suivis, vus_vide(), site)
    assert [(o.ticker, o.source_id) for o in achats] == [("DKS", f"form4:{ACCESSION}:1")]
    assert achats[0].details["groupe"] is False and ventes == []
    assert f"form4:{ACCESSION}" in vus["vus"] and vus["dernier_passage"] == "2026-10-20"
    # même index relu : le dépôt n'est ni téléchargé ni renvoyé une seconde fois
    site2 = site_form4()
    achats2, ventes2, _, vus2 = passer(tmp_path, suivis, {**vus, "dernier_passage": "2026-10-19"}, site2)
    assert achats2 == [] and ventes2 == []
    assert URL_DEPOT not in site2.urls and URL_INDEX_20 in site2.urls
    assert vus2["vus"] == vus["vus"]


def test_entree_vus_non_modifiee(tmp_path, marche):
    vus = vus_vide()
    passer(tmp_path, SUIVIS_FONDS, vus, site_form4())
    assert vus == vus_vide()


def test_index_form4_absent(tmp_path, marche):
    achats, ventes, notes, vus = passer(tmp_path, SUIVIS_FONDS, vus_vide(), Site())
    assert achats == [] and ventes == []
    assert any("Form 4" in n and "404" in n and "2026-10-20" in n for n in notes)
    assert vus["dernier_passage"] == "2026-10-20"  # un jour vide n'est pas une erreur


def test_form4_en_erreur_ne_marque_rien_et_garde_le_dernier_passage(tmp_path, marche):
    site = site_form4(**{URL_INDEX_20: Reponse(code=503)})
    achats, _, notes, vus = passer(tmp_path, SUIVIS_FONDS, vus_vide(), site)
    assert achats == [] and any("Form 4" in n and "503" in n for n in notes)
    assert vus == vus_vide()
    assert CONTACT not in " ".join(notes)


def test_depot_form4_en_erreur_n_est_pas_marque_vu(tmp_path, marche):
    site = site_form4(**{URL_DEPOT: Reponse(code=500)})
    achats, _, notes, vus = passer(tmp_path, SUIVIS_FONDS, vus_vide(), site)
    assert achats == [] and vus["vus"] == [] and vus["dernier_passage"] == "2026-10-19"
    assert any("500" in n for n in notes)


def test_form4_arrete_apres_des_echecs_de_suite(tmp_path, marche):
    chemins = [f"edgar/data/1/0000000001-26-00000{i}.txt" for i in range(8)]
    reponses = {URL_INDEX_20: Reponse(octets=index_form4(*chemins))}
    for c in chemins:
        reponses["https://www.sec.gov/Archives/" + c] = Reponse(code=503)
    site = Site(reponses)
    _, _, notes, _ = passer(tmp_path, SUIVIS_FONDS, vus_vide(), site)
    assert len([u for u in site.urls if u.endswith(".txt")]) == 5 and any("arrêtée" in n for n in notes)


def test_achat_de_dirigeant_sans_les_criteres_ecarte(tmp_path, marche):
    autre_code = depot_form4().replace(b"<transactionCode>P<", b"<transactionCode>A<")
    site = site_form4(**{URL_DEPOT: Reponse(octets=autre_code)})
    achats, ventes, _, vus = passer(tmp_path, SUIVIS_FONDS, vus_vide(), site)
    assert achats == [] and ventes == [] and f"form4:{ACCESSION}" in vus["vus"]  # lu, sans rien à signaler


# --- univers ---

def test_achat_hors_univers_ecarte_et_compte(tmp_path, marche):
    marche.actions["DKS"] = 1e6  # 1 million d'actions à 100 $ : 100 M$
    achats, _, notes, vus = passer(tmp_path, SUIVIS_FONDS, vus_vide(), site_form4())
    assert achats == [] and any("1 achat(s) hors univers" in n for n in notes)
    assert f"form4:{ACCESSION}" in vus["vus"]


def test_nombre_d_actions_inconnu_exclut_le_titre(tmp_path, marche):
    marche.actions["DKS"] = None
    achats, _, notes, _ = passer(tmp_path, SUIVIS_FONDS, vus_vide(), site_form4())
    assert achats == [] and any("hors univers" in n for n in notes)


def test_titre_limite_par_yahoo_relu_au_passage_suivant(tmp_path, marche):
    marche.limites.add("DKS")
    achats, _, notes, vus = passer(tmp_path, SUIVIS_FONDS, vus_vide(), site_form4())
    assert achats == [] and vus["vus"] == [] and vus["dernier_passage"] == "2026-10-19"
    assert any("Yahoo" in n and "DKS" in n for n in notes)


def test_aucun_appel_yahoo_sans_achat(tmp_path, marche):
    passer(tmp_path, SUIVIS_FONDS, vus_vide(), Site())
    assert marche.appels == []


# --- groupes de dirigeants ---

def test_groupe_avec_les_30_derniers_jours_de_vus(tmp_path, marche):
    vus = {**vus_vide(), "achats_dirigeants": [
        {"ticker": "DKS", "date": "2026-10-05", "personne_id": "dirigeant:42"},
        {"ticker": "ZZZ", "date": "2026-09-01", "personne_id": "dirigeant:43"}]}
    achats, _, _, nouveaux = passer(tmp_path, SUIVIS_FONDS, vus, site_form4())
    assert achats[0].details["groupe"] is True
    # l'achat du jour s'ajoute ; celui de plus de 30 jours est purgé
    assert {"ticker": "DKS", "date": "2026-09-22", "personne_id": "dirigeant:1772409"} in nouveaux["achats_dirigeants"]
    assert all(a["ticker"] != "ZZZ" for a in nouveaux["achats_dirigeants"])
    assert len(nouveaux["achats_dirigeants"]) == 2


def test_pas_de_groupe_avec_le_meme_dirigeant_ou_un_achat_ancien(tmp_path, marche):
    vus = {**vus_vide(), "achats_dirigeants": [
        {"ticker": "DKS", "date": "2026-10-05", "personne_id": "dirigeant:1772409"},
        {"ticker": "DKS", "date": "2026-08-01", "personne_id": "dirigeant:44"}]}
    achats, _, _, _ = passer(tmp_path, SUIVIS_FONDS, vus, site_form4())
    assert achats[0].details["groupe"] is False


# --- Chambre ---

def test_personne_non_suivie_ignoree(tmp_path, marche):
    site = site_chambre()
    suivis = suivis_de("politicien:chambre:Autre|Personne|CA01")
    achats, ventes, _, vus = passer(tmp_path, suivis, vus_vide(), session_chambre=site)
    assert achats == [] and ventes == [] and vus["vus"] == []
    assert not any(u.endswith(".pdf") for u in site.urls)


def test_personne_suivie_de_la_chambre(tmp_path, marche):
    achats, ventes, notes, vus = passer(tmp_path, suivis_de(JACKSON), vus_vide(), session_chambre=site_chambre())
    # trois achats d'au moins 15 000 $ ; les ventes ne comptent que pour une personne qui a une position
    assert [o.ticker for o in achats] == ["DDOG", "EL", "GH"] and ventes == []
    assert "chambre:20035551" in vus["vus"]
    # relu : plus rien
    site2 = site_chambre()
    achats2, _, _, _ = passer(tmp_path, suivis_de(JACKSON), {**vus, "dernier_passage": "2026-10-19"},
                              session_chambre=site2)
    assert achats2 == [] and not any(u.endswith(".pdf") for u in site2.urls)


def test_achat_d_elu_sous_le_seuil_ecarte(tmp_path, marche):
    config = {"copie": {**P, "politicien_montant_min": 50000}}
    achats, _, _, _ = passer(tmp_path, suivis_de(JACKSON), vus_vide(), config=config, session_chambre=site_chambre())
    assert [o.ticker for o in achats] == ["DDOG", "EL"]


def test_ventes_des_personnes_qui_ont_une_position(tmp_path, marche):
    etat = {"positions": [position(JACKSON, "WMT"), position("fonds:1135730", "WMT")], "historique": []}
    # Jackson n'est plus suivi mais sa position reste : seule sa vente de WMT est renvoyée
    achats, ventes, _, _ = passer(tmp_path, SUIVIS_FONDS, vus_vide(), etat=etat, session_chambre=site_chambre())
    assert achats == [] and [(v.ticker, v.sens, v.personne_id) for v in ventes] == [("WMT", "vente", JACKSON)]


def test_chambre_en_erreur_n_arrete_pas_les_autres_sources(tmp_path, marche):
    site_web = Site({"https://disclosures-clerk.house.gov/public_disc/financial-pdfs/2026FD.zip": Reponse(code=500)})
    achats, _, notes, vus = passer(tmp_path, suivis_de(JACKSON, "fonds:1135730"), vus_vide(), site_form4(),
                                   session_chambre=site_web)
    assert [o.ticker for o in achats] == ["DKS"]
    assert any(n.startswith("Chambre") and "500" in n for n in notes)
    assert "chambre:20035551" not in vus["vus"] and vus["dernier_passage"] == "2026-10-19"
    assert f"form4:{ACCESSION}" in vus["vus"]


def test_pdf_chambre_illisible_marque_vu_avec_une_note(tmp_path, marche):
    site = site_chambre()
    site.reponses["https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20035551.pdf"] = Reponse(
        octets=(DONNEES / "chambre-scan.pdf").read_bytes())
    achats, _, notes, vus = passer(tmp_path, suivis_de(JACKSON), vus_vide(), session_chambre=site)
    assert achats == [] and "chambre:20035551" in vus["vus"] and any("illisible" in n for n in notes)


def test_pdf_chambre_introuvable_sera_relu(tmp_path, marche):
    site = site_chambre()
    del site.reponses["https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20035551.pdf"]
    _, _, notes, vus = passer(tmp_path, suivis_de(JACKSON), vus_vide(), session_chambre=site)
    assert "chambre:20035551" not in vus["vus"] and any("introuvable" in n for n in notes)


# --- Sénat ---

ACCUEIL = '<html><form><input type="hidden" name="csrfmiddlewaretoken" value="jeton-test"></form></html>'


def session_senat():
    return Session(Reponse(texte=ACCUEIL), Reponse(texte="ok"),
                   Reponse(contenu=json.loads((DONNEES / "senat-recherche.json").read_text())),
                   Reponse(texte=(DONNEES / "senat-ptr.html").read_text()))


def test_senat_ignore_sans_accord(tmp_path, marche, monkeypatch):
    def interdit(*args, **kwargs):
        raise AssertionError("le module senat ne doit pas être appelé sans accord")

    for nom in ("ouvrir_session", "rechercher", "telecharger", "lire_declaration", "lire_recherche", "_page"):
        monkeypatch.setattr(senat, nom, interdit)
    for config in (CONFIG, {"copie": {**P, "senat_accepte": "true"}}, {"copie": {k: v for k, v in P.items()
                                                                              if k != "senat_accepte"}}):
        session = Session()
        achats, ventes, notes, vus = passer(tmp_path, suivis_de(DOE), {**vus_vide("2026-10-10")}, config=config,
                                            session_senat=session)
        assert achats == [] and ventes == [] and session.appels == []
        assert not any("Sénat" in n for n in notes)


def test_senat_lu_apres_l_accord(tmp_path, marche):
    etat = {"positions": [position(DOE, "AAPL")], "historique": []}
    session = session_senat()
    achats, ventes, _, vus = passer(tmp_path, suivis_de(DOE), vus_vide("2026-10-10"), config=CONFIG_SENAT, etat=etat,
                                    session_senat=session)
    assert [(o.ticker, o.personne_id) for o in achats] == [("NVDA", DOE)]
    assert [(o.ticker, o.sens) for o in ventes] == [("AAPL", "vente")]
    assert "senat:3f2b8c1e-5d4a-4e7b-9a10-1c2d3e4f5a6b" in vus["vus"]


def test_senat_papier_marque_vu(tmp_path, marche):
    session = Session(Reponse(texte=ACCUEIL), Reponse(texte="ok"),
                      Reponse(contenu=json.loads((DONNEES / "senat-recherche.json").read_text())))
    achats, _, notes, vus = passer(tmp_path, suivis_de("politicien:senat:Roe|John"), vus_vide("2026-10-10"),
                                   config=CONFIG_SENAT, session_senat=session)
    assert achats == [] and "senat:7a9d0e2f-1b3c-4d5e-8f60-718293a4b5c6" in vus["vus"]
    assert any("papier" in n for n in notes)


def test_senat_en_erreur_n_arrete_pas_les_autres_sources(tmp_path, marche):
    session = Session(Reponse(code=503))
    achats, _, notes, _ = passer(tmp_path, suivis_de(DOE, "fonds:1135730"), vus_vide(), site_form4(),
                                 config=CONFIG_SENAT, session_senat=session)
    assert [o.ticker for o in achats] == ["DKS"] and any(n.startswith("Sénat") for n in notes)


# --- 13F ---

def test_fonds_suivi_seulement(tmp_path, marche, monkeypatch):
    ops = [operation("fonds", "fonds:1135730", "MSFT", source="13f:ACC1:AAA", poids_pct=5),
           operation("fonds", "fonds:1135730", "HLT", sens="vente", source="13f:ACC1:BBB"),
           operation("fonds", PERSHING, "NVDA", source="13f:ACC2:CCC", poids_pct=6)]
    appels = []

    def telecharger(client, debut, dossier, p=None):
        appels.append(debut)
        return ops, 2

    monkeypatch.setattr(fonds, "telecharger", telecharger)
    etat = {"positions": [position("fonds:1135730", "HLT")], "historique": []}
    achats, ventes, notes, vus = passer(tmp_path, SUIVIS_FONDS, vus_vide(), etat=etat)
    assert [o.ticker for o in achats] == ["MSFT"] and [o.ticker for o in ventes] == ["HLT"]
    assert vus["vus"] == ["13f:ACC1"] and appels == [date(2026, 10, 19)]
    assert any("2 nom(s)" in n for n in notes)
    # relu : déjà vu
    achats2, ventes2, _, _ = passer(tmp_path, SUIVIS_FONDS, {**vus, "dernier_passage": "2026-10-19"}, etat=etat)
    assert achats2 == [] and ventes2 == []


def test_13f_non_lu_sans_fonds_suivi(tmp_path, marche, monkeypatch):
    monkeypatch.setattr(fonds, "telecharger", lambda *a, **k: pytest.fail("13F lu sans fonds suivi"))
    passer(tmp_path, suivis_de(JACKSON), vus_vide(), session_chambre=site_chambre())


def test_13f_en_erreur_n_arrete_pas_form4(tmp_path, marche, monkeypatch):
    def telecharger(*args, **kwargs):
        raise RuntimeError("SEC_CONTACT=" + CONTACT)

    monkeypatch.setattr(fonds, "telecharger", telecharger)
    achats, _, notes, vus = passer(tmp_path, SUIVIS_FONDS, vus_vide(), site_form4())
    assert [o.ticker for o in achats] == ["DKS"] and vus["dernier_passage"] == "2026-10-19"
    assert any(n.startswith("13F") for n in notes) and CONTACT not in " ".join(notes)


# --- tickers ---

def test_ticker_illisible_ignore_et_compte(tmp_path, marche, monkeypatch):
    ops = [operation("fonds", "fonds:1135730", "N/A", source="13f:ACC1:AAA"),
           operation("fonds", "fonds:1135730", "nyse: ibm", source="13f:ACC1:BBB")]
    monkeypatch.setattr(fonds, "telecharger", lambda *a, **k: (ops, 0))
    achats, _, notes, _ = passer(tmp_path, SUIVIS_FONDS, vus_vide())
    assert [o.ticker for o in achats] == ["IBM"]
    assert "1 opération(s) ignorée(s) : ticker illisible" in notes


# --- vus.json ---

def test_vus_borne(tmp_path, marche):
    anciens = [f"chambre:{i}" for i in range(nouveautes.VUS_MAX)]
    _, _, _, vus = passer(tmp_path, SUIVIS_FONDS, {**vus_vide(), "vus": anciens}, site_form4())
    assert len(vus["vus"]) == nouveautes.VUS_MAX
    assert vus["vus"][-1] == f"form4:{ACCESSION}" and "chambre:0" not in vus["vus"] and "chambre:1" in vus["vus"]


def test_est_doublon():
    vus = {"vus": ["chambre:123", "form4:0001-26-000001", "13f:0002-26-000002"]}
    assert est_doublon(operation("politicien", "p", "AAA", source="chambre:123:4"), vus)
    assert est_doublon(operation("dirigeant", "d", "AAA", source="form4:0001-26-000001:1"), vus)
    assert est_doublon(operation("fonds", "f", "AAA", source="13f:0002-26-000002:037833100"), vus)
    assert not est_doublon(operation("politicien", "p", "AAA", source="chambre:124:1"), vus)
    assert not est_doublon(operation("politicien", "p", "AAA", source="chambre:123:1"), {"vus": []})


def test_charger_et_enregistrer_vus(tmp_path, monkeypatch):
    monkeypatch.setattr(nouveautes, "VUS", tmp_path / "copie" / "vus.json")
    assert charger_vus() == vus_vide(None)
    vus = {"dernier_passage": "2026-10-20", "vus": ["form4:a"],
           "achats_dirigeants": [{"ticker": "DKS", "date": "2026-09-22", "personne_id": "dirigeant:1"}]}
    enregistrer_vus(vus)
    assert charger_vus() == vus
    (tmp_path / "copie" / "vus.json").write_text("pas du json")
    assert charger_vus() == vus_vide(None)
