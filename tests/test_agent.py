import subprocess
from datetime import date, datetime

import agent
from commun import NY

MARDI = date(2026, 10, 20)


def test_doit_lancer():
    assert agent.doit_lancer(datetime(2026, 10, 20, 16, 50, tzinfo=NY), MARDI, False)
    assert not agent.doit_lancer(datetime(2026, 10, 20, 16, 30, tzinfo=NY), MARDI, False)
    assert not agent.doit_lancer(datetime(2026, 10, 24, 16, 50, tzinfo=NY), date(2026, 10, 23), False)  # samedi
    assert not agent.doit_lancer(datetime(2026, 11, 26, 16, 50, tzinfo=NY), date(2026, 11, 25), False)  # férié
    assert not agent.doit_lancer(datetime(2026, 10, 20, 16, 50, tzinfo=NY), MARDI, True)


def test_commande_claude_sans_shell_libre():
    commande = agent.commande_claude("consigne")
    assert commande[:3] == ["claude", "-p", "consigne"]
    assert "Bash(*)" not in commande and not any(c.startswith("Bash(git") for c in commande)
    assert all(c.startswith(("Bash(python outils/", "Bash(python fiche.py")) for c in commande if c.startswith("Bash"))


def test_session_echouee(tmp_path, monkeypatch):
    envois = []
    monkeypatch.setattr(agent, "SESSIONS", tmp_path)
    monkeypatch.setattr(agent, "envoyer_telegram", envois.append)
    monkeypatch.setattr(agent.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr="quota atteint"))
    agent.lancer_session("consigne", MARDI)
    assert not (tmp_path / "2026-10-20.md").exists()
    assert len(envois) == 1 and "session échouée" in envois[0]
    assert not agent.session_faite(MARDI)          # une nouvelle tentative reste possible
    agent.lancer_session("consigne", MARDI)
    assert agent.session_faite(MARDI) and len(envois) == 2   # au deuxième échec, on abandonne pour la journée


def test_session_reussie(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "SESSIONS", tmp_path)
    monkeypatch.setattr(agent.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="AAA : ✅ validé — ok", stderr=""))
    agent.lancer_session("consigne", MARDI)
    assert "AAA : ✅ validé" in (tmp_path / "2026-10-20.md").read_text()
    assert agent.session_faite(MARDI)
