from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from inities.mesure import selectionner
from inities.modele import Operation
from inities.simulation import frais_courtier, frais_nuls, quantite, simuler

P = {"capital_initial": 3000, "part_par_achat_pct": 10, "part_max_une_action_pct": 15, "positions_max": 10,
     "stop_pct": 15, "duree_max_mois": 6, "fenetre_mois": 18, "recent_mois": 6, "politicien_achats_min": 10,
     "politicien_delai_max_j": 30, "politicien_montant_min": 15000, "dirigeant_montant_min": 100000,
     "groupe_dirigeants_jours": 30, "fonds_positions_min": 3, "capitalisation_min": 2_000_000_000,
     "volume_dollars_min": 10_000_000}
JOURS = pd.bdate_range("2030-01-01", periods=450)
ACTIONS = {"AAA": 1e8, "BBB": 1e9, "CCC": 1e8}


def _cours(ouverture=100.0, cloture=None, volume=1_000_000.0):
    """Séances de JOURS ; ouverture et cloture sont des tableaux ou des constantes, haut et bas les encadrent."""
    ouverture = np.broadcast_to(np.asarray(ouverture, dtype=float), len(JOURS)).copy()
    cloture = ouverture.copy() if cloture is None else np.broadcast_to(np.asarray(cloture, dtype=float),
                                                                       len(JOURS)).copy()
    return pd.DataFrame({"Open": ouverture, "High": np.maximum(ouverture, cloture),
                         "Low": np.minimum(ouverture, cloture), "Close": cloture, "Volume": float(volume)},
                        index=JOURS)


def _dirigeant(ticker, publie, montant=200_000.0, cik=1, sens="achat", rang=0):
    """Achat (ou vente) de dirigeant publié à la séance `publie` de JOURS."""
    pub = JOURS[publie].date()
    return Operation(famille="dirigeant", personne_id=f"dirigeant:{cik}", personne=f"Dirigeant {cik}", ticker=ticker,
                     sens=sens, date_operation=pub - timedelta(days=2), date_publication=pub, montant_min=montant,
                     montant_max=montant, source_id=f"form4:{ticker}:{cik}:{publie}:{sens}:{rang}",
                     details={"code": "P" if sens == "achat" else "S", "dirigeant": True})


def _simuler(ops, cours, debut=20, fin=100, p=None, frais=frais_nuls, qqq=None, actions=None):
    return simuler(ops, cours, _cours(400.0) if qqq is None else qqq, ACTIONS if actions is None else actions,
                   JOURS[debut].date(), JOURS[fin].date(), P if p is None else p, frais)


def _motifs(resultat, ticker):
    return [i["motif"] for i in resultat["ignores"] if i["ticker"] == ticker]


# --- quantite et frais ---

def test_quantite():
    assert quantite(3000, 50, P) == 6
    assert quantite(3000, 400, P) == 1  # 10 % ne paie pas une action, mais 400 $ <= 15 % de 3 000 $
    assert quantite(3000, 450, P) == 1
    assert quantite(3000, 500, P) == 0
    assert quantite(3000, float("nan"), P) == 0 and quantite(0, 50, P) == 0


def test_frais():
    taux = pd.Series([1.05, 1.10], index=pd.to_datetime(["2030-03-01", "2030-03-04"]))
    frais = frais_courtier(taux)
    assert frais(1000, date(2030, 3, 4)) == pytest.approx(1.10 + 1.00)
    assert frais(1000, date(2030, 3, 8)) == pytest.approx(2.10)  # dernier cours connu
    assert frais(1000, date(2030, 3, 2)) == pytest.approx(1.05 + 1.00)
    assert frais_nuls(1000, date(2030, 3, 4)) == 0.0


def test_frais_sans_eurusd_ne_leve_pas():
    assert frais_courtier(pd.Series(dtype=float))(1000, date(2030, 3, 4)) == pytest.approx(2.0)
    assert frais_courtier(None)(1000, date(2030, 3, 4)) == pytest.approx(2.0)
    avant = frais_courtier(pd.Series([1.10], index=pd.to_datetime(["2030-03-04"])))
    assert avant(1000, date(2030, 3, 1)) == pytest.approx(2.10)  # avant le premier point : le premier connu


def test_frais_appliques_a_l_entree():
    taux = pd.Series([1.10], index=pd.to_datetime(["2030-01-02"]))
    p = {**P, "capital_initial": 10_000}
    r = _simuler([_dirigeant("AAA", 30)], {"AAA": _cours()}, p=p, frais=frais_courtier(taux))
    assert r["trades"][0]["quantite"] == 10 and r["trades"][0]["frais"] == pytest.approx(2.10)
    assert r["final"] == pytest.approx(10_000 - 2.10)


def test_frais_comptes_a_l_entree_et_a_la_sortie():
    cours = _cours()
    cours.loc[JOURS[40], "Low"] = 80.0
    r = _simuler([_dirigeant("AAA", 30)], {"AAA": cours}, frais=lambda montant, jour: 1.0 + 0.001 * montant)
    t = r["trades"][0]
    assert t["frais"] == pytest.approx((1.0 + 0.3) + (1.0 + 0.001 * 255))
    assert t["resultat_usd"] == pytest.approx(-45.0 - t["frais"])
    assert r["final"] == pytest.approx(3000 + t["resultat_usd"])


# --- entrées et sorties ---

def test_entree_a_l_ouverture_de_la_premiere_seance_apres_la_publication():
    ouverture = np.full(len(JOURS), 100.0)
    ouverture[31:] = 104.0
    r = _simuler([_dirigeant("AAA", 30)], {"AAA": _cours(ouverture)})
    t = r["trades"][0]
    assert t["entree"] == JOURS[31].date() and t["prix_entree"] == 104.0 and t["personne"] == "Dirigeant 1"
    assert t["quantite"] == 3  # 10 % du compte (300 $) sur la clôture de la veille (100 $), payée 104 $ à l'ouverture
    assert set(t) >= {"ticker", "personne", "entree", "prix_entree", "sortie", "prix_sortie", "raison", "quantite",
                      "resultat_usd", "frais"}


def test_stop_a_moins_15():
    cours = _cours()
    cours.loc[JOURS[40], "Low"] = 80.0  # ouverture au-dessus du stop (85), plus bas en dessous
    t = _simuler([_dirigeant("AAA", 30)], {"AAA": cours})["trades"][0]
    assert (t["entree"], t["prix_entree"], t["quantite"]) == (JOURS[31].date(), 100.0, 3)
    assert t["sortie"] == JOURS[40].date() and t["prix_sortie"] == pytest.approx(85.0) and t["raison"] == "stop"
    assert t["resultat_usd"] == pytest.approx(-45.0)


def test_stop_touche_le_jour_de_l_entree():
    cours = _cours()
    cours.loc[JOURS[31], "Low"] = 80.0
    t = _simuler([_dirigeant("AAA", 30)], {"AAA": cours})["trades"][0]
    assert t["entree"] == t["sortie"] == JOURS[31].date() and t["prix_sortie"] == pytest.approx(85.0)


def test_ouverture_sous_le_stop():
    cours = _cours()
    cours.loc[JOURS[40]:, ["Open", "High", "Low", "Close"]] = 80.0
    t = _simuler([_dirigeant("AAA", 30)], {"AAA": cours})["trades"][0]
    assert t["sortie"] == JOURS[40].date() and t["prix_sortie"] == 80.0 and t["raison"] == "stop"
    assert t["resultat_usd"] == pytest.approx(-60.0)


def test_vente_publiee_sort_a_l_ouverture_suivante():
    ouverture = np.full(len(JOURS), 100.0)
    ouverture[41:] = 110.0
    ops = [_dirigeant("AAA", 30), _dirigeant("AAA", 40, sens="vente")]
    t = _simuler(ops, {"AAA": _cours(ouverture)})["trades"][0]
    assert t["sortie"] == JOURS[41].date() and t["prix_sortie"] == 110.0 and t["raison"] == "vente"
    assert t["resultat_usd"] == pytest.approx(30.0)


def test_vente_publiee_le_lendemain_ne_fait_pas_sortir_le_jour_meme():
    ouverture = np.full(len(JOURS), 100.0)
    ouverture[41] = 110.0  # séance de la publication de la vente : encore détenu
    ouverture[42:] = 120.0
    ops = [_dirigeant("AAA", 30), _dirigeant("AAA", 41, sens="vente")]
    t = _simuler(ops, {"AAA": _cours(ouverture)})["trades"][0]
    assert t["sortie"] == JOURS[42].date() and t["prix_sortie"] == 120.0 and t["raison"] == "vente"


def test_vente_d_une_autre_personne_ou_anterieure_a_l_entree_ne_sort_pas():
    ops = [_dirigeant("AAA", 30), _dirigeant("AAA", 40, sens="vente", cik=2), _dirigeant("AAA", 25, sens="vente")]
    t = _simuler(ops, {"AAA": _cours()})["trades"][0]
    assert t["raison"] == "ouverte"


def test_six_mois():
    entree = JOURS[31]
    echeance = JOURS[JOURS >= entree + pd.DateOffset(months=6)][0]
    ouverture = np.full(len(JOURS), 100.0)
    ouverture[JOURS.get_loc(echeance):] = 120.0
    t = _simuler([_dirigeant("AAA", 30)], {"AAA": _cours(ouverture)}, fin=250)["trades"][0]
    assert t["sortie"] == echeance.date() and t["prix_sortie"] == 120.0 and t["raison"] == "6 mois"
    assert JOURS[JOURS.get_loc(echeance) - 1] < entree + pd.DateOffset(months=6)


def test_pas_de_rachat_ni_onzieme_position():
    tickers = [f"T{k:02d}" for k in range(11)]
    ops = [_dirigeant(t, 30, montant=300_000.0 - 1000 * k, cik=k + 1) for k, t in enumerate(tickers)]
    ops.append(_dirigeant("T00", 50, cik=99))
    r = _simuler(ops, {t: _cours() for t in tickers}, actions={t: 1e8 for t in tickers})
    assert sorted(t["ticker"] for t in r["trades"]) == tickers[:10]
    assert "plafond de 10 positions" in _motifs(r, "T10")[0]
    assert "déjà détenu" in _motifs(r, "T00")[0]


def test_priorite_achat_groupe_avant_montant_plus_eleve():
    p = {**P, "positions_max": 1}
    groupe = [_dirigeant("AAA", 30, montant=120_000.0, cik=1), _dirigeant("AAA", 30, montant=110_000.0, cik=2)]
    gros = _dirigeant("BBB", 30, montant=900_000.0, cik=3)
    cours = {"AAA": _cours(), "BBB": _cours()}
    r = _simuler(groupe + [gros], cours, p=p)
    assert [t["ticker"] for t in r["trades"]] == ["AAA"]
    assert "plafond de 1 positions" in _motifs(r, "BBB")[0]
    # sans second dirigeant, rien n'est groupé : le montant décide
    assert [t["ticker"] for t in _simuler(groupe[:1] + [gros], cours, p=p)["trades"]] == ["BBB"]


def test_un_groupe_n_existe_que_par_les_publications_connues():
    p = {**P, "positions_max": 1}
    # le second dirigeant publie le lendemain : à la séance d'entrée du premier, il n'y a pas encore de groupe
    ops = [_dirigeant("AAA", 30, montant=120_000.0, cik=1), _dirigeant("AAA", 31, montant=110_000.0, cik=2),
           _dirigeant("BBB", 30, montant=900_000.0, cik=3)]
    r = _simuler(ops, {"AAA": _cours(), "BBB": _cours()}, p=p)
    assert [t["ticker"] for t in r["trades"]] == ["BBB"]


def test_taille_sur_la_valeur_de_la_cloture_precedente():
    aaa = _cours()
    aaa.loc[JOURS[51], "Close"] = 400.0  # le compte vaudrait 3 900 $ à la clôture du jour de l'entrée de BBB
    bbb = _cours(10.0, volume=5_000_000.0)
    r = _simuler([_dirigeant("AAA", 30), _dirigeant("BBB", 50, cik=2)], {"AAA": aaa, "BBB": bbb})
    assert {t["ticker"]: t["quantite"] for t in r["trades"]} == {"AAA": 3, "BBB": 30}


def test_liquidites_insuffisantes_reduisent_la_quantite_puis_ignorent():
    ouverture = np.full(len(JOURS), 100.0)
    ouverture[31] = 1400.0  # 3 actions dimensionnées sur la clôture de la veille coûteraient 4 200 $ pour 3 000 $
    r = _simuler([_dirigeant("AAA", 30)], {"AAA": _cours(ouverture)})
    assert r["trades"][0]["quantite"] == 2
    ouverture[31] = 4000.0
    r = _simuler([_dirigeant("AAA", 30)], {"AAA": _cours(ouverture)})
    assert r["trades"] == [] and "liquidités" in _motifs(r, "AAA")[0]


def test_les_frais_sont_pris_en_compte_dans_les_liquidites():
    ouverture = np.full(len(JOURS), 100.0)
    ouverture[31] = 999.0  # 999 $ tiennent dans 1 000 $, pas avec 1 $ + 0,1 % de frais
    p = {**P, "capital_initial": 1000}
    r = _simuler([_dirigeant("AAA", 30)], {"AAA": _cours(ouverture)}, p=p, frais=lambda m, j: 1.0 + 0.001 * m)
    assert r["trades"] == [] and "liquidités" in _motifs(r, "AAA")[0]


def test_achat_publie_avant_le_debut_n_est_pas_copie():
    r = _simuler([_dirigeant("AAA", 25)], {"AAA": _cours()}, debut=30)
    assert r["trades"] == [] and r["ignores"] == []


def test_positions_ouvertes_au_dernier_cours():
    cours = _cours()
    cours.loc[JOURS[100], "Close"] = 130.0
    r = _simuler([_dirigeant("AAA", 30)], {"AAA": cours})
    t = r["trades"][0]
    assert t["raison"] == "ouverte" and t["prix_sortie"] == 130.0 and t["sortie"] == JOURS[100].date()
    assert t["resultat_usd"] == pytest.approx(90.0)
    assert r["final"] == pytest.approx(3090.0) and r["rendement_pct"] == pytest.approx(3.0)
    assert r["valeur"].iloc[-1] == pytest.approx(3090.0)
    assert list(r["valeur"].index) == list(JOURS[20:101])
    assert r["qqq_pct"] == pytest.approx(0.0)


def test_sans_aucune_operation():
    r = _simuler([], {"AAA": _cours()})
    assert r["trades"] == [] and r["ignores"] == [] and r["final"] == 3000 and r["rendement_pct"] == 0


# --- robustesse ---

def test_ouverture_manquante_a_l_entree_est_ignoree():
    cours = _cours()
    cours.loc[JOURS[31], "Open"] = np.nan
    r = _simuler([_dirigeant("AAA", 30)], {"AAA": cours})
    assert r["trades"] == [] and "ouverture" in _motifs(r, "AAA")[0]


def test_ticker_sans_cours_ne_leve_pas():
    r = _simuler([_dirigeant("INCONNU", 30), _dirigeant("AAA", 30, cik=2)], {"AAA": _cours()})
    assert [t["ticker"] for t in r["trades"]] == ["AAA"]
    assert _motifs(r, "INCONNU") == ["hors univers ou sans cours"]


def test_achat_copiable_hors_univers_laisse_une_trace():
    petit = {"AAA": 1e6}  # capitalisation trop faible
    r = _simuler([_dirigeant("AAA", 30)], {"AAA": _cours()}, actions=petit)
    assert r["trades"] == [] and _motifs(r, "AAA") == ["hors univers ou sans cours"]


def test_cours_qui_disparaissent_pendant_la_detention():
    cours = _cours()
    cours.loc[JOURS[50]:, ["Open", "High", "Low", "Close"]] = np.nan
    cours.loc[JOURS[49], "Close"] = 105.0
    r = _simuler([_dirigeant("AAA", 30)], {"AAA": cours})
    t = r["trades"][0]
    assert t["raison"] == "ouverte" and t["prix_sortie"] == 105.0
    assert r["valeur"].notna().all() and r["final"] == pytest.approx(3015.0)


def test_qqq_absent_ou_vide_ne_leve_pas():
    ops = [_dirigeant("AAA", 30)]
    for qqq in (None, pd.DataFrame()):
        r = simuler(ops, {"AAA": _cours()}, qqq, ACTIONS, JOURS[20].date(), JOURS[100].date(), P, frais_nuls)
        assert r["trades"] == [] and r["final"] == 3000 and r["qqq_pct"] is None


def test_qqq_pct():
    qqq = _cours(400.0)
    qqq.loc[JOURS[100], "Close"] = 440.0
    assert _simuler([], {}, qqq=qqq)["qqq_pct"] == pytest.approx(10.0)


# --- politiciens suivis ---

SESSIONS = [30, 40, 50, 60, 70, 80, 90, 100, 110, 140]
DUPONT = "politicien:chambre:Dupont|Jean|CA-01"


def _elu(ticker, publie, personne_id=DUPONT, rang=0, montant=15001.0):
    pub = JOURS[publie].date()
    return Operation(famille="politicien", personne_id=personne_id, personne=personne_id.split(":")[-1].split("|")[0],
                     ticker=ticker, sens="achat", date_operation=pub - timedelta(days=20), date_publication=pub,
                     montant_min=montant, montant_max=montant * 2, source_id=f"chambre:{personne_id}:{publie}:{rang}")


def _scenario():
    """Dupont (10 achats de AAA, en hausse) devient suivi le 1er août 2030 ; il achète encore à la séance 160 (13/08).
    Un dirigeant achète CCC à la séance 190."""
    hausse = 100 * 1.003 ** np.arange(len(JOURS))
    ops = [_elu("AAA", s, rang=i) for i, s in enumerate(SESSIONS)]
    ops += [_elu("AAA", 160, rang=50), _elu("AAA", 162, personne_id="politicien:chambre:Autre|Paul|TX-02", rang=51),
            _dirigeant("CCC", 190)]
    cours = {"AAA": _cours(hausse), "CCC": _cours(50.0)}
    return ops, cours, _cours(400.0)


DEBUT, FIN = JOURS.get_loc(pd.Timestamp("2030-07-01")), 250


def test_un_suivi_est_copie_a_partir_de_sa_selection():
    ops, cours, qqq = _scenario()
    r = _simuler(ops, cours, debut=DEBUT, fin=FIN, qqq=qqq, actions={"AAA": 1e8, "CCC": 1e9})
    assert [(t["ticker"], t["personne"], t["entree"]) for t in r["trades"]] == [
        ("AAA", "Dupont", JOURS[161].date()), ("CCC", "Dirigeant 1", JOURS[191].date())]
    # l'achat de juillet (séance 140) précède la sélection du 1er août ; l'autre élu n'a qu'un achat
    assert r["suivis"] == {"2030-07-01": [], "2030-08-01": [DUPONT], "2030-09-01": [DUPONT], "2030-10-01": [DUPONT],
                           "2030-11-01": [DUPONT], "2030-12-01": [DUPONT]}


# --- pas de regard vers l'avenir ---

def test_un_suivi_exige_des_achats_publies_avant_le_1er():
    # 9 achats avant le 1er juillet, le 10e le 24 juillet : suivi seulement à partir du 1er août
    ops = [_elu("AAA", s, rang=i) for i, s in enumerate(SESSIONS[:-1])] + [_elu("AAA", 146, rang=9)]
    cours, qqq = {"AAA": _scenario()[1]["AAA"]}, _cours(400.0)
    # avec 40 jours de déclarations d'avance, la sélection de juillet retiendrait bien cette personne
    avance = selectionner(ops, cours, qqq, ACTIONS, date(2030, 7, 1) + timedelta(days=40), P)
    assert [ligne["retenu"] for ligne in avance] == [True]
    r = _simuler(ops, cours, debut=DEBUT, fin=FIN, qqq=qqq)
    assert r["suivis"]["2030-07-01"] == [] and r["suivis"]["2030-08-01"] == [DUPONT]
    assert r["trades"] == []  # le 10e achat (juillet) n'a pas été copié : personne n'était suivi ce mois-là

def _abimer(df, depuis):
    df = df.copy()
    df.loc[df.index >= depuis, ["Open", "High", "Low", "Close"]] = 0.5
    df.loc[df.index >= depuis, "Volume"] = 0.0
    return df


@pytest.mark.parametrize("coupure", [175, JOURS.get_loc(pd.Timestamp("2030-09-02")), 192])
def test_simulation_ignore_l_avenir(coupure):
    ops, cours, qqq = _scenario()
    actions = {"AAA": 1e8, "CCC": 1e9}
    depuis = JOURS[coupure]
    avant = _simuler(ops, cours, debut=DEBUT, fin=FIN, qqq=qqq, actions=actions)
    futur = [_elu("AAA", coupure + k, personne_id=f"politicien:chambre:Futur{k}|X|TX-0{k}", rang=k)
             for k in range(1, 12)]
    futur += [_dirigeant("AAA", coupure + 2, cik=7), _dirigeant("AAA", coupure + 3, cik=8, sens="vente")]
    futur = [op for op in futur if op.date_publication >= depuis.date()]
    apres = _simuler(ops + futur, {t: _abimer(df, depuis) for t, df in cours.items()}, debut=DEBUT, fin=FIN,
                     qqq=_abimer(qqq, depuis), actions=actions)
    # le futur change bien le résultat ...
    assert not apres["valeur"].equals(avant["valeur"])
    # ... mais pas ce qui s'est passé avant la coupure
    assert apres["valeur"][apres["valeur"].index < depuis].equals(avant["valeur"][avant["valeur"].index < depuis])
    cle = lambda t: (t["ticker"], t["personne"], t["entree"], t["prix_entree"], t["quantite"])
    assert [cle(t) for t in apres["trades"] if t["entree"] < depuis.date()] == [
        cle(t) for t in avant["trades"] if t["entree"] < depuis.date()]
    limite = depuis.date().isoformat()
    assert {k: v for k, v in apres["suivis"].items() if k <= limite} == {
        k: v for k, v in avant["suivis"].items() if k <= limite}
