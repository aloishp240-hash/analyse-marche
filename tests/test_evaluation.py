import json

from court_terme.evaluation import bilan, cout_et_sources, decision_du_jour, verdicts

TEXTE = """Bilan du jour
Alpha Inc. (AAA) : ✅ validé — **Technique** : repli propre
Beta Corp (BBB) : ❌ rejeté — prévisions abaissées hier soir, voir https://piege.example/x
Gamma (CCC) : rien à signaler"""


def test_verdicts_lus_dans_le_compte_rendu():
    v = verdicts(TEXTE)
    assert v["AAA"]["verdict"] == "valide" and v["BBB"]["verdict"] == "rejete"
    assert "CCC" not in v and "piege.example" not in v["BBB"]["motif"]


def test_cout_et_sources():
    sortie = "\n".join(json.dumps(e) for e in (
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "WebSearch", "input": {"query": "BBB guidance"}},
            {"type": "tool_use", "name": "WebFetch", "input": {"url": "https://exemple.com/a"}}]}},
        {"type": "result", "num_turns": 5, "duration_ms": 30000, "total_cost_usd": 0.12}))
    cout, sources = cout_et_sources(sortie)
    assert (cout["tours"], cout["duree_s"], cout["cout_usd"], cout["WebSearch"], cout["WebFetch"]) == (5, 30, 0.12, 1, 1)
    assert [s["source"] for s in sources] == ["BBB guidance", "https://exemple.com/a"]


def decision(reussie=True):
    attente = {"etat": {"ordres": [{"ticker": "AAA", "id": "id-aaa", "date_signal": "2026-10-20"},
                                   {"ticker": "ZZZ", "id": "ancien", "date_signal": "2026-10-19"}]},
               "refus": [{"ticker": "BBB", "outil": "signal", "motif": "plafond"}]}
    candidats = [{"ticker": t, "entree": 10} for t in ("AAA", "BBB", "CCC")]
    return decision_du_jour("2026-10-20", candidats, attente, TEXTE, "", reussie)


def test_la_decision_reelle_est_celle_du_code():
    d = decision()
    assert [(c["ticker"], c["avis_claude"], c["ordre_cree"]) for c in d["candidats"]] == [
        ("AAA", "valide", True), ("BBB", "rejete", False), ("CCC", None, False)]
    assert d["candidats"][1]["refus_code"] == ["plafond"]
    assert d["incoherences"] == ["CCC : aucun avis de Claude dans le compte rendu"]
    # Session échouée : ses signaux ne sont pas appliqués, aucun ordre ne compte
    assert not any(c["ordre_cree"] for c in decision(reussie=False)["candidats"])


def test_bilan_compare_avec_la_strategie_sans_ia():
    rejoues = {"2026-10-20:AAA": {"statut": "ferme", "motif": "objectif", "resultat_r": 1.8},
               "2026-10-20:BBB": {"statut": "ferme", "motif": "objectif", "resultat_r": 1.9},
               "2026-10-20:CCC": {"statut": "non_execute"}}
    fermees = [{"id": "id-aaa", "resultat_r": 1.7}]
    b = bilan([decision(), decision(reussie=False)], rejoues, fermees, 30)
    assert b["sessions"] == 2 and b["groupes"]["tous"]["candidats"] == 3
    assert b["groupes"]["ordres_crees"]["r_moyen"] == 1.8 and b["groupes"]["rejetes"]["r_moyen"] == 1.9
    assert b["apport_r_par_trade"] == round(1.8 - 1.85, 2)
    assert b["resultats_reels"] == {"trades": 1, "r_moyen": 1.7}
    assert any("BBB" in a and "objectif" in a for a in b["a_relire"]) and b["echantillon_faible"]
