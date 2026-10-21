import json
from pathlib import Path

import backtest
import journal_personnel
from court_terme.preglages import NOMS, VERSIONS

RACINE = Path(__file__).resolve().parents[1]


def test_chaque_version_a_ses_rubriques():
    for v in VERSIONS:
        assert v["preglage"] in NOMS
        assert all(v.get(cle) for cle in ("id", "date", "modifications", "conception", "validation", "metriques",
                                          "limites"))
    assert VERSIONS[0]["id"] == "reference-04973f7"


def test_le_registre_ajoute_sans_reecrire(tmp_path):
    chemin = tmp_path / "registre.json"
    rapport = [{"preglage": "reference", "debut": "2023-11-03", "fin": "2026-10-20", "signaux": 3, "refus": 1,
                "mesures": {"nombre": 2}}]
    backtest.enregistrer(rapport, {"reference": {"risque_pct": 2}}, chemin)
    backtest.enregistrer(rapport, {"reference": {"risque_pct": 2}}, chemin)
    registre = json.loads(chemin.read_text())
    assert len(registre) == 2 and registre[0]["version"] == "reference-04973f7"
    assert registre[0]["parametres"] == {"risque_pct": 2} and registre[0]["periode"] == ["2023-11-03", "2026-10-20"]


def test_journal_personnel_reste_local_et_separe(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(journal_personnel, "JOURNAL", tmp_path / "journal.json")
    assert "personnel/" in (RACINE / ".gitignore").read_text().split()
    assert journal_personnel.main(["ajouter", "aapl", "achat", "100", "2", "--frais", "1", "--date", "2026-10-01",
                                   "--ref", "2026-10-01-AAPL-abc123"]) == 0
    assert journal_personnel.main(["ajouter", "AAPL", "vente", "110", "2", "--frais", "1", "--date", "2026-10-05"]) == 0
    assert "Aucun ordre n'a été passé" in capsys.readouterr().out
    journal_personnel.main(["bilan"])
    sortie = capsys.readouterr().out
    assert "AAPL : résultat réalisé +18.00 USD" in sortie and "fictif n'y figure pas" in sortie
    assert journal_personnel.main(["ajouter", "AAPL; rm", "achat", "1", "1"]) == 1
    assert len(json.loads((tmp_path / "journal.json").read_text())["operations"]) == 2
    assert not (RACINE / "personnel" / "journal.json").exists()


def test_bilan_signale_une_vente_impossible():
    titres, alertes = journal_personnel.bilan([
        {"date": "2026-10-01", "ticker": "AAA", "sens": "achat", "prix": 10, "quantite": 1, "frais": 0, "devise": "USD"},
        {"date": "2026-10-02", "ticker": "AAA", "sens": "vente", "prix": 12, "quantite": 3, "frais": 0, "devise": "USD"}])
    assert titres["AAA"]["realise_net"] == 2 and len(alertes) == 1
