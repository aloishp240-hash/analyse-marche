import json
from datetime import date

import numpy as np
import pandas as pd
import pytest

import agent
from court_terme.etats import anomalies, passer, TransitionInterdite
from court_terme.securite import (baisse_realisee_pct, controle_donnees, controler_baisse, controler_etat,
                                  filtrer_doublons, lever_suspension, noter_anomalie, suivi_en_retard, verrou)

JOUR = date(2026, 10, 20)
TICKERS = [f"T{i}" for i in range(20)]


def historique(jours=("2026-10-19", "2026-10-20"), tickers=TICKERS + ["QQQ"]):
    index = pd.DatetimeIndex(jours)
    colonnes = {(c, t): [100.0] * len(index) for t in tickers for c in ("Close", "High", "Low")}
    for t in tickers:
        colonnes[("High", t)] = [101.0] * len(index)
        colonnes[("Low", t)] = [99.0] * len(index)
    return pd.DataFrame(colonnes, index=index)


def test_donnees_saines():
    assert controle_donnees(historique(), TICKERS, JOUR, "QQQ") == ([], {})


def test_donnees_perimees_bloquent():
    bloquants, _ = controle_donnees(historique(("2026-10-16", "2026-10-19")), TICKERS, JOUR, "QQQ")
    assert any("pas le 2026-10-20" in m for m in bloquants)


def test_titres_incoherents_ecartes():
    hist = historique()
    hist.loc["2026-10-20", ("Close", "T0")] = np.nan
    hist.loc["2026-10-20", ("Close", "T1")] = 120.0          # clôture au-dessus du plus haut
    hist.loc["2026-10-20", ("Low", "T2")] = 0.0
    bloquants, ecartes = controle_donnees(hist, TICKERS, JOUR, "QQQ")
    assert set(ecartes) == {"T0", "T1", "T2"} and bloquants == ["3 titres sur 20 sans cours du jour valides"]
    hist.loc["2026-10-20", ("Close", "QQQ")] = np.nan
    bloquants, _ = controle_donnees(hist, TICKERS, JOUR, "QQQ")
    assert any(m.startswith("QQQ") for m in bloquants)


def test_trop_de_titres_invalides_bloquent():
    hist = historique()
    for t in TICKERS[:3]:
        hist.loc["2026-10-20", ("Close", t)] = np.nan
    bloquants, ecartes = controle_donnees(hist, TICKERS, JOUR, "QQQ")
    assert len(ecartes) == 3 and any("3 titres sur 20" in m for m in bloquants)
    _, ecartes = controle_donnees(historique(tickers=TICKERS[1:] + ["QQQ"]), TICKERS, JOUR, "QQQ")
    assert ecartes == {"T0": "cours du jour absent"}


def ordre(ticker="AAA", **champs):
    return {"id": f"x-{ticker}", "ticker": ticker, "etat": "en_attente", "quantite": 1, "entree": 100,
            "stop": 97, "objectif": 106, "date_signal": "2026-10-16", "traite_jusqu_au": "2026-10-19", **champs}


def etat_avec(**champs):
    return {"capital_initial": 3000, "ordres": [], "positions": [], "fermees": [], "ordres_clos": [], **champs}


def test_suivi_en_retard():
    etat = etat_avec(ordres=[ordre(), ordre("BBB", traite_jusqu_au="2026-10-20"),
                             ordre("CCC", date_signal="2026-10-20", traite_jusqu_au="2026-10-20")])
    assert suivi_en_retard(etat, JOUR) == ["AAA suivi jusqu'au 2026-10-19 seulement"]


def test_suspension_apres_baisse_puis_levee():
    etat = etat_avec(fermees=[{"resultat_eur": 300}, {"resultat_eur": -200}, {"resultat_eur": -150}])
    assert baisse_realisee_pct(etat) == pytest.approx(-10.61, abs=0.01)
    assert not controler_baisse(etat, {"suspension_baisse_pct": None}, JOUR)        # préréglage reference
    assert controler_baisse(etat, {"suspension_baisse_pct": 10}, JOUR)
    assert etat["suspension"]["depuis"] == "2026-10-20"
    assert not controler_baisse(etat, {"suspension_baisse_pct": 10}, JOUR)          # déjà suspendu
    lever_suspension(etat, JOUR)
    assert "suspension" not in etat and baisse_realisee_pct(etat) == 0
    assert not controler_baisse(etat, {"suspension_baisse_pct": 10}, JOUR)


def test_etat_incoherent_suspend():
    etat = etat_avec(ordres=[ordre(), ordre(stop=110)], positions=[{**ordre("BBB"), "etat": "annule"}])
    problemes, nouvelle = controler_etat(etat, JOUR)
    assert nouvelle and etat["suspension"]
    texte = " | ".join(problemes)
    assert "présent 2 fois" in texte and "désordre" in texte and "rangé dans positions" in texte
    assert "prix manquants" in texte                         # position sans prix d'achat : signalée, pas de plantage
    assert anomalies(etat_avec(ordres=[ordre()]), JOUR) == []


def test_transitions_interdites():
    o = ordre()
    passer(o, "execute", "2026-10-20")
    with pytest.raises(TransitionInterdite):
        passer(o, "annule", "2026-10-21")                    # une position ne s'annule pas : elle se vend
    passer(o, "cloture", "2026-10-22")
    with pytest.raises(TransitionInterdite):
        passer(o, "execute", "2026-10-23")
    assert [h["evenement"] for h in o["historique"]] == ["execute", "cloture"]


def test_doublons_non_renvoyes():
    etat = etat_avec()
    evts = [{"id": "a:stop:2026-10-20"}, {"id": "b:objectif:2026-10-20"}]
    assert filtrer_doublons(etat, evts) == (evts, [])
    nouveaux, doublons = filtrer_doublons(etat, [evts[0], {"id": "c:entree:2026-10-20"}])
    assert nouveaux == [{"id": "c:entree:2026-10-20"}] and doublons == [evts[0]]


def test_journal_anomalies_sans_doublon_du_jour(tmp_path):
    chemin = tmp_path / "anomalies.json"
    noter_anomalie("agent.py", "QQQ : cours du jour absent", chemin)
    noter_anomalie("agent.py", "QQQ : cours du jour absent", chemin)
    noter_anomalie("positions.py", "autre", chemin)
    assert [a["texte"] for a in json.loads(chemin.read_text())] == ["QQQ : cours du jour absent", "autre"]


def test_verrou_empeche_deux_passages(tmp_path):
    chemin = tmp_path / ".verrou"
    with verrou(chemin):
        with pytest.raises(SystemExit):
            with verrou(chemin):
                pass
    with verrou(chemin):                                     # libéré à la sortie
        pass


def test_agent_bloque_les_entrees(monkeypatch):
    etat = etat_avec(ordres=[ordre()], suspension={"depuis": "2026-10-19", "motif": "recul"})
    motifs, ecartes = agent.entrees_bloquees(historique(), TICKERS, etat, JOUR, JOUR)
    assert ecartes == {} and len(motifs) == 2
    assert motifs[0].startswith("suivi des positions pas à jour") and "suspendues" in motifs[1]


def test_environnement_claude_sans_jetons_github(monkeypatch):
    for nom in ("GH_TOKEN", "GITHUB_TOKEN", "TELEGRAM_CHAT_ID", "ACTIONS_ID_TOKEN_REQUEST_URL"):
        monkeypatch.setenv(nom, "secret")
    monkeypatch.setenv("REDDIT_CLIENT_ID", "id")
    env = agent.environnement_claude()
    assert "secret" not in env.values() and env["REDDIT_CLIENT_ID"] == "id"
