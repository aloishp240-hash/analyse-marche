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
                        ("EN_ATTENTE", tmp_path / "en-attente.json"), ("DECISIONS", tmp_path / "decisions"),
                        ("CANDIDATS", tmp_path / "candidats-du-jour.json"), ("AUDIT", tmp_path / "audit"),
                        ("ANOMALIES", tmp_path / "anomalies.json")):
        monkeypatch.setattr(agent, nom, chemin)
    monkeypatch.setattr(agent, "deposer", envois.append)
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


def test_session_laisse_une_trace_des_decisions(tmp_path, monkeypatch):
    fichiers(tmp_path, monkeypatch)
    agent.CANDIDATS.write_text(json.dumps({"jour": "2026-10-20", "candidats": [
        {"ticker": "AAA", "entree": 99.5}, {"ticker": "BBB", "entree": 50}]}))

    def run(*a, **k):
        etat = {"capital_initial": 3000, "ordres": [{"ticker": "AAA", "id": "x", "date_signal": "2026-10-20"}],
                "positions": [], "fermees": []}
        agent.EN_ATTENTE.write_text(json.dumps({"etat": etat, "messages": [],
                                                "refus": [{"ticker": "CCC", "outil": "signal", "motif": "hors liste"}]}))
        texte = "Alpha (AAA) : ✅ validé — ok\nBeta (BBB) : ❌ rejeté — mauvaise nouvelle"
        return subprocess.CompletedProcess(a, 0, stdout=flux({"type": "result", "result": texte, "num_turns": 7,
                                                               "duration_ms": 90000, "total_cost_usd": 0.4}), stderr="")
    monkeypatch.setattr(agent.subprocess, "run", run)
    agent.lancer_session("consigne", MARDI)
    trace = json.loads((tmp_path / "decisions" / "2026-10-20.json").read_text())
    assert [(c["ticker"], c["avis_claude"], c["ordre_cree"]) for c in trace["candidats"]] == [
        ("AAA", "valide", True), ("BBB", "rejete", False)]
    assert trace["incoherences"] == [] and trace["refus_hors_candidats"][0]["ticker"] == "CCC"
    assert trace["cout"]["tours"] == 7 and trace["cout"]["cout_usd"] == 0.4


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


def flux(*evenements):
    return "\n".join(json.dumps(e, ensure_ascii=False) for e in evenements)


FLUX = flux(
    {"type": "system", "subtype": "init"},
    {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t0", "name": "ToolSearch", "input": {"query": "select:WebSearch"}},
        {"type": "text", "text": "Je regarde les actualités de AAA."},
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "python outils/actualites.py AAA"}}]}},
    {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t0", "content": [{"type": "tool_reference"}]},
        {"type": "tool_result", "tool_use_id": "t1", "content": "Aucune mauvaise nouvelle. " * 100}]}},
    {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t2", "name": "WebSearch", "input": {"query": "AAA guidance"}}]}},
    {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t2", "content": [{"type": "text", "text": "refusé"}], "is_error": True}]}},
    {"type": "result", "result": "AAA : ✅ validé — ok", "num_turns": 4, "duration_ms": 61500})


def test_lire_flux():
    final, etapes, bilan = agent.lire_flux("bruit\n" + FLUX)
    assert final == "AAA : ✅ validé — ok" and bilan == "4 tours, 62 s"
    assert [e.get("appel") or e["texte"] for e in etapes] == [
        "Je regarde les actualités de AAA.", "Commande : `python outils/actualites.py AAA`",
        "Recherche web : « AAA guidance »"]                                      # ToolSearch ignoré
    assert etapes[1]["reponse"].startswith("Aucune mauvaise nouvelle.") and etapes[2]["erreur"]
    assert agent.lire_flux("texte brut")[0] == "texte brut"


def test_session_reussie_ecrit_le_journal(tmp_path, monkeypatch):
    fichiers(tmp_path, monkeypatch)
    monkeypatch.setattr(agent.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=FLUX, stderr=""))
    agent.lancer_session("consigne", MARDI)
    assert (tmp_path / "2026-10-20.md").read_text() == "# Session du 2026-10-20\n\nAAA : ✅ validé — ok\n"
    journal = (tmp_path / "2026-10-20-journal.md").read_text()
    assert "## 1. Commande : `python outils/actualites.py AAA`" in journal and "## 2. Recherche web" in journal
    assert "> 💬 Je regarde" in journal and "Erreur :" in journal and " […]" in journal
    assert "échouée" not in journal


def test_session_echouee_garde_le_journal(tmp_path, monkeypatch):
    fichiers(tmp_path, monkeypatch)
    monkeypatch.setattr(agent.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout=FLUX, stderr=""))
    agent.lancer_session("consigne", MARDI)
    assert "Session échouée" in (tmp_path / "2026-10-20-journal.md").read_text()
    assert not agent.session_faite(MARDI)
