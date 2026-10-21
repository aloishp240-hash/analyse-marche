import json
from datetime import date
from pathlib import Path

import pytest
import requests

from inities.fonds import (FONDS, ErreurFonds, charger_correspondance, lire_table, nouvelles_positions, normaliser,
                           telecharger)
from inities.sec import ClientSec
from tests.faux_http import Reponse

DONNEES = Path(__file__).parent / "donnees" / "copie"
P = {"fonds_poids_min_pct": 3}
MICROSOFT, HILTON, ALPHABET_C = "594918104", "43300A203", "02079K107"

ENTETE = '<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">'


def _avant():
    return lire_table((DONNEES / "13f-avant.xml").read_text(encoding="utf-8"))


def _apres():
    return lire_table((DONNEES / "13f-apres.xml").read_text(encoding="utf-8"))


def _correspondance():
    return charger_correspondance(json.loads((DONNEES / "company_tickers.json").read_text(encoding="utf-8")))


def _nouvelles(avant, apres, correspondance=None):
    return nouvelles_positions(avant, apres, "1336528", "0001172661-26-002336", date(2026, 5, 15), date(2026, 3, 31),
                               correspondance if correspondance is not None else _correspondance(), P)


def _ligne(cusip, nom, valeur, actions=100, extra=""):
    return (f"<infoTable><nameOfIssuer>{nom}</nameOfIssuer><titleOfClass>COM</titleOfClass><cusip>{cusip}</cusip>"
            f"<value>{valeur}</value><shrsOrPrnAmt><sshPrnamt>{actions}</sshPrnamt><sshPrnamtType>SH</sshPrnamtType>"
            f"</shrsOrPrnAmt>{extra}</infoTable>")


def _table(*lignes):
    return ENTETE + "".join(lignes) + "</informationTable>"


# --- lecture de la table ---

def test_lire_table_reelle():
    apres = _apres()
    assert len(apres) == 11
    assert apres[MICROSOFT] == {"nom": "MICROSOFT CORP", "valeur": 2092970053.0, "actions": 5654078.0}


def test_lignes_du_meme_cusip_additionnees():
    table = lire_table(_table(_ligne("AAA111111", "ALPHA INC", 1000, 10), _ligne("AAA111111", "ALPHA INC", 500, 5)))
    assert table == {"AAA111111": {"nom": "ALPHA INC", "valeur": 1500.0, "actions": 15.0}}


def test_options_exclues():
    puts = "<putCall>Put</putCall>"
    table = lire_table(_table(_ligne("AAA111111", "ALPHA INC", 1000), _ligne("AAA111111", "ALPHA INC", 9000, extra=puts),
                              _ligne("BBB222222", "BETA INC", 4000, extra="<putCall>Call</putCall>")))
    assert table == {"AAA111111": {"nom": "ALPHA INC", "valeur": 1000.0, "actions": 100.0}}


def test_ligne_mal_formee_ignoree():
    table = lire_table(_table(_ligne("AAA111111", "ALPHA INC", 1000), _ligne("../etc/x", "PIEGE", 5000),
                              _ligne("CCC333333", "GAMMA", "beaucoup"), _ligne("DDD444444", "DELTA", -5)))
    assert list(table) == ["AAA111111"]


def test_table_sans_espace_de_noms_et_avec_prefixe():
    brut = _table(_ligne("AAA111111", "ALPHA INC", 1000)).replace(' xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable"', "")
    assert list(lire_table(brut)) == ["AAA111111"]
    prefixe = ('<ns1:informationTable xmlns:ns1="http://www.sec.gov/edgar/document/thirteenf/informationtable">'
               "<ns1:infoTable><ns1:nameOfIssuer>ALPHA</ns1:nameOfIssuer><ns1:cusip>AAA111111</ns1:cusip>"
               "<ns1:value>10</ns1:value><ns1:shrsOrPrnAmt><ns1:sshPrnamt>3</ns1:sshPrnamt></ns1:shrsOrPrnAmt>"
               "</ns1:infoTable></ns1:informationTable>")
    assert lire_table(prefixe)["AAA111111"]["actions"] == 3.0


@pytest.mark.parametrize("brut", ["pas du xml", "", "<autre/>", ENTETE + "</informationTable>",
                                  _table("<infoTable><cusip>AAA111111</cusip></infoTable>")])
def test_table_illisible_ou_vide_refusee(brut):
    with pytest.raises(ErreurFonds):
        lire_table(brut)


def test_entites_et_dtd_non_resolues(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("CONTENU-SECRET")
    piege = (f'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file://{secret}">]>'
             + _table(_ligne("AAA111111", "&e;", 1000)))
    table = lire_table(piege)
    assert "CONTENU-SECRET" not in json.dumps(table)


# --- nouvelles positions ---

def test_nouvelles_positions_reelles():
    ops, sans_ticker = _nouvelles(_avant(), _apres())
    assert sans_ticker == 0
    assert [(o.ticker, o.sens) for o in ops] == [("MSFT", "achat"), ("HLT", "vente")]
    achat, vente = ops
    assert achat.famille == "fonds" and achat.personne_id == "fonds:1336528" and achat.personne == FONDS["1336528"][0]
    assert achat.source_id == f"13f:0001172661-26-002336:{MICROSOFT}"
    assert achat.date_operation == date(2026, 3, 31) and achat.date_publication == date(2026, 5, 15)
    assert achat.montant_min == achat.montant_max == 2092970053.0
    assert achat.details["poids_pct"] == pytest.approx(15.26, abs=0.01)
    assert achat.details["cusip"] == MICROSOFT and achat.details["nom"] == "MICROSOFT CORP"
    assert vente.source_id == f"13f:0001172661-26-002336:{HILTON}" and vente.montant_min == 869983734.0
    assert vente.date_operation == date(2026, 3, 31)


def test_petite_position_ignoree():
    avant, apres = _avant(), _apres()
    total = sum(ligne["valeur"] for ligne in apres.values())
    apres[MICROSOFT] = {"nom": "MICROSOFT CORP", "valeur": total * 0.02 / 0.98, "actions": 10}  # 2 % du portefeuille
    ops, sans_ticker = _nouvelles(avant, apres)
    assert [(o.ticker, o.sens) for o in ops] == [("HLT", "vente")] and sans_ticker == 0


def test_position_juste_au_seuil_reprise():
    apres = {"AAA111111": {"nom": "MICROSOFT CORP", "valeur": 3.0, "actions": 1},
             "BBB222222": {"nom": "AUTRE", "valeur": 97.0, "actions": 1}}
    ops, _ = _nouvelles({"BBB222222": apres["BBB222222"]}, apres)
    assert [o.ticker for o in ops] == ["MSFT"]


def test_position_disparue_vente():
    avant = _avant()
    assert HILTON in avant and HILTON not in _apres()
    ops, _ = _nouvelles(avant, _apres())
    ventes = [o for o in ops if o.sens == "vente"]
    assert [(o.ticker, o.details["cusip"]) for o in ventes] == [("HLT", HILTON)]


def test_position_conservee_ni_achat_ni_vente():
    ops, _ = _nouvelles(_apres(), _apres())
    assert ops == []


def test_nom_ambigu_ignore():
    avant, apres = _avant(), _apres()
    del avant[ALPHABET_C]
    apres[ALPHABET_C] = {"nom": "ALPHABET INC", "valeur": 1.0e10, "actions": 1}  # GOOGL et GOOG : deux tickers
    ops, sans_ticker = _nouvelles(avant, apres)
    assert not any(o.details["cusip"] == ALPHABET_C for o in ops)
    assert sans_ticker == 1


def test_nom_inconnu_et_vente_sans_ticker_comptes():
    avant = {"AAA111111": {"nom": "SOCIETE INCONNUE", "valeur": 5.0, "actions": 1}}
    apres = {"BBB222222": {"nom": "AUTRE INCONNUE", "valeur": 5.0, "actions": 1}}
    assert _nouvelles(avant, apres) == ([], 2)


def test_petite_position_sans_ticker_non_comptee():
    apres = {"AAA111111": {"nom": "INCONNUE", "valeur": 1.0, "actions": 1},
             "BBB222222": {"nom": "MICROSOFT CORP", "valeur": 99.0, "actions": 1}}
    assert _nouvelles({"BBB222222": apres["BBB222222"]}, apres) == ([], 0)


# --- noms et correspondance ---

@pytest.mark.parametrize("nom, attendu", [
    ("HERTZ GLOBAL HLDGS INC", "HERTZ GLOBAL"), ("HERTZ GLOBAL HOLDINGS, INC", "HERTZ GLOBAL"),
    ("Hilton Worldwide Holdings Inc.", "HILTON WORLDWIDE"), ("AMAZON COM INC", "AMAZON"),
    ("Apple Inc. CL A", "APPLE"), ("ACME NEW CORP LTD", "ACME"), ("Smith & Wesson Co", "SMITH WESSON"),
    ("BROOKFIELD Corp /ON/", "BROOKFIELD CORP ON"), ("NEW", ""), ("  uber   technologies, inc ", "UBER TECHNOLOGIES")])
def test_normaliser(nom, attendu):
    assert normaliser(nom) == attendu


def test_correspondance_retire_les_noms_ambigus():
    corr = _correspondance()
    assert corr["MICROSOFT"] == "MSFT" and corr["HILTON WORLDWIDE"] == "HLT" and corr["HILTON GRAND VACATIONS"] == "HGV"
    assert corr["BROOKFIELD BUSINESS"] == "BBUC"
    assert "ALPHABET" not in corr and "HERTZ GLOBAL" not in corr   # GOOGL/GOOG, HTZ/HTZWW
    assert normaliser("BROOKFIELD CORP") not in corr   # « /ON/ » reste dans le nom EDGAR : pas de correspondance sûre


def test_correspondance_ignore_les_entrees_illisibles():
    donnees = {"0": {"ticker": "AAA", "title": "Alpha Inc"}, "1": "pas un dict", "2": {"ticker": "", "title": "Vide"},
               "3": {"ticker": "BBB"}, "4": {"ticker": "CCC", "title": 12}, "5": {"ticker": "AAA", "title": "ALPHA INC."}}
    assert charger_correspondance(donnees) == {"ALPHA": "AAA"}
    assert charger_correspondance([1, 2]) == {}


def test_liste_des_fonds():
    assert len(FONDS) == 13
    assert all(cik.isdigit() and not cik.startswith("0") and len(mots) == 2 for cik, mots in FONDS.items())
    assert FONDS["1067983"][0] == "Berkshire Hathaway" and FONDS["1167483"][0] == "Tiger Global"


# --- téléchargement ---

ACC_AVANT, ACC_APRES = "0001172661-25-005039", "0001172661-26-002336"
ACC_ANCIEN = "0001172661-25-003509"
CONTACT = "moi@exemple.org"


def _submissions(nom="Pershing Square Capital Management, L.P.", depots=None):
    depots = depots if depots is not None else [
        ("13F-HR", "2026-05-15", "2026-03-31", ACC_APRES), ("SC 13D", "2026-04-01", "", "0000000000-26-000001"),
        ("13F-HR/A", "2026-05-20", "2026-03-31", "0001172661-26-009999"),
        ("13F-HR", "2025-11-14", "2025-09-30", ACC_AVANT), ("13F-HR", "2025-08-14", "2025-06-30", ACC_ANCIEN)]
    colonnes = list(zip(*depots))
    return {"name": nom, "filings": {"recent": {
        "form": list(colonnes[0]), "filingDate": list(colonnes[1]), "reportDate": list(colonnes[2]),
        "accessionNumber": list(colonnes[3])}, "files": []}}


def _index(*noms):
    return {"directory": {"item": [{"name": n} for n in noms]}}


def _dossier(cik, accession):
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/"


class SiteSec:
    """Remplace requests.Session : répond selon l'URL, note les URL demandées."""

    def __init__(self, reponses):
        self.reponses = reponses
        self.urls = []

    def request(self, methode, url, **options):
        assert methode == "GET" and options["headers"]["User-Agent"] == f"analyse-marche {CONTACT}"
        self.urls.append(url)
        reponse = self.reponses.get(url)
        return Reponse(code=404) if reponse is None else reponse


def _site_pershing(**en_plus):
    avant = (DONNEES / "13f-avant.xml").read_bytes()
    apres = (DONNEES / "13f-apres.xml").read_bytes()
    corr = (DONNEES / "company_tickers.json").read_bytes()
    reponses = {
        "https://www.sec.gov/files/company_tickers.json": Reponse(octets=corr),
        "https://data.sec.gov/submissions/CIK0001336528.json": Reponse(octets=json.dumps(_submissions()).encode()),
        _dossier("1336528", ACC_APRES) + "index.json": Reponse(octets=json.dumps(_index("primary_doc.xml", "infotable.xml", "x-index.html")).encode()),
        _dossier("1336528", ACC_APRES) + "infotable.xml": Reponse(octets=apres),
        _dossier("1336528", ACC_AVANT) + "index.json": Reponse(octets=json.dumps(_index("primary_doc.xml", "infotable.xml")).encode()),
        _dossier("1336528", ACC_AVANT) + "infotable.xml": Reponse(octets=avant),
        _dossier("1336528", ACC_ANCIEN) + "index.json": Reponse(octets=json.dumps(_index("primary_doc.xml", "infotable.xml")).encode()),
        _dossier("1336528", ACC_ANCIEN) + "infotable.xml": Reponse(octets=avant),
    }
    reponses.update(en_plus)
    return SiteSec(reponses)


def _client(site, tmp_path):
    return ClientSec(contact=CONTACT, session=site, dossier_cache=tmp_path / "cache", pause=0)


def test_telecharger_lit_les_13f_de_chaque_fonds(tmp_path, capsys):
    site = _site_pershing()
    ops, sans_ticker = telecharger(_client(site, tmp_path), date(2026, 1, 1), tmp_path / "13f")
    assert [(o.ticker, o.sens, o.date_publication) for o in ops] == [
        ("MSFT", "achat", date(2026, 5, 15)), ("HLT", "vente", date(2026, 5, 15))]
    assert sans_ticker == 0
    assert ops[0].personne == "Pershing Square" and ops[0].source_id.startswith(f"13f:{ACC_APRES}:")
    # le 13F-HR/A, le formulaire SC 13D et le trimestre hors fenêtre ne sont pas lus
    assert not any("0001172661-26-009999".replace("-", "") in u for u in site.urls)
    assert _dossier("1336528", ACC_ANCIEN) + "infotable.xml" not in site.urls
    # les 12 autres fonds n'existent pas dans ce faux site : signalés, sans arrêter la lecture
    sortie = capsys.readouterr().out
    assert sortie.count("13F ignoré") == 12 and CONTACT not in sortie


def test_telecharger_met_les_tables_en_cache(tmp_path):
    ops, _ = telecharger(_client(_site_pershing(), tmp_path), date(2026, 1, 1), tmp_path / "13f")
    assert sorted(f.name for f in (tmp_path / "13f").glob("*.xml")) == sorted([
        f"1336528-{ACC_APRES}.xml", f"1336528-{ACC_AVANT}.xml"])
    ensuite = _site_pershing()
    ops2, _ = telecharger(_client(ensuite, tmp_path / "autre"), date(2026, 1, 1), tmp_path / "13f")
    assert ops2 == ops
    assert not any(u.endswith("infotable.xml") or u.endswith("index.json") for u in ensuite.urls)


def test_telecharger_ignore_un_fonds_au_nom_inattendu(tmp_path, capsys):
    site = _site_pershing(**{"https://data.sec.gov/submissions/CIK0001336528.json": Reponse(
        octets=json.dumps(_submissions(nom="AUTRE SOCIETE LLC")).encode())})
    assert telecharger(_client(site, tmp_path), date(2026, 1, 1), tmp_path / "13f") == ([], 0)
    sortie = capsys.readouterr().out
    assert "Pershing Square" in sortie and "nom EDGAR inattendu" in sortie
    assert not any("infotable" in u for u in site.urls)


def test_telecharger_table_illisible_ignoree_sans_arreter_les_autres(tmp_path, capsys):
    # Pershing : table du dernier trimestre illisible ; Akre : lisible
    site = _site_pershing(**{_dossier("1336528", ACC_APRES) + "infotable.xml": Reponse(octets=b"<html>pas du xml")})
    akre = _submissions(nom="AKRE CAPITAL MANAGEMENT LLC", depots=[
        ("13F-HR", "2026-05-14", "2026-03-31", "0001112520-26-000009"),
        ("13F-HR", "2026-02-14", "2025-12-31", "0001112520-26-000002")])
    site.reponses["https://data.sec.gov/submissions/CIK0001112520.json"] = Reponse(octets=json.dumps(akre).encode())
    for accession, fichier in (("0001112520-26-000009", "13f-apres.xml"), ("0001112520-26-000002", "13f-avant.xml")):
        site.reponses[_dossier("1112520", accession) + "index.json"] = Reponse(octets=json.dumps(_index("t.xml")).encode())
        site.reponses[_dossier("1112520", accession) + "t.xml"] = Reponse(octets=(DONNEES / fichier).read_bytes())
    ops, _ = telecharger(_client(site, tmp_path), date(2026, 1, 1), tmp_path / "13f")
    assert {o.personne_id for o in ops} == {"fonds:1112520"} and len(ops) == 2
    sortie = capsys.readouterr().out
    assert "Pershing Square" in sortie and ACC_APRES in sortie and "illisible" in sortie


def test_telecharger_erreur_reseau_ignore_le_fonds(tmp_path, capsys):
    class SiteCapricieux(SiteSec):
        def request(self, methode, url, **options):
            if "CIK0001336528" in url:
                raise requests.ConnectionError(f"coupure {CONTACT}")
            return super().request(methode, url, **options)

    site = SiteCapricieux(_site_pershing().reponses)
    assert telecharger(_client(site, tmp_path), date(2026, 1, 1), tmp_path / "13f") == ([], 0)
    sortie = capsys.readouterr().out
    assert "Pershing Square" in sortie and CONTACT not in sortie


def test_telecharger_refuse_un_nom_de_fichier_ou_un_numero_piegeux(tmp_path, capsys):
    submissions = _submissions(depots=[
        ("13F-HR", "2026-05-15", "2026-03-31", "../../evil"), ("13F-HR", "2025-11-14", "2025-09-30", ACC_AVANT)])
    site = _site_pershing(**{"https://data.sec.gov/submissions/CIK0001336528.json": Reponse(
        octets=json.dumps(submissions).encode())})
    assert telecharger(_client(site, tmp_path), date(2026, 1, 1), tmp_path / "13f") == ([], 0)
    assert not any("evil" in u for u in site.urls)

    site = _site_pershing(**{_dossier("1336528", ACC_APRES) + "index.json": Reponse(
        octets=json.dumps(_index("../../infotable.xml", "primary_doc.xml")).encode())})
    assert telecharger(_client(site, tmp_path / "bis"), date(2026, 1, 1), tmp_path / "13f-bis") == ([], 0)
    assert "illisible" in capsys.readouterr().out


def test_telecharger_compte_les_noms_sans_ticker(tmp_path):
    corr = json.loads((DONNEES / "company_tickers.json").read_text())
    corr = {k: v for k, v in corr.items() if v["ticker"] != "MSFT"}
    site = _site_pershing(**{"https://www.sec.gov/files/company_tickers.json": Reponse(octets=json.dumps(corr).encode())})
    ops, sans_ticker = telecharger(_client(site, tmp_path), date(2026, 1, 1), tmp_path / "13f")
    assert [o.ticker for o in ops] == ["HLT"] and sans_ticker == 1


def test_telecharger_lit_une_table_dont_l_extension_est_en_majuscules(tmp_path):
    # Viking (2026T1) : la table s'appelle « MSFS13F033126.XML » dans le dossier du dépôt
    apres = (DONNEES / "13f-apres.xml").read_bytes()
    site = _site_pershing(**{
        _dossier("1336528", ACC_APRES) + "index.json": Reponse(
            octets=json.dumps(_index("PRIMARY_DOC.XML", "INFOTABLE.XML")).encode()),
        _dossier("1336528", ACC_APRES) + "INFOTABLE.XML": Reponse(octets=apres)})
    ops, _ = telecharger(_client(site, tmp_path), date(2026, 1, 1), tmp_path / "13f")
    assert [(o.ticker, o.sens) for o in ops] == [("MSFT", "achat"), ("HLT", "vente")]
    assert not any(u.lower().endswith("primary_doc.xml") for u in site.urls)  # la page d'en-tête n'est pas la table


def test_telecharger_signaler_recoit_cik_nom_et_raison(tmp_path, capsys):
    appels = []
    telecharger(_client(_site_pershing(), tmp_path), date(2026, 1, 1), tmp_path / "13f",
                signaler=lambda cik, nom, raison: appels.append((cik, nom, raison)))
    assert len(appels) == 12 and ("1067983", "Berkshire Hathaway", "dépôts introuvables") in appels
    assert all(cik != "1336528" for cik, _, _ in appels)
    assert "13F ignoré" not in capsys.readouterr().out  # le signalement par défaut n'est plus utilisé
