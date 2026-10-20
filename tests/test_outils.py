import json
from datetime import date

import pytest

from court_terme import attente
from outils import engouement, fermer, info, notes, polymarket
from outils import signal as outil_signal

JOUR = date(2026, 10, 20)


def candidat(ticker, industrie="Semiconductors"):
    return {"ticker": ticker, "configuration": "A", "cloture": 100.0, "atr": 2.0, "rsi": 40.0,
            "force_relative": 5.0, "entree": 99.5, "stop": 96.5, "objectif": 105.5, "industrie": industrie}


@pytest.fixture
def agent(tmp_path, monkeypatch):
    """Fichiers de l'agent dans un dossier temporaire, Telegram et taux de change simulés."""
    envois = []
    candidats, etat = tmp_path / "candidats-du-jour.json", tmp_path / "positions.json"
    candidats.write_text(json.dumps({"jour": JOUR.isoformat(), "candidats": [candidat("AAA")]}))
    for module in (outil_signal, info, fermer):
        monkeypatch.setattr(module, "ETAT", etat)
        monkeypatch.setattr(module, "EN_ATTENTE", tmp_path / "en-attente.json")
    monkeypatch.setattr(outil_signal, "CANDIDATS", candidats)
    monkeypatch.setattr(outil_signal, "taux_eurusd", lambda: 1.0)
    monkeypatch.setattr(outil_signal, "aujourd_hui", lambda: JOUR)
    monkeypatch.setattr(fermer, "aujourd_hui", lambda: JOUR)
    return etat, envois


def en_attente(etat):
    return attente.lire(etat.parent / "en-attente.json")


def ecrire_etat(chemin, suivis):
    ordres = [{**candidat(t, industrie=None), "quantite": 1, "traite_jusqu_au": "2026-10-19", "seances": 0}
              for t in suivis]
    chemin.write_text(json.dumps({"capital_initial": 3000, "debut": "2026-10-19", "ordres": ordres,
                                  "positions": [], "fermees": []}))


def test_signal_refuse_hors_candidats(agent, capsys):
    etat, envois = agent
    outil_signal.main(["ZZZ", "belle tendance"])
    assert "refusé" in capsys.readouterr().out
    assert en_attente(etat)["messages"] == [] and not etat.exists()


def test_signal_refuse_au_plafond(agent, capsys):
    etat, envois = agent
    ecrire_etat(etat, ["B1", "B2", "B3", "B4", "B5"])
    avant = etat.read_text()
    outil_signal.main(["AAA", "belle tendance"])
    assert "refusé" in capsys.readouterr().out
    assert en_attente(etat)["messages"] == [] and etat.read_text() == avant


def test_signal_mis_en_attente(agent, capsys):
    # Rien n'est envoyé ni enregistré pendant la session : agent.py applique les signaux si elle réussit
    etat, envois = agent
    outil_signal.main(["AAA", "Tendance intacte."])
    assert "signal enregistré" in capsys.readouterr().out
    assert envois == [] and not etat.exists()
    attendu = en_attente(etat)
    ordre = attendu["etat"]["ordres"][0]
    assert ordre["ticker"] == "AAA" and ordre["entree"] == 99.5 and ordre["date_signal"] == "2026-10-20"
    assert len(attendu["messages"]) == 1 and "Tendance intacte." in attendu["messages"][0]
    outil_signal.main(["AAA", "Encore."])                  # le second signal voit le premier
    assert "refusé" in capsys.readouterr().out and len(en_attente(etat)["etat"]["ordres"]) == 1


def test_signal_refuse_candidats_perimes(agent, capsys, monkeypatch):
    monkeypatch.setattr(outil_signal, "aujourd_hui", lambda: date(2026, 10, 21))
    outil_signal.main(["AAA", "belle tendance"])
    assert "refusé" in capsys.readouterr().out and agent[1] == []


def test_info_refuse_ticker_non_suivi(agent, capsys):
    etat, envois = agent
    ecrire_etat(etat, ["AAA"])
    info.main(["ZZZ", "résultats avancés"])
    assert "refusé" in capsys.readouterr().out and en_attente(etat)["messages"] == []
    info.main(["AAA", "résultats avancés, voir https://exemple.com/x " + "a" * 600])
    message = en_attente(etat)["messages"][0]
    assert envois == [] and message.startswith("ℹ️ AAA — résultats avancés")
    assert "http" not in message and len(message) <= 420


def test_notes_tronquees(tmp_path, monkeypatch):
    fichier = tmp_path / "notes.md"
    sections = "".join(f"## 2026-01-{j:02d}\n" + "ligne\n" * 30 for j in range(1, 8))
    fichier.write_text("# Notes de l'agent court terme\n" + sections)
    monkeypatch.setattr(notes, "NOTES", fichier)
    monkeypatch.setattr(notes, "aujourd_hui", lambda: JOUR)
    notes.main(["ajouter", "NVDA rejeté : couteau qui tombe."])
    texte = fichier.read_text()
    assert len(texte.splitlines()) <= 200
    assert texte.startswith("# Notes de l'agent court terme")
    assert "## 2026-01-01" not in texte and "## 2026-01-07" in texte
    assert texte.rstrip().endswith("## 2026-10-20\nNVDA rejeté : couteau qui tombe.")


class Reponse:
    def __init__(self, donnees):
        self.donnees = donnees

    def raise_for_status(self):
        pass

    def json(self):
        return self.donnees


def test_engouement_non_classe(monkeypatch, capsys):
    pages = []

    def faux_get(url, **kwargs):
        pages.append(url)
        resultats = [{"rank": 3, "ticker": "NVDA", "mentions": 120, "mentions_24h_ago": 80, "upvotes": 400}]
        return Reponse({"pages": 2, "results": resultats if url.endswith("/1") else []})

    monkeypatch.setattr(engouement.requests, "get", faux_get)
    engouement.main(["NVDA", "ZZZ"])
    sortie = capsys.readouterr().out
    assert "NVDA" in sortie and "120" in sortie
    assert "ZZZ : non classé" in sortie and len(pages) == 2


def test_polymarket_filtre_et_trie(monkeypatch, capsys):
    marches = [{"groupItemTitle": "Pas de changement", "outcomePrices": '["0.86", "0.14"]', "oneWeekPriceChange": 0.07},
               {"groupItemTitle": "Hausse de 25 pb", "outcomePrices": '["0.14", "0.86"]', "oneWeekPriceChange": 0.001},
               {"groupItemTitle": "Clos", "outcomePrices": '["1", "0"]', "closed": True}]
    evenements = [{"title": "Petit pari", "volume": 50, "markets": marches},
                  {"title": "Décision de la Fed", "volume": 5_000_000, "endDate": "2026-10-29T00:00:00Z",
                   "markets": marches}]
    monkeypatch.setattr(polymarket.requests, "get", lambda url, **kwargs: Reponse({"events": evenements}))
    polymarket.main(["Fed"])
    sortie = capsys.readouterr().out
    assert "Petit pari" not in sortie and "Clos" not in sortie
    assert "Pas de changement : 86 % (+7 pts sur 7 j)" in sortie and "Hausse de 25 pb : 14 %\n" in sortie
    assert sortie.index("Pas de changement") < sortie.index("Hausse")


def test_fermer_annule_un_ordre(agent, capsys):
    etat, envois = agent
    ecrire_etat(etat, ["AAA", "BBB"])
    fermer.main(["AAA", "Enquête annoncée."])
    assert "annulé" in capsys.readouterr().out and not envois
    attendu = en_attente(etat)
    assert [o["ticker"] for o in attendu["etat"]["ordres"]] == ["BBB"]
    assert attendu["messages"][0].startswith("❎ ORDRE ANNULÉ — AAA") and "Enquête annoncée." in attendu["messages"][0]


def test_fermer_vend_une_position_puis_libere_la_place(agent, capsys):
    etat, envois = agent
    ecrire_etat(etat, ["B1", "B2", "B3", "B4", "B5"])
    donnees = json.loads(etat.read_text())
    donnees["positions"] = [{**donnees["ordres"].pop(), "prix_entree": 99.5, "date_entree": "2026-10-19"}]
    etat.write_text(json.dumps(donnees))
    outil_signal.main(["AAA", "Meilleur dossier."])
    assert "refusé" in capsys.readouterr().out                                       # 5 titres suivis
    fermer.main(["B5", "Thèse cassée."])
    assert "vente de B5 enregistrée" in capsys.readouterr().out
    position = en_attente(etat)["etat"]["positions"][0]
    assert position["sortie_demandee"] == JOUR.isoformat() and position["justification_sortie"] == "Thèse cassée."
    fermer.main(["B5", "Encore."])
    assert "déjà demandée" in capsys.readouterr().out
    outil_signal.main(["AAA", "Meilleur dossier."])
    assert "signal enregistré" in capsys.readouterr().out


def test_fermer_refuse_ticker_non_suivi(agent, capsys):
    etat, envois = agent
    ecrire_etat(etat, ["AAA"])
    fermer.main(["ZZZ", "raison"])
    assert "refusé" in capsys.readouterr().out and en_attente(etat)["messages"] == []
