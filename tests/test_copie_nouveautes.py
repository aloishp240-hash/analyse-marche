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
    return {"dernier_passage": dernier, "vus": {}, "achats_dirigeants": [], "indisponibles": {}}


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


def url_index(jour):
    trimestre = (jour.month - 1) // 3 + 1
    return f"https://www.sec.gov/Archives/edgar/daily-index/{jour.year}/QTR{trimestre}/form.{jour:%Y%m%d}.idx"


def indices_vides(site, *jours):
    """Index Form 4 sans aucun dépôt pour ces jours (sans cela, le faux site répond 404)."""
    for jour in jours:
        site.reponses[url_index(jour)] = Reponse(octets=index_form4())
    return site


def site_form4(**en_plus):
    reponses = {URL_INDEX_20: Reponse(octets=index_form4(CHEMIN)), URL_DEPOT: Reponse(octets=depot_form4())}
    reponses.update(en_plus)
    return Site(reponses)


class Marche:
    """Faux yfinance : nombre d'actions et dernier cours par ticker, titres limités."""

    def __init__(self):
        self.actions, self.cours, self.limites, self.appels, self.panne = {}, {}, set(), [], None

    def fournir(self, tickers, defaut):
        return {t: v for t in tickers if (v := self.actions.get(t, defaut)) is not None}


@pytest.fixture
def marche(monkeypatch):
    m = Marche()

    def actions(tickers, dossier, attendre=None, taille_lot=20, limites=None):
        m.appels.append(("actions", list(tickers)))
        if m.panne is not None:
            raise m.panne
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


@pytest.fixture(autouse=True)
def sans_13f(monkeypatch):
    """Par défaut, aucun 13F n'est lu (le faux site SEC n'en a pas) ; les tests 13F remplacent cette fonction."""
    monkeypatch.setattr(fonds, "telecharger", lambda *args, **kwargs: ([], 0))


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


def passer(tmp_path, suivis, vus, site_sec=None, config=CONFIG, etat=SANS_POSITION, jour=AUJOURD_HUI, **options):
    if site_sec is None:  # par défaut, aucun dépôt Form 4 ces jours-là
        dernier = vus["dernier_passage"]
        site_sec = indices_vides(Site(), *(jours_a_lire(date.fromisoformat(dernier), jour) if dernier else []))
    return collecter(suivis, vus, jour, config, client(site_sec, tmp_path), etat=etat,
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

def test_deja_vu_ignore(tmp_path, marche, monkeypatch):
    suivis = SUIVIS_FONDS
    site = site_form4()
    achats, ventes, notes, vus = passer(tmp_path, suivis, vus_vide(), site)
    assert [(o.ticker, o.source_id) for o in achats] == [("DKS", f"form4:{ACCESSION}:1")]
    assert achats[0].details["groupe"] is False and ventes == []
    # le jour 2026-10-20 est lu en entier : son dépôt n'a plus besoin d'être gardé dans vus
    assert vus["vus"] == {} and vus["dernier_passage"] == "2026-10-20"

    # passage bloqué (13F en échec) : le dépôt reste vu, daté du jour de l'index, et n'est pas renvoyé
    def telecharger(client, debut, dossier, p=None, signaler=None):
        signaler("1135730", "Coatue", "injoignable")
        return [], 0

    monkeypatch.setattr(fonds, "telecharger", telecharger)
    achats1, _, _, vus1 = passer(tmp_path, suivis, vus_vide(), site_form4())
    assert [o.ticker for o in achats1] == ["DKS"] and vus1["vus"] == {f"form4:{ACCESSION}": "2026-10-20"}
    site2 = site_form4()
    achats2, ventes2, _, vus2 = passer(tmp_path, suivis, vus1, site2)
    assert achats2 == [] and ventes2 == []
    assert URL_DEPOT not in site2.urls and URL_INDEX_20 in site2.urls
    assert vus2["vus"] == vus1["vus"]


def test_entree_vus_non_modifiee(tmp_path, marche):
    vus = vus_vide()
    passer(tmp_path, SUIVIS_FONDS, vus, site_form4())
    assert vus == vus_vide()


def test_index_form4_absent_un_jour_ouvre_est_un_echec(tmp_path, marche):
    achats, ventes, notes, vus = passer(tmp_path, SUIVIS_FONDS, vus_vide(), Site())
    assert achats == [] and ventes == []
    assert any("Form 4" in n and "404" in n and "2026-10-20" in n for n in notes)
    assert vus["dernier_passage"] == "2026-10-19"  # bloqué : le jour sera relu


def test_index_form4_absent_un_jour_ferie_est_un_jour_vide(tmp_path, marche):
    # lundi 12 octobre 2026 : Columbus Day, EDGAR est fermé
    _, _, notes, vus = passer(tmp_path, SUIVIS_FONDS, vus_vide("2026-10-09"), Site(), jour=date(2026, 10, 13))
    assert vus["dernier_passage"] == "2026-10-12" and not any("Form 4" in n for n in notes)


def test_depot_form4_introuvable_un_jour_ouvre_est_un_echec(tmp_path, marche):
    site = site_form4(**{URL_DEPOT: Reponse(code=404)})
    achats, _, notes, vus = passer(tmp_path, SUIVIS_FONDS, vus_vide(), site)
    assert achats == [] and vus["vus"] == {} and vus["dernier_passage"] == "2026-10-19"
    assert any("Form 4" in n and ACCESSION in n and "404" in n for n in notes)


def test_depot_form4_introuvable_un_jour_ferie_est_ignore(tmp_path, marche):
    jour = date(2026, 10, 12)
    site = Site({url_index(jour): Reponse(octets=index_form4(CHEMIN))})
    _, _, notes, vus = passer(tmp_path, SUIVIS_FONDS, vus_vide("2026-10-09"), site, jour=date(2026, 10, 13))
    assert vus["vus"] == {} and vus["dernier_passage"] == "2026-10-12"
    assert any("1 dépôt(s) du 2026-10-12 introuvable(s)" in n for n in notes)


def test_index_form4_403_un_jour_ouvre_est_un_echec_a_relire_sans_erreur(tmp_path, marche):
    # la SEC répond 403, et non 404, pour un fichier d'Archives qui n'existe pas (index pas encore publié)
    site = site_form4(**{URL_INDEX_20: Reponse(code=403)})
    achats, _, notes, vus = passer(tmp_path, SUIVIS_FONDS, vus_vide(), site)
    assert achats == []
    assert any("Form 4" in n and "2026-10-20" in n and "sera relu" in n for n in notes)
    assert not any("a répondu 403" in n for n in notes)
    assert vus["dernier_passage"] == "2026-10-19"  # bloqué : le jour sera relu


def test_index_form4_403_un_jour_ferie_est_silencieux(tmp_path, marche):
    jour = date(2026, 10, 12)  # Columbus Day
    site = Site({url_index(jour): Reponse(code=403)})
    _, _, notes, vus = passer(tmp_path, SUIVIS_FONDS, vus_vide("2026-10-09"), site, jour=date(2026, 10, 13))
    assert vus["dernier_passage"] == "2026-10-12" and not any("Form 4" in n for n in notes)


def test_depot_form4_403_est_un_depot_absent(tmp_path, marche):
    site = site_form4(**{URL_DEPOT: Reponse(code=403)})
    achats, _, notes, vus = passer(tmp_path, SUIVIS_FONDS, vus_vide(), site)
    assert achats == [] and vus["vus"] == {} and vus["dernier_passage"] == "2026-10-19"
    assert any("Form 4" in n and ACCESSION in n and "sera relu" in n for n in notes)
    assert not any("a répondu 403" in n for n in notes)


def test_index_form4_500_reste_une_erreur(tmp_path, marche):
    site = site_form4(**{URL_INDEX_20: Reponse(code=500)})
    _, _, notes, vus = passer(tmp_path, SUIVIS_FONDS, vus_vide(), site)
    assert any("Form 4" in n and "500" in n and "sera relu" not in n for n in notes)
    assert vus == vus_vide()


def test_plafond_de_sept_jours_signale_les_jours_sautes(tmp_path, marche):
    # du 6 au 20 octobre : 11 jours ouvrés, les 7 plus récents seulement sont lus
    site = indices_vides(site_form4(), *[date(2026, 10, j) for j in (12, 13, 14, 15, 16, 19)])
    _, _, notes, vus = passer(tmp_path, SUIVIS_FONDS, vus_vide("2026-10-05"), site)
    assert any("4 jour(s)" in n and "Form 4" in n for n in notes)
    assert vus["dernier_passage"] == "2026-10-20"
    _, _, notes2, _ = passer(tmp_path, SUIVIS_FONDS, vus_vide("2026-10-19"), site_form4())
    assert not any("jour(s)" in n for n in notes2)


def test_form4_en_erreur_ne_marque_rien_et_garde_le_dernier_passage(tmp_path, marche):
    site = site_form4(**{URL_INDEX_20: Reponse(code=503)})
    achats, _, notes, vus = passer(tmp_path, SUIVIS_FONDS, vus_vide(), site)
    assert achats == [] and any("Form 4" in n and "503" in n for n in notes)
    assert vus == vus_vide()
    assert CONTACT not in " ".join(notes)


def test_depot_form4_en_erreur_n_est_pas_marque_vu(tmp_path, marche):
    site = site_form4(**{URL_DEPOT: Reponse(code=500)})
    achats, _, notes, vus = passer(tmp_path, SUIVIS_FONDS, vus_vide(), site)
    assert achats == [] and vus["vus"] == {} and vus["dernier_passage"] == "2026-10-19"
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
    assert achats == [] and ventes == [] and vus["dernier_passage"] == "2026-10-20"  # lu, sans rien à signaler


# --- univers ---

def test_achat_hors_univers_ecarte_et_compte(tmp_path, marche):
    marche.actions["DKS"] = 1e6  # 1 million d'actions à 100 $ : 100 M$
    achats, _, notes, vus = passer(tmp_path, SUIVIS_FONDS, vus_vide(), site_form4())
    assert achats == [] and any("1 achat(s) hors univers" in n and "DKS" in n for n in notes)
    assert vus["dernier_passage"] == "2026-10-20"  # dépôt lu : le jour est passé


def test_nombre_d_actions_inconnu_n_est_ni_hors_univers_ni_vu(tmp_path, marche):
    # DDOG n'a pas de nombre d'actions alors que d'autres titres du même appel en ont : sa déclaration est relue
    marche.actions["DDOG"] = None
    achats, _, notes, vus = passer(tmp_path, suivis_de(JACKSON), vus_vide(), session_chambre=site_chambre())
    assert achats == [] and "chambre:20035551" not in vus["vus"]
    assert any("DDOG" in n and "sera relu" in n for n in notes) and not any("hors univers" in n for n in notes)
    assert vus["dernier_passage"] == "2026-10-19"  # bloqué : les Form 4 des jours à lire seront relus
    assert vus["indisponibles"] == {"DDOG": "2026-10-21"}  # premier jour sans donnée
    marche.actions["DDOG"] = 1e9  # la donnée revient : la déclaration est enfin traitée
    achats2, _, _, vus2 = passer(tmp_path, suivis_de(JACKSON), vus, session_chambre=site_chambre())
    assert "DDOG" in {o.ticker for o in achats2} and "chambre:20035551" in vus2["vus"]
    assert vus2["indisponibles"] == {}  # retiré de la liste dès que la donnée revient


def test_titre_sans_donnees_ecarte_apres_deux_jours(tmp_path, marche):
    marche.actions["DDOG"] = None  # la donnée ne revient pas
    vus = vus_vide()
    for jour in (date(2026, 10, 21), date(2026, 10, 22)):  # bloqué moins de 2 jours : relu
        achats, _, notes, vus = passer(tmp_path, suivis_de(JACKSON), vus, session_chambre=site_chambre(), jour=jour)
        assert achats == [] and "chambre:20035551" not in vus["vus"] and vus["dernier_passage"] == "2026-10-19"
        assert vus["indisponibles"] == {"DDOG": "2026-10-21"}  # la date de première absence ne bouge pas
    achats, _, notes, vus = passer(tmp_path, suivis_de(JACKSON), vus, session_chambre=site_chambre(),
                                   jour=date(2026, 10, 23))
    assert "DDOG" not in {o.ticker for o in achats} and achats  # les autres achats du dépôt passent
    assert "chambre:20035551" in vus["vus"] and vus["indisponibles"] == {}
    assert any("données manquantes depuis 2 jours, achat écarté (DDOG)" in n for n in notes)
    assert not any("hors univers" in n for n in notes) and not any("sera relu" in n for n in notes)


def test_titre_sans_cours_n_est_pas_marque_vu(tmp_path, marche, monkeypatch):
    charger = cours.charger

    def sans_ddog(tickers, *args, **kwargs):
        return {t: df for t, df in charger(tickers, *args, **kwargs).items() if t != "DDOG"}

    monkeypatch.setattr(cours, "charger", sans_ddog)
    achats, _, notes, vus = passer(tmp_path, suivis_de(JACKSON), vus_vide(), session_chambre=site_chambre())
    assert achats == [] and "chambre:20035551" not in vus["vus"]
    assert any("DDOG" in n and "sera relu" in n for n in notes) and not any("hors univers" in n for n in notes)


def test_aucun_cours_recu_est_une_panne_et_ne_marque_rien(tmp_path, marche, monkeypatch):
    monkeypatch.setattr(cours, "charger", lambda tickers, *args, **kwargs: {})
    achats, _, notes, vus = passer(tmp_path, SUIVIS_FONDS, vus_vide(), site_form4())
    assert achats == [] and vus["vus"] == {} and vus["dernier_passage"] == "2026-10-19"
    assert any("Yahoo" in n and "relus" in n and "DKS" in n for n in notes)
    assert vus["indisponibles"] == {}  # une panne ne lance pas le délai de 2 jours : relue tant qu'elle dure
    assert not any("hors univers" in n for n in notes)


def test_aucune_donnee_pour_tout_le_lot_est_une_panne_yahoo(tmp_path, marche):
    marche.actions["DKS"] = None  # le seul titre du lot n'a aucune donnée
    achats, _, notes, vus = passer(tmp_path, SUIVIS_FONDS, vus_vide(), site_form4())
    assert achats == [] and vus["vus"] == {} and vus["dernier_passage"] == "2026-10-19"
    assert any("Yahoo" in n and "DKS" in n for n in notes) and not any("hors univers" in n for n in notes)


def test_yahoo_qui_leve_une_erreur_est_une_panne(tmp_path, marche):
    marche.panne = RuntimeError("Too Many Requests")
    achats, _, notes, vus = passer(tmp_path, SUIVIS_FONDS, vus_vide(), site_form4())
    assert achats == [] and vus["vus"] == {} and vus["dernier_passage"] == "2026-10-19"
    assert any("Yahoo" in n and "DKS" in n for n in notes)
    marche.panne = None  # le passage suivant relit le dépôt
    achats2, _, _, vus2 = passer(tmp_path, SUIVIS_FONDS, vus, site_form4())
    assert [o.ticker for o in achats2] == ["DKS"] and vus2["dernier_passage"] == "2026-10-20"


def test_titre_limite_par_yahoo_relu_au_passage_suivant(tmp_path, marche):
    marche.limites.add("DKS")
    achats, _, notes, vus = passer(tmp_path, SUIVIS_FONDS, vus_vide(), site_form4())
    assert achats == [] and vus["vus"] == {} and vus["dernier_passage"] == "2026-10-19"
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
    assert achats == [] and ventes == [] and vus["vus"] == {}
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


URL_PDF = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20035551.pdf"


def test_pdf_chambre_introuvable_sera_relu(tmp_path, marche):
    site = site_chambre()
    del site.reponses[URL_PDF]
    _, _, notes, vus = passer(tmp_path, suivis_de(JACKSON), vus_vide(), session_chambre=site)
    assert "chambre:20035551" not in vus["vus"]
    assert any("introuvable" in n and "relu" in n and "404" in n for n in notes)
    assert vus["dernier_passage"] == "2026-10-20"  # une 404 n'est pas une erreur de la source


@pytest.mark.parametrize("code", [500, 503, 429])
def test_pdf_chambre_en_erreur_serveur_est_un_echec(tmp_path, marche, code):
    site = site_chambre()
    site.reponses[URL_PDF] = Reponse(code=code)
    _, _, notes, vus = passer(tmp_path, suivis_de(JACKSON), vus_vide(), indices_vides(site, date(2026, 10, 20)),
                              session_chambre=site)
    assert "chambre:20035551" not in vus["vus"] and vus["dernier_passage"] == "2026-10-19"
    assert any(n.startswith("Chambre") and str(code) in n and "introuvable" not in n for n in notes)


def test_achat_d_elu_signale_meme_si_le_pdf_arrive_plusieurs_matins_plus_tard(tmp_path, marche):
    """Déclaration publiée le 19, PDF absent le 20 et le 21, présent le 26 : signalée le 26."""
    site_web = site_chambre()
    del site_web.reponses[URL_PDF]
    site = indices_vides(Site(), *[date(2026, 10, j) for j in (20, 21, 22, 23)])
    vus, suivis = vus_vide("2026-10-19"), suivis_de(JACKSON)
    for jour in (date(2026, 10, 20), date(2026, 10, 21)):
        achats, _, notes, vus = passer(tmp_path, suivis, vus, site, jour=jour, session_chambre=site_web)
        assert achats == [] and "chambre:20035551" not in vus["vus"] and any("introuvable" in n for n in notes)
    site_web.reponses[URL_PDF] = site_chambre().reponses[URL_PDF]
    achats, _, _, vus = passer(tmp_path, suivis, vus, site, jour=date(2026, 10, 26), session_chambre=site_web)
    assert [o.ticker for o in achats] == ["DDOG", "EL", "GH"] and "chambre:20035551" in vus["vus"]


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
        assert [n for n in notes if "Sénat" in n] == [
            "Sénat : 1 personne(s) suivie(s) mais accord du site non accepté (senat_accepte), déclarations non lues"]
    _, _, notes, _ = passer(tmp_path, suivis_de(JACKSON), vus_vide("2026-10-10"), session_senat=Session())
    assert not any("Sénat" in n for n in notes)  # personne du Sénat suivie : pas de note


def test_senat_lu_apres_l_accord(tmp_path, marche):
    etat = {"positions": [position(DOE, "AAPL")], "historique": []}
    session = session_senat()
    achats, ventes, _, vus = passer(tmp_path, suivis_de(DOE), vus_vide("2026-10-10"), config=CONFIG_SENAT, etat=etat,
                                    session_senat=session)
    assert [(o.ticker, o.personne_id) for o in achats] == [("NVDA", DOE)]
    assert [(o.ticker, o.sens) for o in ventes] == [("AAPL", "vente")]
    assert "senat:3f2b8c1e-5d4a-4e7b-9a10-1c2d3e4f5a6b" in vus["vus"]


def _senat_avec_page(page):
    return Session(Reponse(texte=ACCUEIL), Reponse(texte="ok"),
                   Reponse(contenu=json.loads((DONNEES / "senat-recherche.json").read_text())), page)


def test_page_senat_introuvable_sera_relue(tmp_path, marche):
    _, _, notes, vus = passer(tmp_path, suivis_de(DOE), vus_vide("2026-10-10"), config=CONFIG_SENAT,
                              session_senat=_senat_avec_page(Reponse(code=404)))
    assert "senat:3f2b8c1e-5d4a-4e7b-9a10-1c2d3e4f5a6b" not in vus["vus"]
    assert any("introuvable" in n and "relue" in n for n in notes) and vus["dernier_passage"] == "2026-10-20"


@pytest.mark.parametrize("code", [429, 503])
def test_page_senat_en_erreur_est_un_echec(tmp_path, marche, code):
    _, _, notes, vus = passer(tmp_path, suivis_de(DOE), vus_vide("2026-10-10"), config=CONFIG_SENAT,
                              session_senat=_senat_avec_page(Reponse(code=code)))
    assert vus["vus"] == {} and vus["dernier_passage"] == "2026-10-10"
    assert any(n.startswith("Sénat") and str(code) in n and "introuvable" not in n for n in notes)


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

    def telecharger(client, debut, dossier, p=None, signaler=None):
        appels.append(debut)
        return ops, 2

    monkeypatch.setattr(fonds, "telecharger", telecharger)
    etat = {"positions": [position("fonds:1135730", "HLT")], "historique": []}
    achats, ventes, notes, vus = passer(tmp_path, SUIVIS_FONDS, vus_vide(), etat=etat)
    assert [o.ticker for o in achats] == ["MSFT"] and [o.ticker for o in ventes] == ["HLT"]
    assert list(vus["vus"]) == ["13f:ACC1"] and appels == [date(2026, 10, 19)]
    assert any("2 nom(s)" in n for n in notes)
    # relu : déjà vu
    achats2, ventes2, _, _ = passer(tmp_path, SUIVIS_FONDS, {**vus, "dernier_passage": "2026-10-19"}, etat=etat)
    assert achats2 == [] and ventes2 == []


def test_achat_deja_revendu_dans_le_passage_est_marque(tmp_path, marche, monkeypatch):
    # la vente d'un titre pas encore détenu n'est pas renvoyée : collecter marque elle-même l'achat
    ops = [operation("fonds", "fonds:1135730", "MSFT", source="13f:ACC1:AAA", poids_pct=5),
           operation("fonds", "fonds:1135730", "MSFT", sens="vente", source="13f:ACC2:AAA"),
           operation("fonds", "fonds:1135730", "AAPL", source="13f:ACC1:BBB", poids_pct=5),
           operation("fonds", PERSHING, "AAPL", sens="vente", source="13f:ACC3:BBB")]
    monkeypatch.setattr(fonds, "telecharger", lambda *args, **kwargs: (ops, 2))
    achats, ventes, _, _ = passer(tmp_path, SUIVIS_FONDS, vus_vide())
    assert {o.ticker: o.details.get("deja_revendu", False) for o in achats} == {"MSFT": True, "AAPL": False}
    assert ventes == []


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


def test_echec_sur_un_fonds_suivi_bloque_le_passage(tmp_path, marche, monkeypatch):
    ops = [operation("fonds", "fonds:1135730", "MSFT", source="13f:ACC1:AAA", poids_pct=5)]

    def telecharger(client, debut, dossier, p=None, signaler=None):
        signaler("1135730", "Coatue", "ACC9 : table illisible")
        signaler("1067983", "Berkshire Hathaway", "dépôts introuvables")
        return ops, 0

    monkeypatch.setattr(fonds, "telecharger", telecharger)
    achats, _, notes, vus = passer(tmp_path, SUIVIS_FONDS, vus_vide())
    assert achats == [] and vus["vus"] == {} and vus["dernier_passage"] == "2026-10-19"
    assert "13F, Coatue : ACC9 : table illisible" in notes
    assert not any("Berkshire" in n for n in notes)


def test_echec_sur_un_fonds_avec_position_bloque_aussi(tmp_path, marche, monkeypatch):
    def telecharger(client, debut, dossier, p=None, signaler=None):
        signaler("1336528", "Pershing Square", "injoignable")
        return [], 0

    monkeypatch.setattr(fonds, "telecharger", telecharger)
    etat = {"positions": [position(PERSHING, "NVDA")], "historique": []}
    _, _, notes, vus = passer(tmp_path, SUIVIS_FONDS, vus_vide(), etat=etat)
    assert "13F, Pershing Square : injoignable" in notes and vus["dernier_passage"] == "2026-10-19"


def test_echec_sur_un_fonds_non_suivi_est_sans_effet(tmp_path, marche, monkeypatch):
    ops = [operation("fonds", "fonds:1135730", "MSFT", source="13f:ACC1:AAA", poids_pct=5)]

    def telecharger(client, debut, dossier, p=None, signaler=None):
        signaler("1067983", "Berkshire Hathaway", "dépôts introuvables")
        return ops, 0

    monkeypatch.setattr(fonds, "telecharger", telecharger)
    achats, _, notes, vus = passer(tmp_path, SUIVIS_FONDS, vus_vide())
    assert [o.ticker for o in achats] == ["MSFT"] and list(vus["vus"]) == ["13f:ACC1"]
    assert vus["dernier_passage"] == "2026-10-20" and not any("Berkshire" in n for n in notes)


# --- tickers ---

def test_ticker_illisible_ignore_et_compte(tmp_path, marche, monkeypatch):
    ops = [operation("fonds", "fonds:1135730", "N/A", source="13f:ACC1:AAA"),
           operation("fonds", "fonds:1135730", "nyse: ibm", source="13f:ACC1:BBB")]
    monkeypatch.setattr(fonds, "telecharger", lambda *a, **k: (ops, 0))
    achats, _, notes, _ = passer(tmp_path, SUIVIS_FONDS, vus_vide())
    assert [o.ticker for o in achats] == ["IBM"]
    assert "1 opération(s) ignorée(s) : ticker illisible" in notes


# --- vus.json ---

def test_vus_garde_les_form4_d_une_semaine_bloquee_sans_plafond(tmp_path, marche, monkeypatch):
    """dernier_passage bloqué (13F en échec) pendant une semaine, 3 000 dépôts par jour : rien n'est oublié."""
    vus = vus_vide("2026-10-19")
    for numero, jour in enumerate(("2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23", "2026-10-26", "2026-10-27",
                                   "2026-10-28")):
        vus["vus"].update({f"form4:{numero:04d}-26-{i:06d}": jour for i in range(3000)})
    assert len(vus["vus"]) == 21000

    def telecharger(client, debut, dossier, p=None, signaler=None):
        signaler("1135730", "Coatue", "injoignable")  # le passage reste bloqué
        return [], 0

    monkeypatch.setattr(fonds, "telecharger", telecharger)
    achats, _, _, vus = passer(tmp_path, SUIVIS_FONDS, vus, site_form4())
    assert [o.ticker for o in achats] == ["DKS"]
    assert vus["dernier_passage"] == "2026-10-19" and len(vus["vus"]) == 21001
    achats, _, _, vus = passer(tmp_path, SUIVIS_FONDS, vus, site_form4())
    assert achats == [] and len(vus["vus"]) == 21001  # signalé une seule fois, rien d'oublié


def test_purge_des_vus_par_age(tmp_path, marche):
    vus = {**vus_vide("2026-10-19"), "vus": {
        "form4:vieux": "2026-10-19", "form4:egal": "2026-10-20", "form4:ancien-dernier": "2026-10-18",
        "form4:a-lire": "2026-10-21", "chambre:ancien": "2026-09-05", "chambre:limite": "2026-09-06",
        "13f:recent": "2026-10-01"}}
    _, _, _, nouveaux = passer(tmp_path, SUIVIS_FONDS, vus, site_form4())
    # le passage lit le 20 : plus aucun Form 4 daté du 20 ou avant n'est relu
    assert set(nouveaux["vus"]) == {"form4:a-lire", "chambre:limite", "13f:recent"}
    assert nouveaux["dernier_passage"] == "2026-10-20"


def test_vus_ancien_format_liste_daté_d_aujourd_hui(tmp_path, marche):
    vus = {"dernier_passage": "2026-10-19", "vus": ["chambre:1", "form4:x"], "achats_dirigeants": []}
    _, _, _, nouveaux = passer(tmp_path, SUIVIS_FONDS, vus, site_form4())
    assert nouveaux["vus"]["chambre:1"] == "2026-10-21" and nouveaux["vus"]["form4:x"] == "2026-10-21"
    assert vus["vus"] == ["chambre:1", "form4:x"]  # l'entrée n'est pas modifiée


def test_est_doublon():
    vus = {"vus": {"chambre:123": "2026-10-01", "form4:0001-26-000001": "2026-10-20", "13f:0002-26-000002": "2026-10-02"}}
    assert est_doublon(operation("politicien", "p", "AAA", source="chambre:123:4"), vus)
    assert est_doublon(operation("dirigeant", "d", "AAA", source="form4:0001-26-000001:1"), vus)
    assert est_doublon(operation("fonds", "f", "AAA", source="13f:0002-26-000002:037833100"), vus)
    assert not est_doublon(operation("politicien", "p", "AAA", source="chambre:124:1"), vus)
    assert not est_doublon(operation("politicien", "p", "AAA", source="chambre:123:1"), {"vus": {}})
    assert est_doublon(operation("politicien", "p", "AAA", source="chambre:123:4"), {"vus": ["chambre:123"]})


def test_charger_et_enregistrer_vus(tmp_path, monkeypatch):
    monkeypatch.setattr(nouveautes, "VUS", tmp_path / "copie" / "vus.json")
    assert charger_vus() == vus_vide(None)
    vus = {"dernier_passage": "2026-10-20", "vus": {"form4:a": "2026-10-20"},
           "achats_dirigeants": [{"ticker": "DKS", "date": "2026-09-22", "personne_id": "dirigeant:1"}],
           "indisponibles": {"DDOG": "2026-10-20"}}
    enregistrer_vus(vus)
    assert charger_vus() == vus
    assert [p.name for p in (tmp_path / "copie").iterdir()] == ["vus.json"]  # pas de fichier temporaire oublié


def test_vus_json_illisible_leve_une_erreur(tmp_path, monkeypatch):
    monkeypatch.setattr(nouveautes, "VUS", tmp_path / "vus.json")
    for contenu in ("pas du json", "[1, 2]"):
        (tmp_path / "vus.json").write_text(contenu)
        with pytest.raises(nouveautes.ErreurVus):
            charger_vus()
    assert (tmp_path / "vus.json").read_text() == "[1, 2]"  # rien n'est écrasé


def test_vus_json_de_mauvaise_forme_leve_une_erreur(tmp_path, monkeypatch):
    monkeypatch.setattr(nouveautes, "VUS", tmp_path / "vus.json")
    formes = ['{"vus": 5}', '{"vus": null}', '{"vus": [[1], 2]}', '{"achats_dirigeants": 3}',
              '{"achats_dirigeants": [1]}', '{"achats_dirigeants": [["a"]]}', '{"achats_dirigeants": null}',
              '{"dernier_passage": 5}', '{"dernier_passage": "demain"}', '{"vus": {"a": {"b": 1}}}',
              '{"indisponibles": [1]}', '{"indisponibles": {"DDOG": 3}}', '{"indisponibles": {"DDOG": "hier"}}']
    for contenu in formes:
        (tmp_path / "vus.json").write_text(contenu)
        with pytest.raises(nouveautes.ErreurVus):
            charger_vus()
        assert (tmp_path / "vus.json").read_text() == contenu  # rien n'est écrasé


def test_vus_json_ancien_format_et_dates_vides_restent_lisibles(tmp_path, monkeypatch):
    monkeypatch.setattr(nouveautes, "VUS", tmp_path / "vus.json")
    (tmp_path / "vus.json").write_text('{"dernier_passage": null, "vus": ["chambre:1"], "achats_dirigeants": []}')
    vus = charger_vus()
    assert list(vus["vus"]) == ["chambre:1"] and vus["dernier_passage"] is None
    (tmp_path / "vus.json").write_text('{}')
    assert charger_vus() == vus_vide(None)  # ancien fichier sans « indisponibles »


def test_enregistrer_vus_est_atomique(tmp_path, monkeypatch):
    monkeypatch.setattr(nouveautes, "VUS", tmp_path / "vus.json")
    enregistrer_vus({"dernier_passage": "2026-10-19", "vus": {}, "achats_dirigeants": []})

    def plante(source, destination):
        raise OSError("disque plein")

    monkeypatch.setattr(nouveautes.os, "replace", plante)
    with pytest.raises(OSError):
        enregistrer_vus({"dernier_passage": "2026-10-20", "vus": {}, "achats_dirigeants": []})
    assert charger_vus()["dernier_passage"] == "2026-10-19"  # l'ancien fichier est intact


# --- budget de temps de la lecture des Form 4 ---

class Horloge:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class SiteLent(Site):
    """Chaque dépôt Form 4 téléchargé coûte `cout` secondes à l'horloge."""

    def __init__(self, horloge, reponses, cout=400):
        super().__init__(reponses)
        self.horloge, self.cout = horloge, cout

    def request(self, methode, url, **options):
        if url.endswith(".txt"):
            self.horloge.t += self.cout
        return super().request(methode, url, **options)


def accession(n):
    return f"0001772409-26-{n:06d}"


def chemin_de(n):
    return f"edgar/data/1772409/{accession(n)}.txt"


def site_lent(horloge, depots_par_jour, cout=400):
    """depots_par_jour : {jour: [numéros de dépôt]} ; les dépôts valent tous le même achat de DKS."""
    reponses = {}
    for jour, numeros in depots_par_jour.items():
        reponses[url_index(jour)] = Reponse(octets=index_form4(*[chemin_de(n) for n in numeros]))
        for n in numeros:
            reponses["https://www.sec.gov/Archives/" + chemin_de(n)] = Reponse(octets=depot_form4())
    return SiteLent(horloge, reponses, cout)


def test_budget_depasse_au_milieu_d_un_jour_garde_les_depots_lus(tmp_path, marche):
    horloge = Horloge()
    site = site_lent(horloge, {date(2026, 10, 20): [1, 2, 3, 4]})
    achats, _, notes, vus = passer(tmp_path, SUIVIS_FONDS, vus_vide(), site, horloge=horloge)
    assert len([u for u in site.urls if u.endswith(".txt")]) == 3  # le 4e n'est pas téléchargé
    assert sorted(o.source_id.split(":")[1] for o in achats) == [accession(1), accession(2), accession(3)]
    assert vus["vus"] == {f"form4:{accession(n)}": "2026-10-20" for n in (1, 2, 3)}
    assert vus["dernier_passage"] == "2026-10-19"  # le 20 n'est pas lu en entier
    assert any("900" in n and "1 jour(s) restent à lire" in n for n in notes)


def test_passage_suivant_reprend_sans_doublon(tmp_path, marche):
    horloge = Horloge()
    jour = date(2026, 10, 20)
    achats1, _, _, vus1 = passer(tmp_path, SUIVIS_FONDS, vus_vide(), site_lent(horloge, {jour: [1, 2, 3, 4]}),
                                 horloge=horloge)
    assert len(achats1) == 3
    horloge2 = Horloge()
    site2 = site_lent(horloge2, {jour: [1, 2, 3, 4]})
    achats2, _, notes2, vus2 = passer(tmp_path, SUIVIS_FONDS, vus1, site2, horloge=horloge2)
    assert [o.source_id.split(":")[1] for o in achats2] == [accession(4)]
    assert [u for u in site2.urls if u.endswith(".txt")] == ["https://www.sec.gov/Archives/" + chemin_de(4)]
    assert vus2["dernier_passage"] == "2026-10-20" and not any("restent à lire" in n for n in notes2)


def test_budget_depasse_garde_les_jours_lus_en_entier(tmp_path, marche):
    horloge = Horloge()
    site = site_lent(horloge, {date(2026, 10, 19): [1], date(2026, 10, 20): [2, 3, 4, 5]})
    achats, _, notes, vus = passer(tmp_path, SUIVIS_FONDS, vus_vide("2026-10-16"), site, horloge=horloge)
    assert vus["dernier_passage"] == "2026-10-19"
    assert vus["vus"] == {f"form4:{accession(n)}": "2026-10-20" for n in (2, 3)}  # le 19 est lu et purgé
    assert any("1 jour(s) restent à lire" in n for n in notes)


def test_budget_depasse_avant_un_jour_laisse_les_jours_restants(tmp_path, marche):
    horloge = Horloge()
    site = site_lent(horloge, {date(2026, 10, 19): [1, 2, 3], date(2026, 10, 20): [4]})
    achats, _, notes, vus = passer(tmp_path, SUIVIS_FONDS, vus_vide("2026-10-16"), site, horloge=horloge)
    assert vus["dernier_passage"] == "2026-10-19"
    assert [u for u in site.urls if u.endswith(".txt")][-1].endswith(chemin_de(3))
    assert any("1 jour(s) restent à lire" in n for n in notes)


def test_sans_depassement_aucune_note_de_budget(tmp_path, marche):
    horloge = Horloge()
    site = site_lent(horloge, {date(2026, 10, 20): [1, 2]}, cout=10)
    _, _, notes, vus = passer(tmp_path, SUIVIS_FONDS, vus_vide(), site, horloge=horloge)
    assert vus["dernier_passage"] == "2026-10-20" and not any("restent à lire" in n for n in notes)
