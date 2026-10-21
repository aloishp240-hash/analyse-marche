from datetime import date

import numpy as np
import pandas as pd

from commun import charger_config
from court_terme import momentum as m

p = charger_config()["momentum"]


def test_mois_attendu_et_doit_decider():
    assert m.mois_attendu(date(2026, 10, 30)) == "2026-10"       # vendredi, dernier jour ouvré d'octobre
    assert m.mois_attendu(date(2026, 10, 29)) == "2026-09"
    assert m.mois_attendu(date(2027, 5, 28)) == "2027-04"        # le lundi 31 mai est ouvré (férié non connu)
    etat = m.nouvel_etat(p)
    assert not m.doit_decider(etat, date(2026, 10, 21))           # démarrage : on attend la fin du mois
    assert m.doit_decider(etat, date(2026, 10, 30))
    etat["derniere_decision"] = "2026-10"
    assert not m.doit_decider(etat, date(2026, 11, 2))
    assert m.doit_decider(etat, date(2026, 11, 30))
    etat["derniere_decision"] = "2026-09"                          # fin octobre manquée : rattrapage
    assert m.doit_decider(etat, date(2026, 11, 2))


def clotures(n=260):
    jours = pd.bdate_range("2025-01-01", periods=n)
    croissance = {"AAA": 0.004, "BBB": 0.002, "CCC": -0.001, "DDD": 0.003, "EEE": 0.001, "FFF": 0.0005, "GGG": 0.01}
    df = pd.DataFrame({t: 100 * np.exp(g * np.arange(n)) for t, g in croissance.items()}, index=jours)
    return df


def test_classement():
    df = clotures()
    df.loc[df.index[-10:], "BBB"] *= 3        # flambée du dernier mois : ignorée (12 mois hors dernier mois)
    df.loc[df.index[10:15], "GGG"] = np.nan    # trou dans l'historique : exclu
    indice = pd.Series(np.linspace(100, 150, 260), index=df.index)
    porteur, meilleurs = m.classement(df, indice, p)
    assert porteur
    assert [t for t, _ in meilleurs] == ["AAA", "DDD", "BBB", "EEE", "FFF"]
    assert meilleurs[0][1] == round(100 * (np.exp(0.004 * 231) - 1), 1)
    porteur, _ = m.classement(df, pd.Series(np.linspace(150, 100, 260), index=df.index), p)
    assert not porteur
    assert m.classement(df.iloc[-200:], indice, p)[1] == []       # moins de 12 mois d'historique


def etat_investi():
    etat = m.nouvel_etat(p)
    etat["liquidites_eur"] = 100.0
    etat["lignes"] = [{"ticker": "AAA", "nom": "Aaa", "quantite": 10, "prix_entree": 100, "date_entree": "2026-10-01",
                       "cout_eur": 900}, {"ticker": "BBB", "nom": "BBB", "quantite": 5, "prix_entree": 50,
                                          "date_entree": "2026-10-01", "cout_eur": 230}]
    return etat


def test_decider():
    etat = etat_investi()
    d = m.decider(etat, True, [("AAA", 50.0), ("CCC", 40.0)], date(2026, 10, 30), {"CCC": "Ccc"})
    assert (d["ventes"], d["achats"], d["conserves"]) == (["BBB"], ["CCC"], ["AAA"])
    assert etat["decision"] is d and etat["derniere_decision"] == "2026-10"
    d = m.decider(etat, False, [("AAA", 50.0)], date(2026, 11, 30), {})
    assert d["ventes"] == ["AAA", "BBB"] and d["achats"] == []
    etat = m.nouvel_etat(p)
    m.decider(etat, False, [("AAA", 50.0)], date(2026, 10, 30), {})
    assert etat["decision"] is None                               # rien à faire : pas d'ordre en attente


def test_executer_avec_frais():
    etat = etat_investi()
    m.decider(etat, True, [("AAA", 50.0), ("CCC", 40.0), ("DDD", 30.0)], date(2026, 10, 30), {})
    ops = m.executer(etat, {"BBB": 60.0, "CCC": 20.0}, 1.2, date(2026, 11, 2), {"CCC": "Ccc"}, p)
    vente, achat = ops
    # BBB : 5 × 60 $ / 1,2 = 250 € ; frais 1 + 0,25 = 1,25 € ; coût 230 €
    assert vente["montant_eur"] == 250 and vente["frais_eur"] == 1.25 and vente["resultat_eur"] == 18.75
    # DDD sans ouverture : abandonné ; CCC reçoit tout (100 + 248,75 €), frais 1 + 0,35 €
    assert achat["ticker"] == "CCC" and achat["montant_eur"] == 348.75 and achat["frais_eur"] == 1.35
    assert achat["quantite"] == 20.8440                            # (348,75 − 1,35) × 1,2 / 20
    assert etat["liquidites_eur"] == 0 and etat["decision"] is None and etat["debut"] == "2026-11-02"
    assert [l["ticker"] for l in etat["lignes"]] == ["AAA", "CCC"]
    assert round(m.valeur(etat, {"AAA": 120, "CCC": 24}, 1.2), 2) == round((1200 + 20.844 * 24) / 1.2, 2)


def test_repartition_egale():
    etat = m.nouvel_etat(p)
    m.decider(etat, True, [("AAA", 1.0), ("BBB", 1.0), ("CCC", 1.0)], date(2026, 10, 30), {})
    m.executer(etat, {"AAA": 10.0, "BBB": 10.0, "CCC": 10.0}, 1.0, date(2026, 11, 2), {}, p)
    assert [o["montant_eur"] for o in etat["operations"]] == [1000, 1000, 1000]
    assert etat["liquidites_eur"] == 0


def test_messages():
    etat = etat_investi()
    d = m.decider(etat, True, [("AAA", 50.0), ("CCC", 40.0)], date(2026, 10, 30), {"AAA": "Aaa", "CCC": "Ccc"})
    texte = m.message_decision(d, etat, {"AAA": 110, "BBB": 60}, 1.0, {"AAA": "Aaa", "CCC": "Ccc"})
    assert texte.startswith("🔄 ROTATION MOMENTUM — fin octobre 2026\n")
    assert "À vendre à la prochaine ouverture : BBB" in texte
    assert "À acheter à la prochaine ouverture (≈ 400 € chacun) : Ccc (CCC) +40 %" in texte   # 1500 − 1100 gardés
    assert "Conservés : Aaa (AAA) +50 %" in texte
    assert "Portefeuille : 1500 € (-50.0 %)" in texte
    rattrapage = m.decider(etat, False, [], date(2026, 11, 2), {})
    assert "fin octobre 2026 (rattrapage)" in m.message_decision(rattrapage, etat, {}, 1.0, {})
    ops = m.executer(etat, {"AAA": 100.0, "BBB": 50.0}, 1.0, date(2026, 11, 3), {}, p)
    texte = m.message_execution(ops, etat)
    assert "🔴 Vente Aaa (AAA) : 10 à 100.00 $" in texte and "Frais :" in texte
