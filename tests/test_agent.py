import json
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
    envois = fichiers(tmp_path, monkeypatch)
    monkeypatch.setattr(agent.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr="quota atteint"))
    agent.lancer_session("consigne", MARDI)
    assert not (tmp_path / "2026-10-20.md").exists()
    assert len(envois) == 1 and "session échouée" in envois[0]
    assert not agent.session_faite(MARDI)          # une nouvelle tentative reste possible
    agent.lancer_session("consigne", MARDI)
    assert agent.session_faite(MARDI) and len(envois) == 2   # au deuxième échec, on abandonne pour la journée


def test_session_reussie(tmp_path, monkeypatch):
    fichiers(tmp_path, monkeypatch)
    monkeypatch.setattr(agent.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="AAA : ✅ validé — ok", stderr=""))
    agent.lancer_session("consigne", MARDI)
    assert "AAA : ✅ validé" in (tmp_path / "2026-10-20.md").read_text()
    assert agent.session_faite(MARDI)


def fichiers(tmp_path, monkeypatch):
    envois = []
    for nom, chemin in (("SESSIONS", tmp_path), ("ETAT", tmp_path / "positions.json"),
                        ("EN_ATTENTE", tmp_path / "en-attente.json")):
        monkeypatch.setattr(agent, nom, chemin)
    monkeypatch.setattr(agent, "envoyer_telegram", envois.append)
    return envois


def session_qui_signale(code):
    """Faux claude -p : met un signal en attente comme outils/signal.py, puis se termine avec `code`."""
    def run(*a, **k):
        etat = {"capital_initial": 3000, "debut": "2026-10-20", "ordres": [{"ticker": "AAA"}], "positions": [],
                "fermees": []}
        agent.EN_ATTENTE.write_text(json.dumps({"etat": etat, "messages": ["🟢 SIGNAL AAA"]}))
        return subprocess.CompletedProcess(a, code, stdout="AAA : ✅ validé — ok", stderr="")
    return run


def test_signaux_appliques_si_session_reussie(tmp_path, monkeypatch):
    envois = fichiers(tmp_path, monkeypatch)
    monkeypatch.setattr(agent.subprocess, "run", session_qui_signale(0))
    agent.lancer_session("consigne", MARDI)
    assert json.loads(agent.ETAT.read_text())["ordres"] == [{"ticker": "AAA"}]
    assert envois == ["🟢 SIGNAL AAA"] and not agent.EN_ATTENTE.exists()


def test_aucun_signal_si_session_echouee(tmp_path, monkeypatch):
    envois = fichiers(tmp_path, monkeypatch)
    monkeypatch.setattr(agent.subprocess, "run", session_qui_signale(1))
    agent.lancer_session("consigne", MARDI)
    assert not agent.ETAT.exists() and not agent.EN_ATTENTE.exists()
    assert len(envois) == 1 and "session échouée" in envois[0]


def test_claude_sans_lecture_de_fichiers_ni_secrets(tmp_path, monkeypatch):
    # Un contenu web malveillant ne doit pouvoir lire ni .git/config ni les secrets Telegram
    commande = agent.commande_claude("consigne")
    interdits = commande[commande.index("--disallowedTools") + 1:]
    assert {"Read", "Glob", "Grep", "Edit", "Write"} <= set(interdits)
    fichiers(tmp_path, monkeypatch)
    monkeypatch.setenv("TELEGRAM_TOKEN", "123:SECRET")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "jeton")
    appels = []
    monkeypatch.setattr(agent.subprocess, "run",
                        lambda *a, **k: appels.append(k) or subprocess.CompletedProcess(a, 0, stdout="", stderr=""))
    agent.lancer_session("consigne", MARDI)
    assert "TELEGRAM_TOKEN" not in appels[0]["env"] and appels[0]["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "jeton"
