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


def test_rapport_delai_avec_une_decimale_et_pas_de_zero_negatif():
    ligne = _ligne("Durand", True)
    ligne["bilan"].update(delai_moyen=29.6, r3=-0.02, q3=0.01)
    texte = ecrire([ligne], _bilan_vide(), {}, _lecture(), DATE_CHOIX)
    classe = texte.split("## Classement complet")[1].split("## Dirigeants")[0]
    assert "29,6 j" in classe
    assert "-0,0" not in texte and "+0,0" not in texte and "0,0 % / +0,0" not in texte


def test_rapport_classement_avec_des_valeurs_absentes():
    b = {"achats": 2, "delai_moyen": None, "dernier_achat": None, "r3": None, "q3": None, "r6": None, "q6": None,
         "mesures": [], "r": None, "q": None, "part_battu": None}
    ligne = {"personne_id": "politicien:chambre:Vide|X|CA-01", "personne": "Vide", "famille": "politicien", "bilan": b,
             "retenu": False, "motif": "pas assez d'achats mesurables"}
    texte = ecrire([ligne], b, {}, _lecture(), DATE_CHOIX)
    classe = texte.split("## Classement complet")[1].split("## Dirigeants")[0]
    assert "| Vide |" in classe and "—" in classe
    assert "None" not in texte and "nan" not in texte.lower().replace("financ", "")


def test_rapport_dirigeants_sans_copie_mesurable():
    b = {"achats": 3, "delai_moyen": 4.0, "dernier_achat": date(2026, 9, 1), "r3": None, "q3": None, "r6": None,
         "q6": None, "mesures": [], "r": None, "q": None, "part_battu": None}
    texte = ecrire([], b, {}, _lecture(), DATE_CHOIX)
    dirigeants_ = texte.split("## Dirigeants")[1].split("## Portefeuille")[0]
    assert "pas encore mesurable" in dirigeants_ and "ne bat pas" not in dirigeants_


def test_rapport_titres_sans_cours_et_sans_actions():
    lecture = _lecture(sans_cours={"nombre": 12, "exemples": ["AAA", "BBB"]},
                       sans_actions={"nombre": 1, "exemples": ["CCC"]})
    donnees = ecrire([], _bilan_vide(), {}, lecture, DATE_CHOIX).split("## Données")[1]
    assert "12 titres sans cours" in donnees and "par exemple AAA, BBB" in donnees
    assert "1 titre sans nombre d'actions" in donnees and "par exemple CCC" in donnees


def test_rapport_signale_une_proposition_incomplete():
    texte = ecrire([], _bilan_vide(), {}, _lecture(), DATE_CHOIX)  # _lecture a une source en échec
    assert "proposition incomplète" in texte.split("## Liste proposée")[0]
    complet = ecrire([], _bilan_vide(), {}, {"sources": [{"nom": "Chambre", "etat": "lue", "lues": 1}]}, DATE_CHOIX)
    assert "incomplète" not in complet


def test_totaux_comptent_les_jeux_trimestriels_a_part():
    from inities.rapport import totaux
    lecture = {"sources": [{"nom": "Chambre", "etat": "lue", "lues": 90, "illisibles": 10},
                           {"nom": "Form 4", "etat": "lue", "lues": 11, "illisibles": 1, "a_part": True}]}
    assert totaux(lecture) == (90, 10)


# --- valider ---

def test_valider_copie_la_proposition(tmp_path):
    proposition = {"date_choix": "2026-10-21", "complet": True, "sources_en_echec": [],
                   "personnes": [{"personne_id": "fonds:1067983", "personne": "Berkshire Hathaway", "famille": "fonds"}]}
    (tmp_path / "proposition.json").write_text(json.dumps(proposition))
    assert backtest_copie.valider(tmp_path, date(2026, 10, 22)) == 0
    suivis = json.loads((tmp_path / "suivis.json").read_text())
    assert suivis == {"valide_le": "2026-10-22", "personnes": proposition["personnes"]}


def test_valider_refuse_une_proposition_incomplete(tmp_path, capsys):
    proposition = {"date_choix": "2026-10-21", "complet": False, "sources_en_echec": ["13F (fonds)"],
                   "personnes": [{"personne_id": "x", "personne": "X", "famille": "fonds"}]}
    (tmp_path / "proposition.json").write_text(json.dumps(proposition))
    assert backtest_copie.valider(tmp_path, date(2026, 10, 22)) == 1
    assert not (tmp_path / "suivis.json").exists()
    sortie = capsys.readouterr().out
    assert "Validation refusée" in sortie and "incomplète" in sortie and "13F (fonds)" in sortie


def test_valider_refuse_une_proposition_sans_indication(tmp_path, capsys):
    (tmp_path / "proposition.json").write_text(json.dumps({"date_choix": "2026-10-21", "personnes": []}))
    assert backtest_copie.valider(tmp_path, date(2026, 10, 22)) == 1
    assert not (tmp_path / "suivis.json").exists()
    assert "Validation refusée" in capsys.readouterr().out


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


def _cours_faux(tickers, debut, fin, dossier, **_):
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
    assert _lancer(tmp_path) == 1  # tout est écrit, mais le code de sortie dit que la proposition est incomplète
    rapport = (tmp_path / "rapport_copie.md").read_text()
    assert "échec : ConnectionError: coupure" in rapport and "proposition incomplète" in rapport
    assert "Dupont" in rapport.split("## Liste proposée")[1].split("## Classement complet")[0]
    sortie = capsys.readouterr().out
    assert "échec" in sortie and "ATTENTION" in sortie and "13F (fonds)" in sortie
    proposition = json.loads((tmp_path / "copie" / "proposition.json").read_text())
    assert proposition["complet"] is False and proposition["sources_en_echec"] == ["13F (fonds)"]
    assert proposition["personnes"]  # la proposition reste lisible, mais ne se valide pas
    assert backtest_copie.valider(tmp_path / "copie", date(2026, 10, 22)) == 1
    assert not (tmp_path / "copie" / "suivis.json").exists()
    assert "Validation refusée" in capsys.readouterr().out


def test_sources_toutes_lues_donnent_une_proposition_complete(tmp_path, faux):
    assert _lancer(tmp_path) == 0
    proposition = json.loads((tmp_path / "copie" / "proposition.json").read_text())
    assert proposition["complet"] is True and proposition["sources_en_echec"] == []
    assert backtest_copie.valider(tmp_path / "copie", date(2026, 10, 22)) == 0


def test_sans_contact_sec_les_sources_sec_echouent_seules(tmp_path, faux, monkeypatch):
    def sans_contact(**_):
        raise ErreurSec("SEC_CONTACT manquant")
    monkeypatch.setattr(backtest_copie, "ClientSec", sans_contact)
    assert _lancer(tmp_path) == 1  # deux sources en échec : proposition incomplète
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


def test_trimestre_illisible_ne_perd_pas_les_autres(tmp_path, faux, monkeypatch, capsys):
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
    # le jeu illisible n'entre pas dans la part de PDF illisibles
    assert "Au total (déclarations en PDF, hors jeux trimestriels) : 90 lues, 10 illisibles (10,0 %)" in rapport
    assert "Résumé : 90 lues, 10 illisibles (10,0 %)" in capsys.readouterr().out


def _remplir_cache(tmp_path, tickers, actions=None):
    """Écrit dans donnees/cours/ ce que cours.charger et cours.actions_en_circulation y auraient gardé."""
    dossier = tmp_path / "donnees" / "cours"
    dossier.mkdir(parents=True, exist_ok=True)
    for ticker, df in _cours_faux(tickers, None, None, None).items():
        pd.to_pickle({"debut": backtest_copie.DEBUT_COURS, "fin": DATE_CHOIX, "cours": df},
                     module_cours._fichier(dossier, ticker))
    (dossier / "actions.json").write_text(json.dumps(actions if actions is not None else {t: 1e9 for t in tickers}))


def _interdire_le_reseau(monkeypatch):
    def interdit(*a, **k):
        pytest.fail("aucun accès au réseau attendu")
    for module, nom in ((chambre, "telecharger"), (fonds, "telecharger"), (module_cours, "charger"),
                        (module_cours, "actions_en_circulation"), (module_cours, "_telecharger"),
                        (module_cours, "_actions"), (backtest_copie, "ClientSec")):
        monkeypatch.setattr(module, nom, interdit)


def test_sans_telechargement_relit_les_operations(tmp_path, faux, monkeypatch):
    _lancer(tmp_path)
    premier = (tmp_path / "rapport_copie.md").read_text()
    _remplir_cache(tmp_path, ["QQQ", "EURUSD=X", "AAA"])
    _interdire_le_reseau(monkeypatch)
    assert _lancer(tmp_path, sans_telechargement=True) == 0
    assert (tmp_path / "rapport_copie.md").read_text() == premier


def test_sans_telechargement_compte_les_titres_absents_du_cache(tmp_path, faux, monkeypatch, capsys):
    _lancer(tmp_path)
    _remplir_cache(tmp_path, ["QQQ", "EURUSD=X"])  # AAA n'est pas en cache
    _interdire_le_reseau(monkeypatch)
    assert _lancer(tmp_path, sans_telechargement=True) == 0
    donnees = (tmp_path / "rapport_copie.md").read_text().split("## Données")[1]
    assert "1 titre sans cours" in donnees and "par exemple AAA" in donnees
    assert "Dupont" not in (tmp_path / "rapport_copie.md").read_text().split("## Classement complet")[0].split(
        "## Liste proposée")[1]
    assert "Titres : 1 sans cours (par exemple AAA)" in capsys.readouterr().out


def test_sans_telechargement_sans_cours_de_qqq(tmp_path, faux, monkeypatch, capsys):
    _lancer(tmp_path)
    _interdire_le_reseau(monkeypatch)  # cache de cours vide
    assert _lancer(tmp_path, sans_telechargement=True) == 1
    assert "QQQ introuvables" in capsys.readouterr().out


def test_titres_sans_cours_et_sans_actions_sont_comptes(tmp_path, faux, monkeypatch, capsys):
    ops = _achats_gagnants() + [_op("ZZZ", date(2026, 1, 5), "politicien:chambre:Martin|Paul|NY-01"),
                                _op("BBB", date(2026, 1, 6), "politicien:chambre:Martin|Paul|NY-01")]
    monkeypatch.setattr(chambre, "telecharger", lambda annees, dossier, session=None: (ops, 90, 10))
    monkeypatch.setattr(module_cours, "charger",
                        lambda tickers, debut, fin, dossier, **_: {t: df for t, df in _cours_faux(
                            tickers, debut, fin, dossier).items() if t != "ZZZ"})
    monkeypatch.setattr(module_cours, "actions_en_circulation",
                        lambda tickers, dossier: {t: 1e9 for t in tickers if t != "BBB"})
    assert _lancer(tmp_path) == 0
    donnees = (tmp_path / "rapport_copie.md").read_text().split("## Données")[1]
    assert "1 titre sans cours" in donnees and "par exemple ZZZ" in donnees
    assert "1 titre sans nombre d'actions" in donnees and "par exemple BBB" in donnees
    sortie = capsys.readouterr().out
    assert "Titres : 1 sans cours (par exemple ZZZ) ; 1 sans nombre d'actions (par exemple BBB)" in sortie
    assert sortie.rstrip().splitlines().count("Titres : 1 sans cours (par exemple ZZZ) ; 1 sans nombre d'actions "
                                              "(par exemple BBB) ; 0 ticker illisible.") == 1  # aussi dans le résumé final


def test_jeux_trimestriels_filtres_un_par_un(tmp_path, faux, monkeypatch):
    achat = _op("AAA", date(2025, 2, 5), "dirigeant:1", "dirigeant", code="P", dirigeant=True, montant=200_000.0)
    petit = _op("BBB", date(2025, 2, 5), "dirigeant:2", "dirigeant", code="P", dirigeant=True, montant=5_000.0)
    vente_autre = _op("CCC", date(2025, 2, 6), "dirigeant:9", "dirigeant", sens="vente", code="S", dirigeant=True)
    vente_liee = _op("AAA", date(2025, 5, 6), "dirigeant:1", "dirigeant", sens="vente", code="S", dirigeant=True)
    jeux = iter([[achat, petit, vente_autre], [vente_liee]])

    class Client(FauxClient):
        def octets(self, url, cache=False):
            return b"zip" if "2024q1" in url or "2024q2" in url else None
    monkeypatch.setattr(backtest_copie, "ClientSec", Client)
    monkeypatch.setattr(dirigeants, "lire_jeu", lambda octets: next(jeux))
    _lancer(tmp_path)
    gardees = json.loads((tmp_path / "donnees" / "operations.json").read_text())
    assert {(d["ticker"], d["sens"]) for d in gardees if d["famille"] == "dirigeant"} == {("AAA", "achat"),
                                                                                         ("AAA", "vente")}
    rapport = (tmp_path / "rapport_copie.md").read_text()
    assert "avant filtre : 4 opérations" in rapport


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


def _espionner_les_demandes(monkeypatch):
    """Garde les tickers demandés à Yahoo (cours et nombres d'actions), hors QQQ et EURUSD."""
    demandes = {"cours": set(), "actions": set()}

    def charger(tickers, debut, fin, dossier, **_):
        demandes["cours"].update(t for t in tickers if t not in ("QQQ", "EURUSD=X"))
        return _cours_faux(tickers, debut, fin, dossier)

    def actions(tickers, dossier, **_):
        demandes["actions"].update(tickers)
        return {t: 1e9 for t in tickers}
    monkeypatch.setattr(module_cours, "charger", charger)
    monkeypatch.setattr(module_cours, "actions_en_circulation", actions)
    return demandes


def test_seuls_les_titres_que_la_mesure_peut_utiliser_sont_demandes(tmp_path, faux, monkeypatch):
    martin = "politicien:chambre:Martin|Paul|NY-01"
    ops = _achats_gagnants() + [
        _op("VNT", date(2026, 1, 5), martin, sens="vente"),  # une vente ne demande jamais de cours
        _op("OLD", date(2023, 12, 5), martin),  # publié avant la fenêtre la plus large
        _op("SML", date(2026, 1, 5), martin, montant=1000.0),  # sous le montant minimal des élus
        _op("FUT", DATE_CHOIX, martin),  # publié le jour du choix : exclu
        _op("DIR", date(2026, 1, 5), "dirigeant:sec:X|Y", famille="dirigeant", code="S", dirigeant=True),  # pas un achat retenu
        _op("RET", date(2026, 1, 5), "dirigeant:sec:X|Y", famille="dirigeant", code="P", dirigeant=True,
            montant=P["dirigeant_montant_min"] + 1),
        _op("FND", date(2026, 1, 5), "fonds:Z", famille="fonds")]
    monkeypatch.setattr(chambre, "telecharger", lambda annees, dossier, session=None: (ops, 90, 10))
    demandes = _espionner_les_demandes(monkeypatch)
    assert _lancer(tmp_path) == 0
    assert demandes["cours"] == {"AAA", "RET", "FND"}
    assert demandes["actions"] == {"AAA", "RET", "FND"}


def test_tickers_illisibles_jamais_envoyes_a_yahoo_et_comptes(tmp_path, faux, monkeypatch, capsys):
    martin = "politicien:chambre:Martin|Paul|NY-01"
    invalides = ["NCLH]", "N O G", "[ NONE ]", "N.A.", "LEN, LEN.B", "HEI, HEI.A", "BBXIA/B"]
    ops = _achats_gagnants() + [_op(t, date(2026, 1, 5), martin, rang=i) for i, t in enumerate(invalides)] + [
        _op("NYSE:NYCB", date(2026, 1, 6), martin), _op("nasdaq: msft", date(2026, 1, 7), martin)]
    monkeypatch.setattr(chambre, "telecharger", lambda annees, dossier, session=None: (ops, 90, 10))
    demandes = _espionner_les_demandes(monkeypatch)
    assert _lancer(tmp_path) == 0
    assert demandes["cours"] == {"AAA", "NYCB", "MSFT"}  # préfixe retiré, majuscules, rien d'illisible
    donnees = (tmp_path / "rapport_copie.md").read_text().split("## Données")[1]
    assert "7 titres dont le ticker est illisible" in donnees
    assert "(par exemple BBXIA/B, HEI, HEI.A, LEN, LEN.B, N O G, N.A.)" in donnees  # 5 exemples, par ordre
    assert "7 ticker illisible (par exemple " in capsys.readouterr().out
