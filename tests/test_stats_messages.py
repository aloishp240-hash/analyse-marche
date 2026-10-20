from court_terme.messages import message_evenement, message_signal
from court_terme.stats import bilan_hebdo, statistiques


def fermees(*resultats):
    return [{"ticker": f"T{i}", "resultat_eur": r} for i, r in enumerate(resultats)]


def test_statistiques():
    s = statistiques(fermees(30, -15, 20, -10))
    assert s == {"nombre": 4, "taux_reussite": 50, "gain_moyen": 25, "perte_moyenne": -12.5,
                 "esperance": 6.25, "resultat_total": 25}


def test_statistiques_vide():
    s = statistiques([])
    assert s["nombre"] == 0 and s["esperance"] is None and s["taux_reussite"] is None


def test_statistiques_sans_perte():
    assert statistiques(fermees(10))["perte_moyenne"] is None


def test_bilan_hebdo():
    etat = {"capital_initial": 3000, "debut": "2026-10-01", "ordres": [], "fermees": fermees(30, -15),
            "positions": [{"ticker": "AAA", "prix_entree": 100, "quantite": 5}]}
    texte = bilan_hebdo(etat, 1.2, 3.4, 1.0, {"AAA": 110})
    assert "3065.00" in texte                       # 3 015 € réalisés + 50 € latents
    assert "+2.17" in texte and "+3.40" in texte and "+1.20" in texte
    assert "50" in texte and "7.50" in texte          # taux de réussite, espérance


def test_message_signal():
    ordre = {"ticker": "AAA", "configuration": "A", "entree": 99.5, "stop": 96.5, "objectif": 105.5,
             "quantite": 6, "justification": "Tendance intacte, rien d'inquiétant."}
    texte = message_signal(ordre, 1.0)
    for attendu in ("AAA", "repli en tendance", "99.50", "96.50", "105.50", "Quantité 6", "18.00 €",
                    "Tendance intacte"):
        assert attendu in texte


def test_message_evenement():
    for type_ in ("entree", "expire", "stop", "objectif", "temps", "stop_remonte"):
        texte = message_evenement({"type": type_, "ticker": "AAA", "jour": "2026-10-20", "prix": 99.5,
                                   "resultat_eur": -18.0})
        assert "AAA" in texte and "99.50" in texte
