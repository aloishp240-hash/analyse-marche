import json
from datetime import date

import agent
from court_terme.audit import message_audit, points_nouveaux, rapport

JOUR = "2026-10-20"
CANDIDATS = {"jour": JOUR, "donnees_au": JOUR, "entrees_bloquees": [], "titres_ecartes": {}}


def ordre(ticker, id_, **autres):
    return {"ticker": ticker, "id": id_, "etat": "en_attente", "entree": 10, "stop": 9, "objectif": 12, "quantite": 5,
            "date_signal": JOUR, "traite_jusqu_au": JOUR,
            "historique": [{"jour": JOUR, "evenement": "candidat"}, {"jour": JOUR, "evenement": "en_attente"}], **autres}


def etat(**autres):
    return {"capital_initial": 3000, "ordres": [], "positions": [], "fermees": [], "ordres_clos": [], **autres}


def test_journee_sans_rien_d_anormal():
    texte, points = rapport(JOUR, etat(), CANDIDATS, None, [], 0)
    assert points == [] and "Rien d'anormal." in texte and "à jour" in texte
    assert "Décisions de Claude" not in texte  # rubrique vide omise


def test_donnees_perimees_et_erreurs():
    candidats = {**CANDIDATS, "donnees_au": "2026-10-19", "entrees_bloquees": ["dernière séance le 2026-10-19"],
                 "titres_ecartes": {"BBB": "cours du jour absent"}}
    anomalies = [{"quand": "2026-10-20T16:50", "source": "agent.py", "texte": "BBB écarté"},
                 {"quand": "2026-10-20T16:55", "source": "agent.py", "texte": "BBB écarté"},
                 {"quand": "2026-10-10T10:00", "source": "positions.py", "texte": "vieux"}]
    texte, _ = rapport(JOUR, etat(), candidats, None, anomalies, 1)
    assert "PÉRIMÉS" in texte and "Rien d'anormal" not in texte and "BBB (cours du jour absent)" in texte
    assert texte.count("agent.py : BBB écarté") == 1 and "vieux" not in texte
    assert "session Claude échouée 1 fois" in texte


def test_ecart_entre_la_session_et_l_etat_enregistre():
    decision = {"session_reussie": True, "candidats": [
        {"ticker": "AAA", "avis_claude": "valide", "ordre_cree": True, "id_ordre": "id-aaa", "refus_code": []},
        {"ticker": "BBB", "avis_claude": "rejete", "motif_claude": "prévisions abaissées", "ordre_cree": False,
         "id_ordre": None, "refus_code": []}],
        "incoherences": [], "refus_hors_candidats": [], "fermetures": 0,
        "cout": {"tours": 6, "duree_s": 90, "WebSearch": 2, "WebFetch": 1},
        "sources": [{"outil": "WebSearch", "source": "BBB guidance"}]}
    # L'ordre AAA décidé n'a pas été enregistré ; un ordre CCC du jour existe sans décision
    texte, points = rapport(JOUR, etat(ordres=[ordre("CCC", "id-ccc")]), CANDIDATS, decision, [], 0)
    assert any("id-aaa" in p and "absent" in p for p in points)
    assert any("id-ccc" in p and "sans décision" in p for p in points)
    assert "BBB : rejeté par Claude — prévisions abaissées" in texte and "Source (WebSearch) : BBB guidance" in texte
    assert "CCC : ordre enregistré" in texte and "candidat" not in texte.split("## Événements")[1].split("##")[0]


def test_suivi_en_retard_suspension_et_signaux_clos():
    clos = {**ordre("DDD", "id-ddd", date_signal="2026-10-15"), "etat": "annule",
            "historique": [{"jour": JOUR, "evenement": "annule", "motif": "clôture sous la moyenne mobile 50 jours"}]}
    retard = ordre("EEE", "id-eee", date_signal="2026-10-16", traite_jusqu_au="2026-10-16",
                   date_resultats="2026-10-23")
    e = etat(ordres=[retard], ordres_clos=[clos], suspension={"depuis": "2026-10-19", "motif": "baisse"})
    texte, points = rapport(JOUR, e, CANDIDATS, None, [], 0)
    assert any("EEE suivi jusqu'au 2026-10-16" in p for p in points)
    assert any("suspendues" in p for p in points)
    assert "DDD : annulé — clôture sous la moyenne mobile 50 jours" in texte
    assert "EEE : résultats annoncés le 2026-10-23" in texte


def test_points_deja_signales_pas_renvoyes():
    texte, points = rapport(JOUR, etat(suspension={"depuis": JOUR, "motif": "baisse"}), CANDIDATS, None, [], 0)
    assert points_nouveaux(points, texte) == [] and points_nouveaux(points, "") == points
    assert "1 point(s) à vérifier" in message_audit(JOUR, points)


def test_agent_ecrit_l_audit_et_previent_une_seule_fois(tmp_path, monkeypatch):
    envois = []
    for nom, chemin in (("ETAT", tmp_path / "positions.json"), ("CANDIDATS", tmp_path / "candidats.json"),
                        ("DECISIONS", tmp_path / "decisions"), ("AUDIT", tmp_path / "audit"),
                        ("ANOMALIES", tmp_path / "anomalies.json"), ("SESSIONS", tmp_path / "sessions")):
        monkeypatch.setattr(agent, nom, chemin)
    monkeypatch.setattr(agent, "deposer", envois.append)
    (tmp_path / "positions.json").write_text(json.dumps(etat(suspension={"depuis": JOUR, "motif": "baisse"})))
    (tmp_path / "candidats.json").write_text(json.dumps(CANDIDATS))
    agent.ecrire_audit(date(2026, 10, 20))
    agent.ecrire_audit(date(2026, 10, 20))
    assert (tmp_path / "audit" / f"{JOUR}.md").exists()
    assert len(envois) == 1 and "AUDIT" in envois[0]
