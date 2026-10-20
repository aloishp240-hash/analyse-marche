import json
from datetime import date

import pytest

from outils import engouement, info, notes
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
    for module in (outil_signal, info):
        monkeypatch.setattr(module, "ETAT", etat)
        monkeypatch.setattr(module, "envoyer_telegram", envois.append)
    monkeypatch.setattr(outil_signal, "CANDIDATS", candidats)
    monkeypatch.setattr(outil_signal, "taux_eurusd", lambda: 1.0)
    monkeypatch.setattr(outil_signal, "aujourd_hui", lambda: JOUR)
    return etat, envois


def ecrire_etat(chemin, suivis):
    ordres = [{**candidat(t, industrie=None), "quantite": 1, "traite_jusqu_au": "2026-10-19", "seances": 0}
              for t in suivis]
    chemin.write_text(json.dumps({"capital_initial": 3000, "debut": "2026-10-19", "ordres": ordres,
                                  "positions": [], "fermees": []}))


def test_signal_refuse_hors_candidats(agent, capsys):
    etat, envois = agent
    outil_signal.main(["ZZZ", "belle tendance"])
    assert "refusé" in capsys.readouterr().out
    assert envois == [] and not etat.exists()


def test_signal_refuse_au_plafond(agent, capsys):
    etat, envois = agent
    ecrire_etat(etat, ["B1", "B2", "B3", "B4", "B5"])
    avant = etat.read_text()
    outil_signal.main(["AAA", "belle tendance"])
    assert "refusé" in capsys.readouterr().out
    assert envois == [] and etat.read_text() == avant


def test_signal_accepte(agent, capsys):
    etat, envois = agent
    outil_signal.main(["AAA", "Tendance intacte."])
    assert "signal enregistré" in capsys.readouterr().out
    ordre = json.loads(etat.read_text())["ordres"][0]
    assert ordre["ticker"] == "AAA" and ordre["entree"] == 99.5 and ordre["date_signal"] == "2026-10-20"
    assert len(envois) == 1 and "AAA" in envois[0] and "Tendance intacte." in envois[0]


def test_signal_refuse_candidats_perimes(agent, capsys, monkeypatch):
    monkeypatch.setattr(outil_signal, "aujourd_hui", lambda: date(2026, 10, 21))
    outil_signal.main(["AAA", "belle tendance"])
    assert "refusé" in capsys.readouterr().out and agent[1] == []


def test_info_refuse_ticker_non_suivi(agent, capsys):
    etat, envois = agent
    ecrire_etat(etat, ["AAA"])
    info.main(["ZZZ", "résultats avancés"])
    assert "refusé" in capsys.readouterr().out and envois == []
    info.main(["AAA", "résultats avancés"])
    assert envois == ["ℹ️ AAA — résultats avancés"]


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
