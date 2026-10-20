from datetime import date

import pytest

from commun import charger_config
from court_terme.portefeuille import (ajouter_ordre, capital_courant, charger, quantite, sauver,
                                      verifier_ajout)

p = charger_config()["court_terme"]
CANDIDAT = {"ticker": "AAA", "configuration": "A", "cloture": 100, "atr": 2, "rsi": 40,
            "force_relative": 8, "entree": 99.5, "stop": 96.5, "objectif": 105.5}


def etat_neuf():
    return {"capital_initial": 3000, "debut": None, "ordres": [], "positions": [], "fermees": []}


def test_quantite_risque_et_plafond():
    assert quantite(99.5, 96.5, 3000, 1.0, p) == 6.03    # plafond 600 $ < risque (10 actions)
    assert quantite(200, 190, 3000, 1.0, p) == 3
    assert quantite(5000, 4800, 3000, 1.0, p) == 0.12    # fraction d'un titre cher
    assert quantite(10 ** 6, 9 * 10 ** 5, 3000, 1.0, p) == 0  # moins d'un centième d'action


def test_quantite_convertit_les_euros_en_dollars():
    assert quantite(100, 90, 3000, 1.5, p) == 4.5     # risque 45 $ / 10 $ = 4,5


def test_capital_courant():
    etat = {**etat_neuf(), "fermees": [{"resultat_eur": 25}, {"resultat_eur": -30}]}
    assert capital_courant(etat) == 2995


def test_refus():
    etat = etat_neuf()
    autre = {"ticker": "BBB", "industrie": "Semis", "quantite": 1, "entree": 10}
    assert verifier_ajout(etat, "AAA", "Logiciels", 0, 99.5, 1.0, p)                      # quantité 0
    assert verifier_ajout({**etat, "positions": [{**autre, "ticker": "AAA"}]}, "AAA", "X", 1, 10, 1.0, p)
    plein = {**etat, "ordres": [{**autre, "ticker": f"T{i}", "industrie": f"I{i}"} for i in range(5)]}
    assert verifier_ajout(plein, "AAA", "Logiciels", 1, 10, 1.0, p)
    deux = {**etat, "ordres": [autre, {**autre, "ticker": "CCC"}]}
    assert verifier_ajout(deux, "AAA", "Semis", 1, 10, 1.0, p)
    assert verifier_ajout(deux, "AAA", None, 1, 10, 1.0, p) is None                       # industrie inconnue
    engage = {**etat, "positions": [{**autre, "quantite": 290, "entree": 10}]}             # 2 900 € engagés
    assert verifier_ajout(engage, "AAA", "Logiciels", 2, 99.5, 1.0, p)
    assert verifier_ajout(etat, "AAA", "Logiciels", 6, 99.5, 1.0, p) is None
    vendue = {**etat, "positions": [{**autre, "quantite": 290, "entree": 10, "sortie_demandee": "2026-10-20"}]}
    assert verifier_ajout(vendue, "AAA", "Logiciels", 2, 99.5, 1.0, p) is None           # capital libéré


def test_ajouter_ordre():
    etat = etat_neuf()
    ordre = ajouter_ordre(etat, CANDIDAT, "Logiciels", "Tendance intacte.", date(2026, 10, 19), 1.0, p)
    assert ordre["quantite"] == 6.03 and ordre["industrie"] == "Logiciels"
    assert ordre["date_signal"] == ordre["traite_jusqu_au"] == "2026-10-19" and ordre["seances"] == 0
    assert etat["ordres"] == [ordre] and etat["debut"] == "2026-10-19"
    with pytest.raises(ValueError):
        ajouter_ordre(etat, CANDIDAT, "Logiciels", "Doublon.", date(2026, 10, 19), 1.0, p)


def test_charger_sauver(tmp_path):
    chemin = tmp_path / "positions.json"
    assert charger(chemin, p) == etat_neuf()
    etat = etat_neuf()
    ajouter_ordre(etat, CANDIDAT, "Logiciels", "Tendance intacte.", date(2026, 10, 19), 1.0, p)
    sauver(etat, chemin)
    assert charger(chemin, p) == etat
