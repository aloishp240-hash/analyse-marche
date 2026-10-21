import io
import json
import zipfile
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

import backtest_copie
from commun import charger_config
from inities import chambre, cours as module_cours, dirigeants, fonds, senat
from inities.mesure import bilan
from inities.modele import Operation
from inities.rapport import AVERTISSEMENT, ecrire
from inities.sec import ErreurSec

DATE_CHOIX = date(2026, 10, 21)
P = charger_config()["copie"]
JOURS = pd.bdate_range("2023-10-02", DATE_CHOIX)
CONTACT = "moi@exemple.org"


# --- rapport ---

def _bilan_vide():
    return bilan([], {}, None, DATE_CHOIX)


def test_rapport_sections():
    texte = ecrire([], _bilan_vide(), {}, {}, DATE_CHOIX)
    for titre in ("## Liste proposée", "## Classement complet", "## Dirigeants (filtre par achat)",
                  "## Portefeuille au fil de l'eau (oct. 2025 → oct. 2026)", "## Données"):
        assert titre in texte
    assert "Résultats passés, pas une garantie ; pas un conseil financier." in texte
    assert AVERTISSEMENT in texte


def _ligne(nom, retenu, motif="retenu", famille="politicien", achats=12, part=0.7):
    b = {"achats": achats, "delai_moyen": 12.4, "dernier_achat": date(2026, 5, 1), "r3": 5.0, "q3": 2.0, "r6": 9.0,
         "q6": 4.0, "mesures": [(9.0, 4.0)], "r": 9.0, "q": 4.0, "part_battu": part}
    return {"personne_id": f"politicien:chambre:{nom}|X|CA-01", "personne": nom, "famille": famille, "bilan": b,
            "retenu": retenu, "motif": motif}


def _simulation(rendement, final, qqq=10.0):
    trades = [{"raison": "stop"}, {"raison": "stop"}, {"raison": "6 mois"}, {"raison": "ouverte"}]
    return {"trades": trades, "final": final, "rendement_pct": rendement, "qqq_pct": qqq,
            "ignores": [{"motif": "titre déjà détenu"}, {"motif": "titre déjà détenu"}, {"motif": "liquidités insuffisantes"}],
            "valeur": pd.Series(dtype=float), "suivis": {}}


def _lecture(**ajouts):
    lecture = {
        "sources": [
            {"nom": "Chambre", "etat": "lue", "lues": 90, "illisibles": 10, "operations": 400},
            {"nom": "Sénat", "etat": "non lue", "detail": "accord non donné"},
            {"nom": "Form 4 (jeux trimestriels)", "etat": "echec", "detail": "SecInjoignable : coupure"},
            {"nom": "13F", "etat": "lue", "operations": 30},
        ],
        "sans_ticker": 7, "personnes_sans_ligne": 41, "fonds_sans_13f": ["Greenlight", "Scion"],
    }
    lecture.update(ajouts)
    return lecture


def test_rapport_contenu():
    classement = [_ligne("Martin", False, "ne bat pas QQQ"), _ligne("Durand", True)]
    resultats = {"sans_frais": _simulation(12.5, 3375.0), "avec_frais": _simulation(10.0, 3300.0)}
    texte = ecrire(classement, _bilan_vide(), resultats, _lecture(), DATE_CHOIX)
    liste = texte.split("## Liste proposée")[1].split("## Classement complet")[0]
    assert "Durand" in liste and "Martin" not in liste
    classe = texte.split("## Classement complet")[1].split("## Dirigeants")[0]
    assert classe.index("Durand") < classe.index("Martin")  # les retenus d'abord
    assert "ne bat pas QQQ" in classe and "+9,0 % / +4,0 %" in classe and "70 %" in classe
    portefeuille = texte.split("## Portefeuille")[1].split("## Données")[0]
    assert "Sans frais" in portefeuille and "Avec frais" in portefeuille and "+12,5 %" in portefeuille
    assert "stop 2" in portefeuille and "6 mois 1" in portefeuille and "titre déjà détenu : 2" in portefeuille
    donnees = texte.split("## Données")[1]
    assert "Sénat non lu (accord non donné)" in donnees
    assert "échec : SecInjoignable : coupure" in donnees
    assert "10 illisibles (10,0 %)" in donnees
    assert "41 personnes sans aucun achat retenu (fenêtre, univers ou montant)" in donnees
    assert "7 noms de fonds sans ticker sûr" in donnees
    assert "2 fonds sans 13F récent" in donnees and "Greenlight" in donnees and "Scion" in donnees
    for limite in ("nombre d'actions", "dividendes", "CUSIP", "administrateur"):
        assert limite in donnees


def test_rapport_aucune_personne_retenue():
    texte = ecrire([_ligne("Martin", False, "ne bat pas QQQ")], _bilan_vide(), {}, _lecture(), DATE_CHOIX)
    assert "Aucune personne ne passe les critères" in texte.split("## Classement complet")[0]


def test_rapport_dirigeants_et_senat_lu():
    b = {"achats": 40, "delai_moyen": 3.0, "dernier_achat": date(2026, 9, 1), "r3": 4.0, "q3": 2.0, "r6": 8.0,
         "q6": 3.0, "mesures": [(8.0, 3.0)], "r": 8.0, "q": 3.0, "part_battu": 0.6}
    lecture = _lecture(sources=[{"nom": "Sénat", "etat": "lue", "lues": 5, "illisibles": 0, "operations": 9}])
    texte = ecrire([], b, {}, lecture, DATE_CHOIX)
    assert "40 achats" in texte.split("## Dirigeants")[1].split("## Portefeuille")[0]
    assert "Sénat non lu" not in texte


# --- valider ---

def test_valider_copie_la_proposition(tmp_path):
    proposition = {"date_choix": "2026-10-21",
                   "personnes": [{"personne_id": "fonds:1067983", "personne": "Berkshire Hathaway", "famille": "fonds"}]}
    (tmp_path / "proposition.json").write_text(json.dumps(proposition))
    assert backtest_copie.valider(tmp_path, date(2026, 10, 22)) == 0
    suivis = json.loads((tmp_path / "suivis.json").read_text())
    assert suivis == {"valide_le": "2026-10-22", "personnes": proposition["personnes"]}


def test_valider_sans_proposition(tmp_path, capsys):
    assert backtest_copie.valider(tmp_path, date(2026, 10, 22)) == 1
    assert not (tmp_path / "suivis.json").exists()
    assert "proposition" in capsys.readouterr().out


# --- backtest_copie.py sans réseau ---

def _op(ticker, publication, personne_id, famille="politicien", sens="achat", rang=0, montant=20000.0, **details):
    return Operation(famille=famille, personne_id=personne_id, personne=personne_id.split(":")[-1].split("|")[0],
                     ticker=ticker, sens=sens, date_operation=publication - timedelta(days=10),
                     date_publication=publication, montant_min=montant, montant_max=montant * 2,
                     source_id=f"test:{personne_id}:{ticker}:{publication}:{sens}:{rang}", details=details)


def _achats_gagnants():
    """10 achats d'un élu sur un titre en hausse régulière, le dernier récent."""
    depart = date(2025, 5, 5)
    return [_op("AAA", depart + timedelta(days=40 * i), "politicien:chambre:Dupont|Jean|CA-01", rang=i)
            for i in range(10)]


def _cours_faux(tickers, debut, fin, dossier):
    n = len(JOURS)
    resultat = {}
    for ticker in tickers:
        if ticker == "AAA":
            prix = 100 * 1.002 ** np.arange(n)
        elif ticker == "EURUSD=X":
            prix = np.full(n, 1.1)
        else:
            prix = np.full(n, 400.0)
        resultat[ticker] = pd.DataFrame({"Open": prix, "High": prix * 1.01, "Low": prix * 0.995, "Close": prix,
                                         "Volume": 1_000_000.0}, index=JOURS)
    return resultat


class FauxClient:
    appels = []

    def __init__(self, dossier_cache=None, **_):
        self.dossier_cache = dossier_cache

    def octets(self, url, cache=False):
        FauxClient.appels.append(url)
        return None

    def json(self, url, cache=False):
        FauxClient.appels.append(url)
        if "CIK0001067983" in url:
            return {"filings": {"recent": {"form": ["13F-HR"], "filingDate": ["2026-08-14"],
                                           "reportDate": ["2026-06-30"], "accessionNumber": ["0001067983-26-000001"]}}}
        return {"filings": {"recent": {}}}


@pytest.fixture
def faux(monkeypatch):
    """Aucun réseau : toutes les sources, les cours et les actions sont remplacés."""
    FauxClient.appels = []
    monkeypatch.setattr(backtest_copie, "ClientSec", FauxClient)
    monkeypatch.setattr(chambre, "telecharger", lambda annees, dossier, session=None: (_achats_gagnants(), 90, 10))
    monkeypatch.setattr(senat, "telecharger", lambda *a, **k: pytest.fail("le Sénat ne doit pas être lu"))
    monkeypatch.setattr(fonds, "telecharger", lambda client, debut, dossier, p=None: ([], 3))
    monkeypatch.setattr(module_cours, "charger", _cours_faux)
    monkeypatch.setattr(module_cours, "actions_en_circulation", lambda tickers, dossier: {t: 1e9 for t in tickers})
    monkeypatch.setattr(dirigeants, "lire_jeu", lambda octets: [])


def _lancer(tmp_path, **options):
    return backtest_copie.lancer(
        config={"copie": P}, dossier_donnees=tmp_path / "donnees", dossier_copie=tmp_path / "copie",
        fichier_rapport=tmp_path / "rapport_copie.md", date_choix=DATE_CHOIX, **options)


def test_lancer_ecrit_rapport_proposition_et_bilans(tmp_path, faux, capsys):
    assert _lancer(tmp_path) == 0
    rapport = (tmp_path / "rapport_copie.md").read_text()
    assert "Dupont" in rapport.split("## Liste proposée")[1].split("## Classement complet")[0]
    assert "Sénat non lu (accord non donné)" in rapport
    assert "90 déclarations lues, 10 illisibles (10,0 %)" in rapport
    assert "12 fonds sans 13F récent" in rapport
    proposition = json.loads((tmp_path / "copie" / "proposition.json").read_text())
    assert proposition["date_choix"] == "2026-10-21"
    assert [p["personne_id"] for p in proposition["personnes"]] == ["politicien:chambre:Dupont|Jean|CA-01"]
    bilans = json.loads((tmp_path / "copie" / "bilans.json").read_text())
    assert bilans["politicien:chambre:Dupont|Jean|CA-01"]["achats"] == 10 and "dirigeants" in bilans
    assert not (tmp_path / "copie" / "suivis.json").exists()
    sortie = capsys.readouterr().out
    assert "Chambre" in sortie and "10,0 %" in sortie and "1 personne retenue" in sortie
    assert CONTACT not in sortie + rapport


def test_lancer_deux_simulations_avec_et_sans_frais(tmp_path, faux):
    _lancer(tmp_path)
    rapport = (tmp_path / "rapport_copie.md").read_text()
    portefeuille = rapport.split("## Portefeuille")[1].split("## Données")[0]
    assert "Sans frais" in portefeuille and "Avec frais" in portefeuille
    assert "ouverte" in portefeuille  # une position encore ouverte au 21/10/2026


def test_echec_d_une_source_ne_perd_pas_le_reste(tmp_path, faux, monkeypatch, capsys):
    def tombe(*a, **k):
        raise ConnectionError("coupure")
    monkeypatch.setattr(fonds, "telecharger", tombe)
    assert _lancer(tmp_path) == 0
    rapport = (tmp_path / "rapport_copie.md").read_text()
    assert "échec : ConnectionError: coupure" in rapport
    assert "Dupont" in rapport.split("## Liste proposée")[1].split("## Classement complet")[0]
    assert "échec" in capsys.readouterr().out


def test_sans_contact_sec_les_sources_sec_echouent_seules(tmp_path, faux, monkeypatch):
    def sans_contact(**_):
        raise ErreurSec("SEC_CONTACT manquant")
    monkeypatch.setattr(backtest_copie, "ClientSec", sans_contact)
    assert _lancer(tmp_path) == 0
    rapport = (tmp_path / "rapport_copie.md").read_text()
    assert rapport.count("échec : ErreurSec: SEC_CONTACT manquant") == 2  # Form 345 et 13F
    assert "Dupont" in rapport


def test_senat_lu_seulement_si_accord(tmp_path, faux, monkeypatch):
    appels = []

    def lire_senat(config, debut, fin, dossier, session=None):
        appels.append((debut, fin))
        return [], 4, 1
    monkeypatch.setattr(senat, "telecharger", lire_senat)
    backtest_copie.lancer(config={"copie": {**P, "senat_accepte": True}}, dossier_donnees=tmp_path / "d",
                          dossier_copie=tmp_path / "c", fichier_rapport=tmp_path / "r.md", date_choix=DATE_CHOIX)
    assert appels == [(date(2024, 1, 1), DATE_CHOIX)]
    assert "Sénat non lu" not in (tmp_path / "r.md").read_text()


def test_jeux_form345_du_premier_trimestre_au_dernier_publie(tmp_path, faux, monkeypatch):
    vide = io.BytesIO()
    with zipfile.ZipFile(vide, "w") as z:
        z.writestr("x", "x")
    publies = {dirigeants.url_jeu(a, t) for a in (2024, 2025) for t in range(1, 5)} | {
        dirigeants.url_jeu(2026, t) for t in (1, 2, 3)}

    class Client(FauxClient):
        def octets(self, url, cache=False):
            FauxClient.appels.append(url)
            return vide.getvalue() if url in publies else None
    monkeypatch.setattr(backtest_copie, "ClientSec", Client)
    _lancer(tmp_path)
    demandes = [u for u in FauxClient.appels if u.endswith("_form345.zip")]
    assert demandes[0] == dirigeants.url_jeu(2024, 1) and demandes[-1] == dirigeants.url_jeu(2026, 4)
    assert len(demandes) == 12  # 2024T1 à 2026T4, le dernier en 404
    rapport = (tmp_path / "rapport_copie.md").read_text()
    assert "dernier trimestre publié : 2026T3" in rapport
    ligne = next(l for l in rapport.splitlines() if l.startswith("- Form 4"))
    assert "échec" not in ligne and "11 jeux trimestriels lus, 0 illisible" in ligne


def test_trimestre_illisible_ne_perd_pas_les_autres(tmp_path, faux, monkeypatch):
    class Client(FauxClient):
        def octets(self, url, cache=False):
            return b"zip"
    monkeypatch.setattr(backtest_copie, "ClientSec", Client)
    appels = []

    def lire(octets):
        appels.append(1)
        if len(appels) == 2:
            raise dirigeants.ErreurDirigeants("jeu trimestriel : zip invalide")
        return []
    monkeypatch.setattr(dirigeants, "lire_jeu", lire)
    _lancer(tmp_path)
    assert len(appels) == 12  # 2024T1 à 2026T4, tous lus malgré l'échec du deuxième
    rapport = (tmp_path / "rapport_copie.md").read_text()
    assert "11 jeux trimestriels lus, 1 illisible (8,3 %)" in rapport
    assert "2024T2 : ErreurDirigeants: jeu trimestriel : zip invalide" in rapport


def test_sans_telechargement_relit_les_operations(tmp_path, faux, monkeypatch):
    _lancer(tmp_path)
    premier = (tmp_path / "rapport_copie.md").read_text()

    def interdit(*a, **k):
        pytest.fail("aucun téléchargement attendu")
    monkeypatch.setattr(chambre, "telecharger", interdit)
    monkeypatch.setattr(fonds, "telecharger", interdit)
    monkeypatch.setattr(backtest_copie, "ClientSec", interdit)
    assert _lancer(tmp_path, sans_telechargement=True) == 0
    assert (tmp_path / "rapport_copie.md").read_text() == premier


def test_sans_telechargement_sans_donnees(tmp_path, faux, capsys):
    assert _lancer(tmp_path, sans_telechargement=True) == 1
    assert not (tmp_path / "rapport_copie.md").exists()
    assert "lance d'abord backtest_copie.py" in capsys.readouterr().out


def test_filtre_ne_garde_que_les_operations_utiles():
    p = P
    achat_ok = _op("AAA", date(2026, 1, 5), "dirigeant:1", "dirigeant", code="P", dirigeant=True, montant=200_000.0)
    achat_petit = _op("BBB", date(2026, 1, 5), "dirigeant:2", "dirigeant", code="P", dirigeant=True, montant=5_000.0)
    vente_liee = _op("AAA", date(2026, 3, 5), "dirigeant:1", "dirigeant", sens="vente", code="S", dirigeant=True)
    vente_autre = _op("CCC", date(2026, 3, 5), "dirigeant:9", "dirigeant", sens="vente", code="S", dirigeant=True)
    politicien = _op("DDD", date(2026, 1, 5), "politicien:chambre:A|B|NY-01")
    garde = backtest_copie.filtrer([achat_ok, achat_petit, vente_liee, vente_autre, politicien], p)
    assert garde == [achat_ok, vente_liee, politicien]
