from datetime import date, timedelta

from commun import charger_config
from court_terme.simulation import avancer

p = charger_config()["court_terme"]
J0 = date(2026, 10, 19)


def ordre():
    return {"ticker": "AAA", "configuration": "A", "entree": 99.5, "stop": 96.5, "objectif": 105.5, "atr": 2,
            "quantite": 6, "industrie": "Logiciels", "date_signal": J0.isoformat(),
            "traite_jusqu_au": J0.isoformat(), "seances": 0}


def position():
    return {**ordre(), "prix_entree": 99.5, "date_entree": J0.isoformat(), "stop_initial": 96.5, "seances": 1}


def etat(ordres=(), positions=()):
    return {"capital_initial": 3000, "debut": None, "ordres": list(ordres), "positions": list(positions),
            "fermees": []}


def barre(n, ouv, haut, bas, clo, terminee=True):
    return {"jour": J0 + timedelta(days=n), "ouverture": ouv, "haut": haut, "bas": bas, "cloture": clo,
            "terminee": terminee}


def calme(n):
    return barre(n, 100, 101, 99.8, 100.5)


def test_execution_a_la_limite():
    e = etat([ordre()])
    evts = avancer(e, {"AAA": [barre(1, 101, 102, 99, 100)]}, 1.0, p)
    assert [(v["type"], v["prix"]) for v in evts] == [("entree", 99.5)]
    assert e["ordres"] == [] and e["positions"][0]["prix_entree"] == 99.5
    assert e["positions"][0]["seances"] == 1 and e["positions"][0]["stop_initial"] == 96.5


def test_execution_ouverture_sous_limite():
    e = etat([ordre()])
    evts = avancer(e, {"AAA": [barre(1, 98, 100, 97.5, 99)]}, 1.0, p)
    assert [(v["type"], v["prix"]) for v in evts] == [("entree", 98)]


def test_expiration_apres_3_seances():
    e = etat([ordre()])
    evts = avancer(e, {"AAA": [calme(1), calme(2)]}, 1.0, p)
    assert evts == [] and e["ordres"][0]["seances"] == 2
    evts = avancer(e, {"AAA": [calme(1), calme(2), calme(3)]}, 1.0, p)
    assert [v["type"] for v in evts] == ["expire"] and e["ordres"] == []


def test_sortie_ouverture_sous_stop():
    e = etat(positions=[position()])
    evts = avancer(e, {"AAA": [barre(1, 95, 96, 94, 95.5)]}, 1.0, p)
    assert [(v["type"], v["prix"]) for v in evts] == [("stop", 95)]
    assert e["fermees"][0]["resultat_eur"] == round((95 - 99.5) * 6, 2) and e["positions"] == []


def test_sortie_ouverture_au_dessus_objectif():
    e = etat(positions=[position()])
    evts = avancer(e, {"AAA": [barre(1, 107, 108, 106, 107)]}, 1.0, p)
    assert [(v["type"], v["prix"]) for v in evts] == [("objectif", 107)]


def test_stop_et_objectif_meme_seance():
    e = etat(positions=[position()])
    evts = avancer(e, {"AAA": [barre(1, 100, 106, 96, 101)]}, 1.0, p)
    assert [(v["type"], v["prix"]) for v in evts] == [("stop", 96.5)]


def test_objectif():
    e = etat(positions=[position()])
    evts = avancer(e, {"AAA": [barre(1, 101, 106, 100, 105)]}, 1.25, p)
    assert [(v["type"], v["prix"]) for v in evts] == [("objectif", 105.5)]
    assert e["fermees"][0]["resultat_eur"] == round(6 * 6 / 1.25, 2)
    assert e["fermees"][0]["motif"] == "objectif" and e["fermees"][0]["prix_sortie"] == 105.5


def test_stop_remonte_jamais_redescendu():
    e = etat(positions=[position()])
    evts = avancer(e, {"AAA": [barre(1, 100, 102.6, 99.8, 102)]}, 1.0, p)
    assert [v["type"] for v in evts] == ["stop_remonte"] and e["positions"][0]["stop"] == 99.5
    evts = avancer(e, {"AAA": [calme(2)]}, 1.0, p)
    assert evts == [] and e["positions"][0]["stop"] == 99.5


def test_stop_temporel_15e_seance():
    e = etat(positions=[{**position(), "seances": 0}])
    evts = avancer(e, {"AAA": [calme(n) for n in range(1, 15)]}, 1.0, p)
    assert evts == [] and e["positions"][0]["seances"] == 14
    evts = avancer(e, {"AAA": [barre(15, 100, 101, 99.8, 100.7)]}, 1.0, p)
    assert [(v["type"], v["prix"]) for v in evts] == [("temps", 100.7)]


def test_jour_entree_compte_comme_premiere_seance():
    e = etat([ordre()])
    avancer(e, {"AAA": [barre(1, 101, 102, 99, 100)]}, 1.0, p)
    evts = avancer(e, {"AAA": [calme(n) for n in range(2, 16)]}, 1.0, p)
    assert [v["type"] for v in evts] == ["temps"] and evts[0]["jour"] == (J0 + timedelta(days=15)).isoformat()


def test_ouverture_sous_stop_le_jour_entree():
    e = etat([ordre()])
    evts = avancer(e, {"AAA": [barre(1, 95, 96, 94, 95)]}, 1.0, p)
    assert [(v["type"], v["prix"]) for v in evts] == [("entree", 95), ("stop", 95)]
    assert e["fermees"][0]["resultat_eur"] == 0


def test_idempotence():
    e = etat([ordre()])
    en_cours = barre(1, 101, 102, 99, 100, terminee=False)
    assert len(avancer(e, {"AAA": [en_cours]}, 1.0, p)) == 1
    assert avancer(e, {"AAA": [en_cours]}, 1.0, p) == []
    assert e["positions"][0]["seances"] == 0
    terminee = barre(1, 101, 102, 99, 100)
    avancer(e, {"AAA": [terminee]}, 1.0, p)
    avancer(e, {"AAA": [terminee]}, 1.0, p)
    assert e["positions"][0]["seances"] == 1


def test_ticker_sans_barre():
    e = etat([ordre()], [{**position(), "ticker": "BBB"}])
    avant = repr(e)
    assert avancer(e, {}, 1.0, p) == [] and repr(e) == avant


def test_stop_remonte_seulement_en_fin_de_seance():
    # Barre partielle : le plus bas (99) peut dater d'avant la hausse ; la rejouer ne doit pas sortir au stop remonté
    e = etat([ordre()])
    en_cours = barre(1, 101, 102.6, 99, 102, terminee=False)
    assert [v["type"] for v in avancer(e, {"AAA": [en_cours]}, 1.0, p)] == ["entree"]
    assert avancer(e, {"AAA": [en_cours]}, 1.0, p) == []
    assert e["positions"][0]["stop"] == 96.5
    evts = avancer(e, {"AAA": [barre(1, 101, 102.6, 99, 102)]}, 1.0, p)
    assert [v["type"] for v in evts] == ["stop_remonte"] and e["positions"][0]["stop"] == 99.5
