import json
from datetime import date, datetime

import pandas as pd
import pytest

import copie_matin
from court_terme import alpaca, boite
from court_terme.alpaca import ErreurAlpaca
from inities import copie_alpaca, cours, nouveautes, redaction, sec
from inities.modele import Operation, vers_dict
from inities.nouveautes import ErreurVus

PARIS = copie_matin.PARIS
ELU = "politicien:chambre:Jackson|Jonathan|IL01"
MERCREDI = datetime(2026, 10, 21, 8, 0, tzinfo=PARIS)
AUJOURD_HUI = MERCREDI.date()
SECRET = "jeton-ultra-secret-123"


def op(ticker="AAA", sens="achat", source="chambre:1:1", personne=ELU, date_operation=date(2026, 10, 14)):
    return Operation("politicien", personne, "Jonathan Jackson", ticker, sens, date_operation, date(2026, 10, 19),
                     50001.0, 100000.0, source)


def config(actif=False):
    return {"copie": {"alpaca_actif": actif, "heure_paris": "08:00", "descriptions_max": 5}}


class FauxClient:
    def __init__(self, equity="3012.5"):
        self.equity = equity

    def compte(self):
        return {"equity": self.equity}


class Copieur:
    """Remplace copier : note les arguments, peut écrire l'état des positions ou lever une erreur."""

    def __init__(self, messages=(), signales=(), erreur=None, positions=()):
        self.messages, self.signales, self.erreur, self.positions = list(messages), list(signales), erreur, positions
        self.appels = []

    def __call__(self, achats, ventes, derniers_cours, client, p, aujourd_hui):
        self.appels.append({"achats": list(achats), "ventes": list(ventes), "cours": dict(derniers_cours),
                            "client": client, "date": aujourd_hui})
        if self.positions:
            copie_alpaca.enregistrer_etat({"positions": list(self.positions), "historique": []})
        if self.erreur:
            raise self.erreur
        return self.messages, self.signales


def cours_de(*valeurs, debut="2026-10-12"):
    index = pd.bdate_range(debut, periods=len(valeurs))
    return pd.DataFrame({"Close": valeurs}, index=index)


@pytest.fixture
def monde(tmp_path, monkeypatch):
    """Dossier copie/ temporaire et doublures de tout ce qui touche au réseau ou à Claude."""
    dossier = tmp_path / "copie"
    dossier.mkdir()
    (dossier / "suivis.json").write_text(json.dumps({"personnes": [{"personne_id": ELU}]}))
    monkeypatch.setattr(copie_matin, "SUIVIS", dossier / "suivis.json")
    monkeypatch.setattr(copie_matin, "BILANS", dossier / "bilans.json")
    monkeypatch.setattr(copie_matin, "DERNIER_LANCEMENT", dossier / "dernier_lancement.json")
    monkeypatch.setattr(copie_matin, "VENTES_EN_ATTENTE", dossier / "ventes_en_attente.json")
    monkeypatch.setattr(nouveautes, "VUS", dossier / "vus.json")
    monkeypatch.setattr(copie_alpaca, "POSITIONS", dossier / "positions.json")
    monkeypatch.setattr(copie_alpaca, "SUSPENSION", dossier / "suspension.json")
    monkeypatch.setattr(copie_matin, "charger_config", lambda: config())
    monkeypatch.setattr(copie_matin, "DOSSIER_COURS", tmp_path / "cours")
    monde = type("Monde", (), {})()
    monde.dossier, monde.deposes, monde.crees = dossier, [], []
    monde.nouveaux_vus = {"dernier_passage": "2026-10-20", "vus": {"chambre:1": "2026-10-21"},
                          "achats_dirigeants": []}
    monde.resultat = ([], [], [], monde.nouveaux_vus)
    monde.collectes = []
    monde.cours = {}
    monde.descriptions = {}
    monde.decrits = []

    def collecter(suivis, vus, aujourd_hui, config, client_sec, **options):
        monde.collectes.append({"suivis": suivis, "vus": vus, "date": aujourd_hui, "client_sec": client_sec})
        return monde.resultat

    def decrire(achats):
        monde.decrits.append(list(achats))
        return monde.descriptions

    def charger(tickers, debut, fin, dossier, **options):
        monde.cours_demandes = (list(tickers), debut, fin)
        return {t: monde.cours[t] for t in tickers if t in monde.cours}

    monkeypatch.setattr(nouveautes, "collecter", collecter)
    monkeypatch.setattr(redaction, "decrire", decrire)
    monkeypatch.setattr(cours, "charger", charger)
    monkeypatch.setattr(sec, "ClientSec", lambda: "client-sec")
    monkeypatch.setattr(boite, "deposer", monde.deposes.append)

    def faux_client():
        monde.crees.append(1)
        return FauxClient()

    monkeypatch.setattr(alpaca, "Client", faux_client)
    return monde


def regler(monkeypatch, actif):
    monkeypatch.setattr(copie_matin, "charger_config", lambda: config(actif))


def ecrits(monde):
    return sorted(p.name for p in monde.dossier.iterdir())


# --- doit_lancer ---

def test_doit_lancer():
    def a(heure, minute, jour=21):
        return datetime(2026, 10, jour, heure, minute, tzinfo=PARIS)

    assert copie_matin.doit_lancer(a(7, 59), None) is False
    assert copie_matin.doit_lancer(a(8, 0), None) is True
    assert copie_matin.doit_lancer(a(8, 30), date(2026, 10, 20)) is True
    assert copie_matin.doit_lancer(a(8, 0, jour=24), None) is False  # samedi
    assert copie_matin.doit_lancer(a(8, 0, jour=25), None) is False  # dimanche
    assert copie_matin.doit_lancer(a(8, 30), date(2026, 10, 21)) is False  # déjà passé aujourd'hui
    assert copie_matin.doit_lancer(a(8, 30), None, heure_paris="09:00") is False


def test_hors_horaire_sans_force_ne_fait_rien(monde, monkeypatch):
    assert copie_matin.main([], maintenant=datetime(2026, 10, 21, 7, 0, tzinfo=PARIS)) == 0
    assert monde.collectes == [] and ecrits(monde) == ["suivis.json"]


def test_deja_passe_aujourd_hui_sans_force(monde):
    (monde.dossier / "dernier_lancement.json").write_text('{"jour": "2026-10-21"}')
    assert copie_matin.main([], maintenant=MERCREDI) == 0
    assert monde.collectes == []


def test_liste_non_validee(monde, capsys):
    (monde.dossier / "suivis.json").unlink()
    assert copie_matin.main(["--force"], maintenant=MERCREDI) == 0
    assert "Liste non validée" in capsys.readouterr().out
    assert monde.collectes == [] and monde.deposes == []


# --- déroulé normal ---

def test_rien_de_nouveau_pas_de_claude(monde, monkeypatch):
    regler(monkeypatch, True)
    copieur = Copieur()
    monkeypatch.setattr(copie_alpaca, "copier", copieur)
    assert copie_matin.main(["--force"], maintenant=MERCREDI) == 0
    assert monde.decrits == [] and monde.deposes == []
    assert len(copieur.appels) == 1 and copieur.appels[0]["achats"] == []  # les sorties sont traitées
    assert json.loads((monde.dossier / "vus.json").read_text()) == monde.nouveaux_vus
    assert json.loads((monde.dossier / "dernier_lancement.json").read_text()) == {"jour": "2026-10-21"}


def test_copie_desactivee_aucun_ordre(monde, monkeypatch):
    monkeypatch.setattr(alpaca, "Client", lambda: pytest.fail("le client Alpaca ne doit pas être créé"))
    monkeypatch.setattr(copie_alpaca, "copier", lambda *a, **k: pytest.fail("copier ne doit pas être appelé"))
    monde.resultat = ([op()], [op("BBB", "vente", "chambre:2:1")], [], monde.nouveaux_vus)
    assert copie_matin.main(["--force"], maintenant=MERCREDI) == 0
    assert len(monde.deposes) == 1
    assert "seulement signalé (copie désactivée)" in monde.deposes[0]
    assert "Compte fictif : copie désactivée" in monde.deposes[0]
    assert not (monde.dossier / "ventes_en_attente.json").exists()
    assert "vus.json" in ecrits(monde)


def test_dry_run_n_ecrit_rien(monde, monkeypatch, capsys):
    regler(monkeypatch, True)
    monkeypatch.setattr(alpaca, "Client", lambda: pytest.fail("pas d'Alpaca en essai à vide"))
    monkeypatch.setattr(copie_alpaca, "copier", lambda *a, **k: pytest.fail("pas de copie en essai à vide"))
    monkeypatch.setattr(redaction, "decrire", lambda *a, **k: pytest.fail("pas de Claude en essai à vide"))
    monde.resultat = ([op()], [], ["Form 4 : lecture impossible"], monde.nouveaux_vus)
    assert copie_matin.main(["--force", "--dry-run"], maintenant=MERCREDI) == 0
    assert ecrits(monde) == ["suivis.json"]
    assert monde.deposes == []
    sortie = capsys.readouterr().out
    assert "NOUVEL ACHAT" in sortie and "Form 4 : lecture impossible" in sortie


def test_descriptions_de_claude_dans_le_message(monde, monkeypatch):
    monde.resultat = ([op()], [], [], monde.nouveaux_vus)
    monde.descriptions = {"chambre:1:1": {"contexte": "Parti démocrate, commission du commerce"}}
    copie_matin.main(["--force"], maintenant=MERCREDI)
    assert monde.decrits == [[op()]]
    assert "Contexte : Parti démocrate, commission du commerce" in monde.deposes[0]


def test_bilan_et_variation(monde):
    (monde.dossier / "bilans.json").write_text(json.dumps(
        {ELU: {"achats": 12, "r6": 9.0, "q6": 4.0}}))
    # séances du 12 au 21 octobre ; l'achat date du 14 : référence 100, dernier cours 110
    monde.cours = {"AAA": cours_de(99.0, 99.5, 100.0, 105.0, 106.0, 108.0, 110.0)}
    monde.resultat = ([op(date_operation=date(2026, 10, 14))], [], [], monde.nouveaux_vus)
    copie_matin.main(["--force"], maintenant=MERCREDI)
    assert "Variation depuis leur achat : +10.0 %" in monde.deposes[0]
    assert "12 achats, copie +9.0 % contre +4.0 % pour QQQ à 6 mois" in monde.deposes[0]


def test_variation_indisponible_sans_cours(monde):
    monde.resultat = ([op()], [], [], monde.nouveaux_vus)
    copie_matin.main(["--force"], maintenant=MERCREDI)
    assert "Variation depuis leur achat : indisponible" in monde.deposes[0]
    assert "Historique de leurs achats : indisponible" in monde.deposes[0]


def test_variation_au_premier_jour_disponible_apres(monde):
    # achat un samedi (17) : première séance disponible = lundi 19
    monde.cours = {"AAA": cours_de(50.0, 80.0, 100.0, 120.0, debut="2026-10-16")}
    monde.resultat = ([op(date_operation=date(2026, 10, 17))], [], [], monde.nouveaux_vus)
    copie_matin.main(["--force"], maintenant=MERCREDI)
    assert "+50.0 %" in monde.deposes[0]  # 120 contre 80 (lundi 19)


def test_copie_active_derniers_cours_compte_et_signales(monde, monkeypatch):
    regler(monkeypatch, True)
    monde.cours = {"AAA": cours_de(100.0, 101.0, float("nan"), 102.0), "BBB": cours_de(50.0, 51.0)}
    achats = [op("AAA"), op("BBB", source="chambre:2:1")]
    monde.resultat = (achats, [], [], monde.nouveaux_vus)
    copieur = Copieur(messages=["Ordre d'achat passé : 3 AAA, stop à 86,70 $."],
                      signales=[(achats[1], "plafond de 10 positions atteint")],
                      positions=[{"personne_id": ELU, "ticker": "AAA", "source_id": "chambre:1:1"}])
    monkeypatch.setattr(copie_alpaca, "copier", copieur)
    copie_matin.main(["--force"], maintenant=MERCREDI)
    appel = copieur.appels[0]
    assert appel["cours"]["AAA"] == 102.0 and appel["cours"]["BBB"] == 51.0
    assert isinstance(appel["client"], FauxClient) and appel["date"] == AUJOURD_HUI
    textes = "\n".join(monde.deposes)
    assert "Ordre d'achat passé : 3 AAA" in textes
    assert "Compte fictif : 3 012,50 $ (compte paper)" in textes
    assert "seulement signalé (plafond de 10 positions atteint)" in textes
    # l'achat copié n'est pas « seulement signalé »
    assert textes.count("seulement signalé") == 1


def test_cours_limites_aux_10_derniers_jours(monde):
    monde.cours = {"AAA": cours_de(1.0, 2.0, debut="2026-08-03")}  # vieux cours : pas de cours récent
    monde.resultat = ([op()], [], [], monde.nouveaux_vus)
    copie_matin.main(["--force"], maintenant=MERCREDI)
    tickers, debut, fin = monde.cours_demandes
    assert tickers == ["AAA"] and fin == AUJOURD_HUI and debut <= date(2026, 10, 11)


def test_notes_regroupees_en_une_alerte(monde):
    monde.resultat = ([], [], ["Sénat : non lu", "Form 4 : 2 jours sautés"], monde.nouveaux_vus)
    copie_matin.main(["--force"], maintenant=MERCREDI)
    assert len(monde.deposes) == 1
    assert monde.deposes[0].startswith("⚠️ COPIE D'INITIÉS")
    assert "Sénat : non lu" in monde.deposes[0] and "Form 4 : 2 jours sautés" in monde.deposes[0]


def test_messages_de_copier_deposes(monde, monkeypatch):
    regler(monkeypatch, True)
    monkeypatch.setattr(copie_alpaca, "copier", Copieur(messages=[
        "Stop exécuté sur AAA : position fermée.", "Copie suspendue : écart avec le compte. Plus aucun ordre."]))
    copie_matin.main(["--force"], maintenant=MERCREDI)
    assert monde.deposes[0] == "Stop exécuté sur AAA : position fermée."
    assert monde.deposes[1].startswith("⚠️ COPIE D'INITIÉS") and "Copie suspendue" in monde.deposes[1]


# --- ventes en attente ---

def test_ventes_en_attente(monde, monkeypatch):
    regler(monkeypatch, True)
    ancienne = op("OLD", "vente", "chambre:7:1")
    sans_position = op("GONE", "vente", "chambre:8:1")
    nouvelle = op("NEW", "vente", "chambre:9:1")
    (monde.dossier / "ventes_en_attente.json").write_text(json.dumps(
        [vers_dict(ancienne), vers_dict(sans_position)]))
    monde.resultat = ([], [ancienne, nouvelle], [], monde.nouveaux_vus)  # « ancienne » revient : pas de doublon
    copieur = Copieur(positions=[
        {"personne_id": ELU, "ticker": "OLD", "source_id": "x:1"},
        {"personne_id": ELU, "ticker": "NEW", "source_id": "x:2"}])
    monkeypatch.setattr(copie_alpaca, "copier", copieur)
    copie_matin.main(["--force"], maintenant=MERCREDI)
    passees = [v.source_id for v in copieur.appels[0]["ventes"]]
    assert passees == ["chambre:7:1", "chambre:8:1", "chambre:9:1"]
    restantes = json.loads((monde.dossier / "ventes_en_attente.json").read_text())
    assert [v["source_id"] for v in restantes] == ["chambre:7:1", "chambre:9:1"]  # GONE n'a plus de position


# --- erreurs ---

def test_erreur_vus(monde, monkeypatch):
    def lever():
        raise ErreurVus("vus.json illisible (JSON invalide)")

    monkeypatch.setattr(nouveautes, "charger_vus", lever)
    assert copie_matin.main(["--force"], maintenant=MERCREDI) == 1
    assert len(monde.deposes) == 1 and "vus.json illisible" in monde.deposes[0]
    assert monde.collectes == [] and ecrits(monde) == ["suivis.json"]


def test_erreur_alpaca_ne_depose_aucun_achat_et_n_ecrit_pas_vus(monde, monkeypatch):
    regler(monkeypatch, True)
    monde.resultat = ([op()], [op("BBB", "vente", "chambre:2:1")], [], monde.nouveaux_vus)
    monkeypatch.setattr(copie_alpaca, "copier", Copieur(erreur=ErreurAlpaca("Alpaca injoignable")))
    assert copie_matin.main(["--force"], maintenant=MERCREDI) == 1
    assert len(monde.deposes) == 1
    assert monde.deposes[0].startswith("⚠️ COPIE D'INITIÉS") and "Alpaca injoignable" in monde.deposes[0]
    assert "NOUVEL ACHAT" not in monde.deposes[0]
    assert ecrits(monde) == ["suivis.json"]


def test_client_alpaca_impossible_a_creer(monde, monkeypatch):
    regler(monkeypatch, True)

    def lever():
        raise ErreurAlpaca("ALPACA_API_KEY_ID manquant")

    monkeypatch.setattr(alpaca, "Client", lever)
    monkeypatch.setattr(copie_alpaca, "copier", lambda *a, **k: pytest.fail("pas de copie sans client"))
    monde.resultat = ([op()], [], [], monde.nouveaux_vus)
    assert copie_matin.main(["--force"], maintenant=MERCREDI) == 1
    assert "ALPACA_API_KEY_ID manquant" in monde.deposes[0] and "NOUVEL ACHAT" not in monde.deposes[0]
    assert ecrits(monde) == ["suivis.json"]


def test_autre_exception_de_copier(monde, monkeypatch, capsys):
    regler(monkeypatch, True)
    monde.resultat = ([op()], [], [], monde.nouveaux_vus)
    monkeypatch.setattr(copie_alpaca, "copier", Copieur(erreur=RuntimeError(f"TOKEN={SECRET}")))
    copie_matin.main(["--force"], maintenant=MERCREDI)
    textes = "\n".join(monde.deposes)
    assert "RuntimeError" in textes and SECRET not in textes
    assert "seulement signalé (copie suspendue)" in textes
    assert "vus.json" in ecrits(monde) and "dernier_lancement.json" in ecrits(monde)
    sortie = capsys.readouterr()
    assert SECRET not in sortie.out + sortie.err


def test_copie_suspendue_avant_le_passage_signale_les_achats(monde, monkeypatch):
    regler(monkeypatch, True)
    copie_alpaca.SUSPENSION.write_text("{}")
    monde.resultat = ([op()], [], [], monde.nouveaux_vus)
    monkeypatch.setattr(copie_alpaca, "copier", Copieur())  # copier ne renvoie rien quand la copie est suspendue
    copie_matin.main(["--force"], maintenant=MERCREDI)
    assert "seulement signalé (copie suspendue)" in monde.deposes[0]


def test_client_sec_sans_contact(monde, monkeypatch, capsys):
    def lever():
        raise sec.ErreurSec("SEC_CONTACT manquant (.env ou secrets GitHub) : la SEC exige un contact")

    monkeypatch.setattr(sec, "ClientSec", lever)
    assert copie_matin.main(["--force"], maintenant=MERCREDI) == 1
    assert "SEC_CONTACT manquant" in monde.deposes[0] and ecrits(monde) == ["suivis.json"]
