"""Règles prudentes du préréglage amelioree : frais, exécution, jour d'achat, annulations, splits, risque, envoi."""
import json
from datetime import date

import pytest

import backtest
import envoyer
from commun import charger_config
from court_terme import boite
from court_terme.calendrier import jours_feries, seance, seances_entre
from court_terme.portefeuille import quantite, verifier_ajout
from court_terme.preglages import REFERENCE, parametres
from court_terme.simulation import ajuster_splits, annuler_avant_resultats, avancer
from court_terme.stats import mesures, statistiques
from court_terme.strategie import exclu

# Valeurs écrites ici pour que les tests ne changent pas quand on règle config.json
P = {**REFERENCE, "risque_pct": 1, "frais_fixes_eur": 1, "frais_pct": 0.1, "glissement_pct": 0.05, "marge_limite_atr": 0.05,
     "jour_entree_prudent": True, "annulation_gap_atr": 1, "annulation_sous_mm50": True,
     "annulation_resultats": True, "exclure_resultats_inconnus": True, "jours_feries": True,
     "risque_cumule_max_pct": 4, "marge_gap_atr": 0.5}


def ordre(**autres):
    return {"ticker": "X", "nom": "X", "configuration": "A", "entree": 100.0, "stop": 92.5, "objectif": 115.0,
            "atr": 5.0, "quantite": 10, "date_signal": "2026-10-01", "traite_jusqu_au": "2026-10-01", "seances": 0,
            **autres}


def position(**autres):
    return {**ordre(), "prix_entree": 100.0, "date_entree": "2026-10-02", "stop_initial": 92.5, "seances": 1,
            "traite_jusqu_au": "2026-10-02", "frais_entree_eur": 2.0, **autres}


def barre(j, o, h, b, c, terminee=True):
    return {"jour": date(2026, 10, j), "ouverture": o, "haut": h, "bas": b, "cloture": c, "terminee": terminee}


def etat(ordres=(), positions=()):
    return {"capital_initial": 3000, "debut": None, "ordres": list(ordres), "positions": list(positions),
            "fermees": []}


def types(evts):
    return [e["type"] for e in evts]


def test_config_amelioree_complete():
    p = parametres(charger_config())
    assert set(REFERENCE) <= set(p)
    assert p["frais_fixes_eur"] > 0 and p["jour_entree_prudent"] and p["risque_pct"] <= 1
    with pytest.raises(ValueError):
        parametres(charger_config(), "inconnu")


# --- Exécution de l'ordre limite


def test_limite_juste_touchee_pas_executee():
    e = etat([ordre()])
    assert types(avancer(e, {"X": [barre(2, 103, 104, 100.0, 103)]}, 1.0, P)) == []
    assert e["ordres"][0]["seances"] == 1


def test_limite_nettement_franchie_executee():
    e = etat([ordre()])
    assert types(avancer(e, {"X": [barre(2, 103, 104, 99.7, 103)]}, 1.0, P)) == ["entree"]
    pos = e["positions"][0]
    assert pos["prix_entree"] == 100.0 and pos["frais_entree_eur"] == 2.0 and pos["risque_initial_eur"] == 75.0


def test_ouverture_sous_la_limite_executee_a_l_ouverture():
    e = etat([ordre()])
    avancer(e, {"X": [barre(2, 97, 99, 96, 98)]}, 1.0, P)
    assert e["positions"][0]["prix_entree"] == 97


# --- Jour d'achat prudent


def test_pas_d_objectif_ni_de_stop_remonte_le_jour_de_l_achat():
    e = etat([ordre()])
    # Le plus haut (116, au-dessus de l'objectif et de entrée + 1,5 ATR) a pu avoir lieu avant l'achat
    assert types(avancer(e, {"X": [barre(2, 104, 116, 99, 101)]}, 1.0, P)) == ["entree"]
    assert e["positions"][0]["stop"] == 92.5 and e["positions"][0]["seances"] == 1


def test_stop_reste_possible_le_jour_de_l_achat():
    e = etat([ordre()])
    evts = avancer(e, {"X": [barre(2, 101, 102, 92, 93)]}, 1.0, P)
    assert types(evts) == ["entree", "stop"]
    assert evts[1]["prix"] == pytest.approx(92.5 * (1 - 0.0005))


def test_reference_garde_l_ancien_comportement():
    e = etat([ordre()])
    ref = parametres(charger_config(), "reference")
    assert types(avancer(e, {"X": [barre(2, 104, 116, 99, 101)]}, 1.0, ref)) == ["entree", "objectif"]


# --- Frais et glissement


def test_frais_deduits_a_l_objectif():
    e = etat(positions=[position()])
    evts = avancer(e, {"X": [barre(5, 110, 116, 109, 114)]}, 1.0, P)
    f = e["fermees"][0]
    assert types(evts) == ["objectif"] and f["prix_sortie"] == 115
    assert f["resultat_brut_eur"] == 150 and f["frais_eur"] == pytest.approx(4.15)
    assert f["resultat_eur"] == pytest.approx(145.85)


def test_gap_sous_le_stop_vendu_a_l_ouverture_avec_glissement():
    e = etat(positions=[position()])
    avancer(e, {"X": [barre(5, 85, 86, 84, 85.5)]}, 1.0, P)
    f = e["fermees"][0]
    assert f["motif"] == "stop" and f["prix_sortie"] == pytest.approx(85 * 0.9995)
    assert f["resultat_eur"] == pytest.approx((85 * 0.9995 - 100) * 10 - 2 - (1 + 85 * 0.9995 * 10 / 1000), abs=0.01)


# --- Annulations


def test_annulation_sur_gap_d_ouverture():
    e = etat([ordre()])
    evts = avancer(e, {"X": [barre(2, 93, 95, 92.6, 94)]}, 1.0, P)
    assert types(evts) == ["annule"] and "ouverture" in evts[0]["motif"] and e["ordres"] == e["positions"] == []


def test_annulation_sous_la_mm50_seulement_en_fin_de_seance():
    e = etat([ordre(mm50=98.0)])
    assert avancer(e, {"X": [barre(2, 101, 102, 100.5, 97.5, terminee=False)]}, 1.0, P) == []
    evts = avancer(e, {"X": [barre(2, 101, 102, 100.5, 97.5)]}, 1.0, P)
    assert types(evts) == ["annule"] and "moyenne mobile" in evts[0]["motif"]


def test_annulation_avant_resultats():
    e = etat([ordre(ticker="X"), ordre(ticker="Y")])
    evts = annuler_avant_resultats(e, date(2026, 10, 1), {"X": {"date": "2026-10-20"}, "Y": {"date": "2026-12-01"}}, P)
    assert [(v["type"], v["ticker"]) for v in evts] == [("annule", "X")]
    assert [o["ticker"] for o in e["ordres"]] == ["Y"]
    assert annuler_avant_resultats(etat([ordre()]), date(2026, 10, 1), {"X": {"date": "2026-10-20"}},
                                   parametres(charger_config(), "reference")) == []


def test_date_de_resultats_inconnue_exclue():
    ind = {"cloture": 100, "veille": 99.5, "atr": 2}
    assert exclu(ind, date(2026, 10, 1), None, P) == "date des prochains résultats inconnue"
    assert exclu(ind, date(2026, 10, 1), None, parametres(charger_config(), "reference")) is None


# --- Idempotence (deux passages sur la même séance)


def test_barre_partielle_puis_terminee_sans_double_comptage():
    e = etat([ordre()])
    partielle = barre(2, 103, 104, 99.5, 101, terminee=False)
    assert types(avancer(e, {"X": [partielle]}, 1.0, P)) == ["entree"]
    assert avancer(e, {"X": [partielle]}, 1.0, P) == []
    assert avancer(e, {"X": [barre(2, 103, 104, 99.5, 101.5)]}, 1.0, P) == []
    assert len(e["positions"]) == 1 and e["positions"][0]["seances"] == 1
    assert avancer(e, {"X": [barre(2, 103, 104, 99.5, 101.5)]}, 1.0, P) == []


# --- Splits


def test_split_ramene_la_position_a_l_echelle_des_cours():
    e = etat(positions=[position()])
    evts = ajuster_splits(e, {"X": [(date(2026, 10, 5), 10.0)]})
    pos = e["positions"][0]
    assert types(evts) == ["split"] and evts[0]["ratio"] == 10
    assert (pos["prix_entree"], pos["stop"], pos["objectif"], pos["quantite"]) == (10, 9.25, 11.5, 100)
    assert ajuster_splits(e, {"X": [(date(2026, 10, 5), 10.0)]}) == []           # pas deux fois
    assert avancer(e, {"X": [barre(5, 10.1, 10.3, 10.0, 10.2)]}, 1.0, P) == []   # pas de fausse vente au stop


def test_split_avant_le_signal_ignore():
    e = etat([ordre()])
    assert ajuster_splits(e, {"X": [(date(2026, 9, 1), 4.0), (date(2026, 10, 1), 2.0)]}) == []
    assert e["ordres"][0]["entree"] == 100.0


# --- Calendrier de la Bourse de New York


def test_jours_feries():
    assert date(2026, 7, 3) in jours_feries(2026)        # 4 juillet un samedi : vendredi chômé
    assert date(2026, 11, 26) in jours_feries(2026)      # Thanksgiving
    assert date(2026, 4, 3) in jours_feries(2026)        # Vendredi saint
    assert date(2021, 6, 18) not in jours_feries(2021)   # Juneteenth chômé à partir de 2022
    assert seance(date(2027, 12, 31))                    # 1er janvier 2028 un samedi : pas de jour chômé
    assert seances_entre(date(2026, 7, 2), date(2026, 7, 6)) == 1


# --- Risque


def test_quantite_compte_les_frais():
    assert quantite(100, 92.5, 3000, 1.0, P) == 3.61    # (30 € − 2 € de frais fixes) / 7,74 $ par action
    assert quantite(100, 92.5, 3000, 1.0, {**P, "frais_fixes_eur": 0, "frais_pct": 0, "glissement_pct": 0}) == 4


def test_plafond_de_risque_cumule():
    vide = etat()
    assert verifier_ajout(vide, "Y", None, 3.61, 100, 1.0, P, 92.5, 5) is None
    charge = etat(positions=[position(industrie="Autre")])   # 10 actions : environ 104 € de risque gap compris
    motif = verifier_ajout(charge, "Y", None, 3.61, 100, 1.0, P, 92.5, 5)
    assert motif and "risque cumulé" in motif


# --- Mesures


def test_mesures_et_resultat_nul():
    fermees = [{"resultat_eur": 30, "resultat_brut_eur": 32, "frais_eur": 2, "risque_initial_eur": 15},
               {"resultat_eur": -15, "resultat_brut_eur": -13, "frais_eur": 2, "risque_initial_eur": 15},
               {"resultat_eur": 0, "resultat_brut_eur": 2, "frais_eur": 2, "risque_initial_eur": 15}]
    courbe = [{"valeur": v, "qqq": q} for v, q in ((1000, 1000), (1100, 1010), (990, 1020), (1050, 1030))]
    m = mesures(fermees, courbe, 1000)
    assert statistiques(fermees)["perte_moyenne"] == -15 and m["nuls"] == 1
    assert m["facteur_profit"] == 2 and m["r_moyen"] == pytest.approx(0.33) and m["frais_total"] == 6
    assert m["baisse_max_pct"] == -10 and m["echantillon_faible"]


def test_date_de_resultats_trop_lointaine_inconnue():
    dates = {"X": [date(2026, 10, 20)], "Y": [date(2027, 3, 1)]}
    assert backtest.prochains(dates, date(2026, 10, 1)) == {"X": {"date": "2026-10-20"}}


# --- Boîte d'envoi


def test_boite_d_envoi(tmp_path, monkeypatch):
    chemin = tmp_path / "a-envoyer.json"
    monkeypatch.setattr(boite, "BOITE", chemin)
    boite.deposer("un", chemin)
    boite.deposer("deux", chemin)
    assert json.loads(chemin.read_text()) == ["un", "deux"]
    envoyes = []

    def faux_envoi(texte):
        if texte == "deux":
            raise RuntimeError("Telegram indisponible")
        envoyes.append(texte)

    monkeypatch.setattr(envoyer, "envoyer_telegram", faux_envoi)
    monkeypatch.setattr(boite.lire, "__defaults__", (chemin,))
    monkeypatch.setattr(boite.ecrire, "__defaults__", (chemin,))
    assert envoyer.main([]) == 1
    assert envoyes == ["un"] and boite.lire(chemin) == ["deux"]   # le message en échec reste dans la boîte
