import copy
from datetime import date, timedelta

from analyse_trades import analyser
from commun import charger_config
from court_terme.analyse import VARIANTES, mesures_trade, rejouer, synthese
from court_terme.etats import EN_ATTENTE
from court_terme.preglages import parametres
from court_terme.simulation import avancer

p = parametres(charger_config(), "amelioree")
J0 = date(2026, 10, 19)


def ordre():
    return {"id": "2026-10-19-AAA-abcdef", "ticker": "AAA", "configuration": "A", "entree": 99.5, "stop": 96.5,
            "objectif": 105.5, "atr": 2, "quantite": 6, "industrie": "Logiciels", "date_signal": J0.isoformat(),
            "traite_jusqu_au": J0.isoformat(), "seances": 0, "etat": EN_ATTENTE, "historique": [],
            "date_resultats": "2026-11-20"}


def barre(n, ouv, haut, bas, clo):
    return {"jour": J0 + timedelta(days=n), "ouverture": ouv, "haut": haut, "bas": bas, "cloture": clo,
            "terminee": True}


# Achat le 1er jour, stop remonté le 2e, objectif touché le 3e, puis rechute
BARRES = [barre(1, 101, 102, 99, 100), barre(2, 100, 103, 99.8, 102.5), barre(3, 103, 106, 102.8, 105),
          barre(4, 104, 104.5, 99, 100)]


def trade_reel(barres=BARRES):
    etat = {"capital_initial": 3000, "ordres": [ordre()], "positions": [], "fermees": [], "ordres_clos": []}
    for b in barres:
        avancer(etat, {"AAA": [b]}, 1.0, p)
    return etat["fermees"][0]


def test_la_variante_officielle_retrouve_le_resultat_reel():
    reel = trade_reel()
    assert reel["motif"] == "objectif"
    rejoue = rejouer(reel, BARRES, {}, p)
    assert rejoue["statut"] == "ferme"
    assert (rejoue["motif"], rejoue["resultat_eur"]) == (reel["motif"], reel["resultat_eur"])


def test_sans_objectif_garde_la_position_jusqu_au_stop():
    rejoue = rejouer(trade_reel(), BARRES, {}, p, "sans_objectif")
    assert rejoue["motif"] == "stop" and rejoue["date_sortie"] == (J0 + timedelta(days=4)).isoformat()
    assert rejoue["resultat_eur"] < trade_reel()["resultat_eur"]


def test_entree_au_marche_achete_meme_sans_toucher_la_limite():
    hautes = [barre(1, 101, 103, 100.5, 102), barre(2, 102, 104, 101.5, 103), barre(3, 103, 108, 102.5, 107)]
    reel = {**trade_reel(), "date_signal": J0.isoformat()}
    assert rejouer(reel, hautes, {}, p)["statut"] == "non_execute"
    marche = rejouer(reel, hautes, {}, p, "entree_au_marche")
    assert marche["prix_entree"] == 101 and marche["motif"] == "objectif"


def test_les_variantes_ne_touchent_pas_au_trade_officiel():
    reel = trade_reel()
    avant = copy.deepcopy(reel)
    analyses = analyser([reel], {"AAA": BARRES}, {}, p)
    assert reel == avant
    assert set(analyses[0]["variantes"]) == set(VARIANTES)
    assert all(v["statut"] == "ferme" for v in analyses[0]["variantes"].values())


def test_fiche_du_trade():
    fiche = mesures_trade(trade_reel())
    assert fiche["entree_prevue"] == 99.5 and fiche["entree_executee"] == 99.5 and fiche["ecart_entree_pct"] == 0
    assert fiche["stop_initial"] == 96.5 and fiche["motif"] == "objectif"
    assert fiche["connu_avant"] == {"date_resultats": "2026-11-20"}
    assert [h["evenement"] for h in fiche["pendant"]] == ["stop_remonte"]
    assert fiche["mfe_r"] > 0 and fiche["mae_r"] <= 0 and fiche["duree_seances"] == 3


def test_un_split_n_est_pas_rejoue():
    assert rejouer({**trade_reel(), "splits": ["2026-10-21"]}, BARRES, {}, p)["statut"] == "non_rejoue"


def test_synthese_signale_le_petit_echantillon():
    resume = synthese(analyser([trade_reel()], {"AAA": BARRES}, {}, p))
    assert resume["officielle"]["ecart_r_moyen"] == 0
    assert resume["sans_objectif"]["ecart_r_moyen"] < 0
    assert all(r["echantillon_faible"] for r in resume.values())
