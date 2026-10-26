import json
import logging
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from inities import cours as module_cours
from inities.cours import actions_en_circulation, charger
from inities.dirigeants import achat_retenu
from inities.mesure import (SEANCES_3M, SEANCES_6M, bat_qqq, bilan, bilan_dirigeants, dans_univers, rendement_copie,
                            selectionner)
from inities.modele import Operation, nettoyer_ticker, non_cote

P = {"fenetre_mois": 18, "recent_mois": 6, "politicien_achats_min": 10, "politicien_delai_max_j": 30,
     "politicien_montant_min": 15000, "dirigeant_montant_min": 100000, "groupe_dirigeants_jours": 30,
     "fonds_positions_min": 3, "capitalisation_min": 2_000_000_000, "volume_dollars_min": 10_000_000}
JOURS = pd.bdate_range("2029-01-01", periods=300)
DATE_CHOIX = JOURS[200].date()


def _cours(ouverture, cloture, volume=1_000_000.0):
    """DataFrame de 300 séances ; ouverture et cloture sont des tableaux ou des constantes."""
    ouverture = np.broadcast_to(np.asarray(ouverture, dtype=float), len(JOURS)).copy()
    cloture = np.broadcast_to(np.asarray(cloture, dtype=float), len(JOURS)).copy()
    return pd.DataFrame({"Open": ouverture, "High": np.maximum(ouverture, cloture), "Low": np.minimum(ouverture, cloture),
                         "Close": cloture, "Volume": float(volume)}, index=JOURS)


def _hausse():
    return _cours(100 * 1.003 ** np.arange(300), 100 * 1.003 ** np.arange(300))


def _baisse():
    return _cours(100 * 0.997 ** np.arange(300), 100 * 0.997 ** np.arange(300))


def _qqq():
    return _cours(400.0, 400.0)


def _op(ticker="AAA", pub=None, famille="politicien", personne_id="politicien:chambre:Dupont|Jean|CA-01",
        montant=15001.0, delai_j=20, rang=0, sens="achat", **details):
    pub = pub or JOURS[30].date()
    return Operation(famille=famille, personne_id=personne_id, personne=personne_id.split(":")[-1].split("|")[-1], ticker=ticker,
                     sens=sens, date_operation=pub - timedelta(days=delai_j), date_publication=pub,
                     montant_min=montant, montant_max=montant * 2, source_id=f"test:{personne_id}:{pub}:{rang}",
                     details=details)


def _achats_politicien(sessions, personne_id="politicien:chambre:Dupont|Jean|CA-01", ticker="AAA", **options):
    return [_op(ticker, JOURS[s].date(), personne_id=personne_id, rang=i, **options) for i, s in enumerate(sessions)]


SESSIONS = [30, 40, 50, 60, 70, 80, 90, 100, 110, 140]  # dernier achat 60 séances (≈ 3 mois) avant le choix
ACTIONS = {"AAA": 1e8, "BBB": 1e8, "PETIT": 1e6}


def _univers():
    return {"AAA": _hausse(), "BBB": _baisse(), "PETIT": _hausse()}


# --- rendement_copie ---

def _cours_publication():
    """Open = 100 à la séance 31 (lendemain de la publication à la séance 30), Close = 110 63 séances plus tard."""
    cloture = np.full(300, 100.0)
    cloture[31 + SEANCES_3M] = 110.0
    qqq_cloture = np.full(300, 400.0)
    qqq_cloture[31 + SEANCES_3M] = 420.0
    return {"AAA": _cours(100.0, cloture)}, _cours(400.0, qqq_cloture)


def test_rendement_copie_au_prix_de_publication():
    cours, qqq = _cours_publication()
    op = _op(pub=JOURS[30].date())
    assert rendement_copie(op, cours, qqq, 63, date(2030, 12, 31)) == pytest.approx((10.0, 5.0))


def test_entree_a_l_ouverture_de_la_premiere_seance_apres_la_publication():
    cours, qqq = _cours_publication()
    cours["AAA"].iloc[30, cours["AAA"].columns.get_loc("Open")] = 1.0  # séance de la publication : ignorée
    cours["AAA"].iloc[32, cours["AAA"].columns.get_loc("Open")] = 1.0  # séance suivante : ignorée
    # publication un samedi : la première séance après est le lundi
    samedi = JOURS[30].date() + timedelta(days=(5 - JOURS[30].weekday()) % 7 or 7)
    suivante = JOURS[JOURS > pd.Timestamp(samedi)][0]
    assert suivante.weekday() == 0
    assert rendement_copie(_op(pub=JOURS[30].date()), cours, qqq, 63, date(2030, 12, 31))[0] == pytest.approx(10.0)
    position = JOURS.get_loc(suivante)
    cours["AAA"].iloc[position, cours["AAA"].columns.get_loc("Open")] = 50.0
    cours["AAA"].iloc[position + 63, cours["AAA"].columns.get_loc("Close")] = 75.0
    resultat = rendement_copie(_op(pub=samedi), cours, qqq, 63, date(2030, 12, 31))
    assert resultat[0] == pytest.approx(50.0)


def test_rendement_inconnu_si_pas_encore_ecoule():
    cours, qqq = _cours_publication()
    op = _op(pub=JOURS[30].date())
    assert rendement_copie(op, cours, qqq, 126, op.date_publication + timedelta(days=100)) is None


def test_la_seance_de_sortie_doit_etre_strictement_avant_la_date_limite():
    cours, qqq = _cours_publication()
    op = _op(pub=JOURS[30].date())
    sortie = JOURS[31 + SEANCES_3M].date()
    assert rendement_copie(op, cours, qqq, 63, sortie) is None
    assert rendement_copie(op, cours, qqq, 63, sortie + timedelta(days=1)) is not None


def test_rendement_inconnu_si_une_donnee_manque():
    cours, qqq = _cours_publication()
    op = _op(pub=JOURS[30].date())
    limite = date(2030, 12, 31)
    assert rendement_copie(_op("ZZZ", JOURS[30].date()), cours, qqq, 63, limite) is None
    sans_serie = {"AAA": cours["AAA"].iloc[:60]}
    assert rendement_copie(op, sans_serie, qqq, 63, limite) is None
    trou = {"AAA": cours["AAA"].copy()}
    trou["AAA"].iloc[31 + SEANCES_3M, trou["AAA"].columns.get_loc("Close")] = np.nan
    assert rendement_copie(op, trou, qqq, 63, limite) is None
    assert rendement_copie(op, cours, qqq.drop(qqq.index[31 + SEANCES_3M]), 63, limite) is None
    assert rendement_copie(op, cours, qqq.drop(columns=["Open"]), 63, limite) is None
    zero = {"AAA": cours["AAA"].copy()}
    zero["AAA"].iloc[31, zero["AAA"].columns.get_loc("Open")] = 0.0
    assert rendement_copie(op, zero, qqq, 63, limite) is None


# --- dans_univers ---

def test_dans_univers_selon_capitalisation_et_volume():
    op = _op(pub=JOURS[30].date())
    assert dans_univers(op, {"AAA": _hausse()}, {"AAA": 1e8}, P)
    assert not dans_univers(op, {"AAA": _hausse()}, {"AAA": 1e6}, P)  # ~1e8 $ de capitalisation
    assert not dans_univers(op, {"AAA": _cours(100.0, 100.0, volume=1_000)}, {"AAA": 1e8}, P)  # 1e5 $ par jour


def test_univers_utilise_la_veille_de_la_publication():
    op = _op(pub=JOURS[30].date())
    cours = _cours(100.0, 100.0)
    cours.iloc[29, cours.columns.get_loc("Close")] = 10.0  # veille : capitalisation 1e9 < 2e9 avec 1e8 actions
    cours.iloc[30, cours.columns.get_loc("Close")] = 1000.0  # jour de la publication : ne compte pas
    assert not dans_univers(op, {"AAA": cours}, {"AAA": 1e8}, P)


def test_univers_donnees_manquantes_ne_levent_pas():
    op = _op(pub=JOURS[30].date())
    assert not dans_univers(op, {}, {"AAA": 1e8}, P)
    assert not dans_univers(op, {"AAA": _hausse()}, {}, P)
    assert not dans_univers(op, {"AAA": _hausse()}, {"AAA": 0.0}, P)
    assert not dans_univers(_op(pub=JOURS[10].date()), {"AAA": _hausse()}, {"AAA": 1e8}, P)  # < 20 séances
    sans_volume = _hausse()
    sans_volume.iloc[15, sans_volume.columns.get_loc("Volume")] = np.nan
    assert not dans_univers(op, {"AAA": sans_volume}, {"AAA": 1e8}, P)
    assert not dans_univers(op, {"AAA": _hausse().drop(columns=["Volume"])}, {"AAA": 1e8}, P)


# --- bilan et bat_qqq ---

def test_bilan_compte_et_mesure():
    ops = _achats_politicien([30, 40], delai_j=10) + [_op(pub=JOURS[140].date(), delai_j=30)]
    b = bilan(ops, {"AAA": _hausse()}, _qqq(), DATE_CHOIX)
    assert b["achats"] == 3
    assert b["delai_moyen"] == pytest.approx((10 + 10 + 30) / 3)
    assert b["dernier_achat"] == JOURS[140].date()
    assert b["r3"] > 0 and b["q3"] == pytest.approx(0.0)
    assert b["r6"] > b["r3"] and b["q6"] == pytest.approx(0.0)
    assert len(b["mesures"]) == 2  # le dernier achat n'a aucune mesure écoulée
    assert b["part_battu"] == 1.0


def test_bilan_sans_achat_ni_mesure():
    b = bilan([], {}, _qqq(), DATE_CHOIX)
    assert b["achats"] == 0 and b["delai_moyen"] is None and b["dernier_achat"] is None
    assert b["r3"] is None and b["r6"] is None and b["mesures"] == [] and b["part_battu"] is None
    assert not bat_qqq(b)


def test_bilan_mesure_a_6_mois_sinon_a_3_mois():
    # session 30 : mesurable à 6 mois ; session 110 : seulement à 3 mois
    ops = [_op(pub=JOURS[30].date(), rang=1), _op(pub=JOURS[110].date(), rang=2)]
    b = bilan(ops, {"AAA": _hausse()}, _qqq(), DATE_CHOIX)
    attendu6 = (1.003 ** 126 - 1) * 100
    attendu3 = (1.003 ** 63 - 1) * 100
    assert b["mesures"][0][0] == pytest.approx(attendu6, rel=0.01)
    assert b["mesures"][1][0] == pytest.approx(attendu3, rel=0.01)
    assert b["r6"] == pytest.approx(b["mesures"][0][0])
    assert b["r"] == pytest.approx(sum(m[0] for m in b["mesures"]) / 2)
    assert b["q"] == pytest.approx(0.0)


def test_bat_qqq_exige_la_majorite():
    base = {"r3": None, "q3": None, "r6": 30.0, "q6": 10.0, "part_battu": 0.6}
    assert bat_qqq(base)
    assert not bat_qqq({**base, "part_battu": 0.4})
    assert not bat_qqq({**base, "part_battu": 0.5})
    assert not bat_qqq({**base, "r6": 10.0})
    assert not bat_qqq({**base, "r6": 5.0})


def test_bat_qqq_se_replie_sur_3_mois():
    assert bat_qqq({"r3": 5.0, "q3": 1.0, "r6": None, "q6": None, "part_battu": 1.0})
    assert not bat_qqq({"r3": None, "q3": None, "r6": None, "q6": None, "part_battu": 1.0})


# --- selectionner ---

def _selection(ops, cours=None, actions=None, date_choix=DATE_CHOIX):
    return selectionner(ops, cours or _univers(), _qqq(), actions or ACTIONS, date_choix, P)


def _ligne(lignes, personne_id):
    return next(ligne for ligne in lignes if ligne["personne_id"] == personne_id)


def test_politicien_retenu():
    lignes = _selection(_achats_politicien(SESSIONS))
    assert len(lignes) == 1
    ligne = lignes[0]
    assert ligne["retenu"] is True and ligne["motif"] == "retenu"
    assert ligne["famille"] == "politicien" and ligne["personne"] == "CA-01"
    assert ligne["bilan"]["achats"] == 10 and ligne["bilan"]["delai_moyen"] == 20


def test_politicien_motif_pas_assez_d_achats():
    ligne = _selection(_achats_politicien(SESSIONS[:9]))[0]
    assert not ligne["retenu"] and ligne["motif"] == "9 achats, il en faut 10"


def test_achats_sous_le_montant_minimum_ne_comptent_pas():
    ops = _achats_politicien(SESSIONS[:9]) + [_op(pub=JOURS[s].date(), montant=1001.0, rang=50 + s) for s in (45, 55)]
    ligne = _selection(ops)[0]
    assert ligne["bilan"]["achats"] == 9 and ligne["motif"] == "9 achats, il en faut 10"
    exact = _achats_politicien(SESSIONS[:9]) + [_op(pub=JOURS[140].date(), montant=15000.0, rang=99)]
    assert _selection(exact)[0]["retenu"]  # la borne basse de 15 000 $ est acceptée


def test_politicien_motif_delai_trop_long():
    ligne = _selection(_achats_politicien(SESSIONS, delai_j=35))[0]
    assert not ligne["retenu"] and "délai" in ligne["motif"] and "35" in ligne["motif"]
    assert _selection(_achats_politicien(SESSIONS, delai_j=30))[0]["motif"].startswith("délai")  # strictement < 30


def test_politicien_motif_dernier_achat_trop_ancien():
    ops = _achats_politicien([30, 40, 50, 60, 70, 80, 90, 100, 105, 110])  # dernier achat 90 séances avant le choix
    ligne = _selection(ops, date_choix=(JOURS[200] + pd.DateOffset(months=5)).date())[0]
    assert not ligne["retenu"] and "dernier achat" in ligne["motif"]


def test_politicien_motif_ne_bat_pas_qqq():
    ligne = _selection(_achats_politicien(SESSIONS, ticker="BBB"))[0]
    assert not ligne["retenu"] and "QQQ" in ligne["motif"]


def test_hors_univers_et_ticker_sans_cours_ignores():
    ops = (_achats_politicien(SESSIONS[:5], ticker="PETIT") + _achats_politicien(SESSIONS[5:], ticker="INCONNU")
           + _achats_politicien(SESSIONS[:3], ticker="AAA", personne_id="politicien:senat:Martin|Paul"))
    lignes = _selection(ops)
    assert len(lignes) == 1  # seul Paul Martin a des achats dans l'univers
    assert lignes[0]["personne_id"] == "politicien:senat:Martin|Paul"
    assert lignes[0]["bilan"]["achats"] == 3


def test_fenetre_de_choix():
    debut = (pd.Timestamp(DATE_CHOIX) - pd.DateOffset(months=18)).date()
    dedans = _op(pub=max(debut, JOURS[30].date()), rang=1)
    veille = _op(pub=JOURS[30].date(), rang=2)
    apres = _op(pub=DATE_CHOIX, rang=3)  # publiée le jour du choix : hors fenêtre
    ligne = _selection([dedans, veille, apres])[0]
    assert ligne["bilan"]["achats"] == 2
    ancien = _selection([_op(pub=JOURS[30].date()), _op(pub=JOURS[30].date(), rang=1)],
                        date_choix=(pd.Timestamp(JOURS[30]) + pd.DateOffset(months=18)).date() + timedelta(days=1))
    assert ancien == []  # tout est plus vieux que 18 mois


def test_ventes_et_dirigeants_ne_sont_pas_selectionnes():
    ops = [_op(sens="vente", rang=1), _op(famille="dirigeant", personne_id="dirigeant:42", montant=500000.0, rang=2,
                                          code="P", dirigeant=True)]
    assert _selection(ops) == []


def test_fonds_retenu_et_motifs():
    def fonds(sessions, cik="1", ticker="AAA"):
        return [_op(ticker, JOURS[s].date(), famille="fonds", personne_id=f"fonds:{cik}", montant=1e7, delai_j=45,
                    rang=i) for i, s in enumerate(sessions)]
    ops = fonds([30, 60, 140]) + fonds([30, 60], cik="2") + fonds([30, 40, 140], cik="3", ticker="BBB")
    ops += fonds([30, 40, 50], cik="4")  # trois positions mais aucune dans les 6 derniers mois
    lignes = _selection(ops)
    assert _ligne(lignes, "fonds:1")["retenu"]  # le délai de 45 jours ne concerne que les élus
    assert _ligne(lignes, "fonds:2")["motif"] == "2 nouvelles positions, il en faut 3"
    assert "QQQ" in _ligne(lignes, "fonds:3")["motif"]
    ancien = _selection(ops, date_choix=(JOURS[200] + pd.DateOffset(months=5)).date())
    assert "dernier achat" in _ligne(ancien, "fonds:4")["motif"]


def test_selection_ignore_l_avenir():
    ops = _achats_politicien(SESSIONS)
    cours, qqq = _univers(), _qqq()
    avant = selectionner(ops, cours, qqq, ACTIONS, DATE_CHOIX, P)
    assert avant[0]["retenu"]

    def abime(df):
        futur = df.index >= pd.Timestamp(DATE_CHOIX)
        df = df.copy()
        df.loc[futur, ["Open", "High", "Low", "Close"]] = 0.01
        df.loc[futur, "Volume"] = 0.0
        return df
    cours_modifies = {t: abime(df) for t, df in cours.items()}
    qqq_modifie = abime(qqq)
    assert not cours_modifies["AAA"].equals(cours["AAA"])
    # le futur est bien capable de changer le bilan quand on ne le protège pas
    assert bilan(ops, cours_modifies, qqq_modifie, date(2031, 1, 1)) != bilan(ops, cours, qqq, date(2031, 1, 1))
    ops_futures = ops + [_op(pub=DATE_CHOIX, rang=100), _op(pub=DATE_CHOIX + timedelta(days=5), rang=101),
                         _op(pub=DATE_CHOIX + timedelta(days=9), rang=102, ticker="BBB",
                             personne_id="politicien:chambre:Futur|Nouveau|TX-02")]
    assert selectionner(ops_futures, cours_modifies, qqq_modifie, ACTIONS, DATE_CHOIX, P) == avant


# --- bilan_dirigeants ---

def _dirigeant(pub, montant=200000.0, rang=0, ticker="AAA", **details):
    options = {"code": "P", "dirigeant": True, **details}
    return _op(ticker, pub, famille="dirigeant", personne_id="dirigeant:42", montant=montant, rang=rang, **options)


def test_bilan_dirigeants_garde_les_achats_retenus_dans_l_univers():
    ops = [_dirigeant(JOURS[30].date(), rang=1), _dirigeant(JOURS[40].date(), rang=2),
           _dirigeant(JOURS[50].date(), montant=5000.0, rang=3),  # sous le seuil
           _dirigeant(JOURS[50].date(), rang=4, dirigeant=False),  # pas dirigeant
           _dirigeant(JOURS[50].date(), rang=5, code="S"),  # pas un achat
           _dirigeant(JOURS[50].date(), rang=6, ticker="PETIT"),  # hors univers
           _dirigeant(JOURS[50].date(), rang=7, ticker="INCONNU"),
           _dirigeant(DATE_CHOIX + timedelta(days=2), rang=8)]  # publié après la date limite
    assert [achat_retenu(op, P) for op in ops[:3]] == [True, True, False]
    b = bilan_dirigeants(ops, _univers(), _qqq(), ACTIONS, DATE_CHOIX, P)
    assert b["achats"] == 2 and b["part_battu"] == 1.0


# --- cours.charger et actions_en_circulation ---

class FauxYf:
    def __init__(self, series, actions=None):
        self.series, self.actions = series, actions or {}
        self.appels_download, self.appels_ticker = [], []

    def download(self, tickers, **options):
        self.appels_download.append((list(tickers), options))
        morceaux = {t: self.series[t] for t in tickers if t in self.series}
        if not morceaux:
            return pd.DataFrame()
        return pd.concat(morceaux, axis=1)

    def Ticker(self, ticker):
        self.appels_ticker.append(ticker)
        valeur = self.actions.get(ticker)
        if valeur == "erreur":
            raise RuntimeError("hors ligne")
        return type("T", (), {"fast_info": {"shares": valeur}})()


def _serie(debut="2030-01-02", jours=40):
    index = pd.bdate_range(debut, periods=jours)
    return pd.DataFrame({"Open": 10.0, "High": 11.0, "Low": 9.0, "Close": 10.5, "Volume": 1e6}, index=index)


def test_charger_telecharge_puis_lit_le_cache(tmp_path, monkeypatch):
    faux = FauxYf({"AAA": _serie(), "BBB": _serie()})
    monkeypatch.setattr(module_cours, "yf", faux)
    resultat = charger(["AAA", "BBB", "ZZZ"], date(2030, 1, 2), date(2030, 2, 20), tmp_path)
    assert set(resultat) == {"AAA", "BBB"}  # un ticker sans données est absent
    assert list(resultat["AAA"].columns) == ["Open", "High", "Low", "Close", "Volume"]
    options = faux.appels_download[0][1]
    assert options["auto_adjust"] is True and options["start"] == "2030-01-02" and options["end"] == "2030-02-21"
    assert len(faux.appels_download) == 1
    again = charger(["AAA", "BBB"], date(2030, 1, 2), date(2030, 2, 20), tmp_path)
    assert len(faux.appels_download) == 1  # tout vient du cache
    pd.testing.assert_frame_equal(again["AAA"], resultat["AAA"])
    charger(["AAA", "ZZZ"], date(2030, 1, 2), date(2030, 2, 20), tmp_path)
    assert faux.appels_download[-1][0] == ["ZZZ"]  # seul le ticker absent du cache est redemandé


def test_charger_coupe_a_la_periode_et_redemande_une_periode_plus_large(tmp_path, monkeypatch):
    faux = FauxYf({"AAA": _serie()})
    monkeypatch.setattr(module_cours, "yf", faux)
    court = charger(["AAA"], date(2030, 1, 10), date(2030, 1, 20), tmp_path)["AAA"]
    assert court.index.min() >= pd.Timestamp("2030-01-10") and court.index.max() <= pd.Timestamp("2030-01-20")
    charger(["AAA"], date(2030, 1, 10), date(2030, 1, 15), tmp_path)
    assert len(faux.appels_download) == 1  # période incluse
    charger(["AAA"], date(2030, 1, 2), date(2030, 1, 20), tmp_path)
    assert len(faux.appels_download) == 2  # débute plus tôt


def test_charger_ignore_erreurs_et_cache_illisible(tmp_path, monkeypatch):
    faux = FauxYf({"AAA": _serie()})
    monkeypatch.setattr(module_cours, "yf", faux)
    charger(["AAA"], date(2030, 1, 2), date(2030, 2, 20), tmp_path)
    for fichier in tmp_path.iterdir():
        fichier.write_bytes(b"pas un pickle")
    assert "AAA" in charger(["AAA"], date(2030, 1, 2), date(2030, 2, 20), tmp_path)
    assert len(faux.appels_download) == 2

    def casse(*args, **options):
        raise RuntimeError("hors ligne")
    monkeypatch.setattr(faux, "download", casse)
    pauses = []
    assert charger(["NEW"], date(2030, 1, 2), date(2030, 2, 20), tmp_path, attendre=pauses.append) == {}
    assert pauses == [30, 60, 120]


class LimiteYf(FauxYf):
    """Yahoo qui refuse les `refus` premiers appels (exception de limite de débit), puis répond."""
    def __init__(self, series, refus, actions=None):
        super().__init__(series, actions)
        self.refus = refus

    def download(self, tickers, **options):
        if self.refus > 0:
            self.refus -= 1
            self.appels_download.append((list(tickers), options))
            raise RuntimeError("YFRateLimitError('Too Many Requests. Rate limited. Try after a while.')")
        return super().download(tickers, **options)


def test_charger_attend_puis_reessaie_apres_une_limite_de_debit(tmp_path, monkeypatch):
    faux = LimiteYf({"AAA": _serie(), "BBB": _serie()}, refus=1)
    monkeypatch.setattr(module_cours, "yf", faux)
    pauses = []
    resultat = charger(["AAA", "BBB"], date(2030, 1, 2), date(2030, 2, 20), tmp_path, attendre=pauses.append)
    assert set(resultat) == {"AAA", "BBB"}
    assert pauses == [30]  # une seule attente, puis ça passe
    assert len(faux.appels_download) == 2


def test_charger_echec_permanent_ne_leve_pas_et_renonce_apres_trois_reprises(tmp_path, monkeypatch):
    faux = LimiteYf({"AAA": _serie()}, refus=100)
    monkeypatch.setattr(module_cours, "yf", faux)
    pauses = []
    assert charger(["AAA", "BBB"], date(2030, 1, 2), date(2030, 2, 20), tmp_path, attendre=pauses.append) == {}
    assert pauses == [30, 60, 120]
    assert len(faux.appels_download) == 4  # 1 essai + 3 reprises
    assert not list(tmp_path.glob("cours-*.pkl"))  # rien en cache pour ce qui a échoué


def test_charger_garde_en_cache_les_lots_deja_recus_quand_un_lot_echoue(tmp_path, monkeypatch):
    faux = FauxYf({"AAA": _serie(), "BBB": _serie(), "CCC": _serie(), "DDD": _serie()})
    appels = []

    def download(tickers, **options):
        appels.append(list(tickers))
        if "CCC" in tickers:
            raise RuntimeError("Rate limited")
        return FauxYf.download(faux, tickers, **options)
    monkeypatch.setattr(faux, "download", download)
    monkeypatch.setattr(module_cours, "yf", faux)
    pauses = []
    resultat = charger(["AAA", "BBB", "CCC", "DDD"], date(2030, 1, 2), date(2030, 2, 20), tmp_path,
                       attendre=pauses.append, taille_lot=2)
    assert set(resultat) == {"AAA", "BBB"}
    assert module_cours.PAUSE_LOTS in pauses  # une pause entre les deux lots
    appels.clear()
    assert set(charger(["AAA", "BBB"], date(2030, 1, 2), date(2030, 2, 20), tmp_path, attendre=pauses.append)) \
        == {"AAA", "BBB"}
    assert appels == []  # le lot 1 est en cache


def test_charger_demande_par_lots_de_la_taille_voulue(tmp_path, monkeypatch):
    series = {f"T{i:03d}": _serie() for i in range(7)}
    faux = FauxYf(series)
    monkeypatch.setattr(module_cours, "yf", faux)
    charger(list(series), date(2030, 1, 2), date(2030, 2, 20), tmp_path, attendre=lambda s: None, taille_lot=3)
    assert [len(t) for t, _ in faux.appels_download] == [3, 3, 1]


def test_actions_en_circulation_reessaie_apres_limite_et_ecrit_au_fil_de_l_eau(tmp_path, monkeypatch):
    faux = FauxYf({}, {"AAA": 5e8, "BBB": 1e8})
    essais = {"AAA": 0}
    vrai_ticker = faux.Ticker

    def ticker(nom):
        if nom == "AAA" and essais["AAA"] == 0:
            essais["AAA"] += 1
            raise RuntimeError("YFRateLimitError: Too Many Requests")
        return vrai_ticker(nom)
    monkeypatch.setattr(faux, "Ticker", ticker)
    monkeypatch.setattr(module_cours, "yf", faux)
    pauses = []
    assert actions_en_circulation(["AAA", "BBB"], tmp_path, attendre=pauses.append, taille_lot=1) \
        == {"AAA": 5e8, "BBB": 1e8}
    assert 30 in pauses
    assert json.loads((tmp_path / "actions.json").read_text()) == {"AAA": 5e8, "BBB": 1e8}


def test_actions_en_circulation_echec_permanent_ne_leve_pas(tmp_path, monkeypatch):
    faux = FauxYf({}, {"AAA": 5e8})

    def ticker(nom):
        raise RuntimeError("Rate limited")
    monkeypatch.setattr(faux, "Ticker", ticker)
    monkeypatch.setattr(module_cours, "yf", faux)
    pauses = []
    assert actions_en_circulation(["AAA"], tmp_path, attendre=pauses.append) == {}
    assert pauses == [30, 60, 120]


def test_actions_en_circulation_et_cache(tmp_path, monkeypatch):
    faux = FauxYf({}, {"AAA": 5e8, "BBB": None, "CCC": "erreur", "DDD": float("nan"), "EEE": 0})
    monkeypatch.setattr(module_cours, "yf", faux)
    tickers = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
    assert actions_en_circulation(tickers, tmp_path) == {"AAA": 5e8}
    premier = len(faux.appels_ticker)
    assert actions_en_circulation(["AAA"], tmp_path) == {"AAA": 5e8}
    assert len(faux.appels_ticker) == premier  # AAA vient du cache
    (tmp_path / "actions.json").write_text("{pas du json")
    assert actions_en_circulation(["AAA"], tmp_path) == {"AAA": 5e8}


def test_constantes_de_seances():
    assert (SEANCES_3M, SEANCES_6M) == (63, 126)


def test_bat_qqq_compare_les_memes_copies_que_la_part_battue():
    # 1 copie à 6 mois (+2 % contre +1 %), 3 copies à 3 mois (+0,1 % contre 0), 2 copies à 3 mois (-30 % contre 0)
    mesures = [(2.0, 1.0)] + [(0.1, 0.0)] * 3 + [(-30.0, 0.0)] * 2
    b = {"r3": -9.8, "q3": 0.0, "r6": 2.0, "q6": 1.0, "mesures": mesures,
         "r": sum(m[0] for m in mesures) / 6, "q": sum(m[1] for m in mesures) / 6, "part_battu": 4 / 6}
    assert b["r"] < b["q"]
    assert not bat_qqq(b)
    assert bat_qqq({**b, "r": 1.0, "q": 0.5})


@pytest.mark.parametrize("brut, propre", [
    ("AAPL", "AAPL"), ("  aapl ", "AAPL"), ("NYSE:NYCB", "NYCB"), ("nasdaq: msft", "MSFT"), ("BRK.B", "BRK.B"),
    ("BRK-B", "BRK-B"), ("NCLH]", None), ("N O G", None), ("[ NONE ]", None), ("N.A.", None),
    ("LEN, LEN.B", None), ("HEI, HEI.A", None), ("BBXIA/B", None), ("", None), ("TOOLONGX", None), (None, None)])
def test_nettoyer_ticker(brut, propre):
    assert nettoyer_ticker(brut) == propre


@pytest.mark.parametrize("brut, attendu", [
    ("[NONE]", True), ("[ NONE ]", True), ("None", True), ("N/A", True), ("NA", True), ("N.A.", True), ("-", True),
    ("", True), (None, True), ("CRAFX", True), ("vfiax", True), ("AAPL", False), ("NXG", False), ("GOOGL", False),
    ("XXXX", False), ("LEN, LEN.B", False)])
def test_non_cote(brut, attendu):
    assert non_cote(brut) is attendu


def test_charger_demande_le_point_de_classe_avec_un_tiret_mais_garde_le_ticker_d_origine(tmp_path, monkeypatch):
    faux = FauxYf({"BRK-B": _serie()})  # Yahoo ne connaît que « BRK-B »
    monkeypatch.setattr(module_cours, "yf", faux)
    resultat = charger(["BRK.B"], date(2030, 1, 2), date(2030, 2, 20), tmp_path)
    assert set(resultat) == {"BRK.B"}
    assert faux.appels_download[0][0] == ["BRK-B"]
    assert (tmp_path / "cours-BRK.B.pkl").exists()
    charger(["BRK.B"], date(2030, 1, 2), date(2030, 2, 20), tmp_path)
    assert len(faux.appels_download) == 1  # relu du cache sous le ticker d'origine


def test_actions_en_circulation_demande_le_point_de_classe_avec_un_tiret(tmp_path, monkeypatch):
    faux = FauxYf({}, {"BRK-B": 2e9})
    monkeypatch.setattr(module_cours, "yf", faux)
    assert actions_en_circulation(["BRK.B"], tmp_path) == {"BRK.B": 2e9}
    assert faux.appels_ticker == ["BRK-B"]
    assert json.loads((tmp_path / "actions.json").read_text()) == {"BRK.B": 2e9}


class SilencieuxYf(FauxYf):
    """yfinance 1.x en cas de limite de débit : download ne lève rien, renvoie un tableau vide et journalise l'erreur
    sur le logger « yfinance » ; Ticker(...).fast_info n'a pas de valeur et journalise de même."""
    def __init__(self, series, limites_restantes, limites=("AAA",), actions=None):
        super().__init__(series, actions)
        self.restantes, self.limites = limites_restantes, list(limites)

    def _journaliser(self, noms):
        logging.getLogger("yfinance").error(f"{noms}: YFRateLimitError('Too Many Requests. Rate limited. Try after a while.')")

    def download(self, tickers, **options):
        self.appels_download.append((list(tickers), options))
        if self.restantes > 0:
            self.restantes -= 1
            self._journaliser([t for t in tickers if t in self.limites])
            return pd.DataFrame()
        morceaux = {t: self.series[t] for t in tickers if t in self.series}
        return pd.concat(morceaux, axis=1) if morceaux else pd.DataFrame()

    def Ticker(self, ticker):
        self.appels_ticker.append(ticker)
        if self.restantes > 0:
            self.restantes -= 1
            self._journaliser([ticker])
            return type("T", (), {"fast_info": {"shares": None}})()
        return super().Ticker(ticker)


def test_charger_detecte_la_limite_journalisee_sans_exception_puis_reprend(tmp_path, monkeypatch):
    faux = SilencieuxYf({"AAA": _serie(), "BBB": _serie()}, limites_restantes=1, limites=("AAA", "BBB"))
    monkeypatch.setattr(module_cours, "yf", faux)
    pauses, limites = [], set()
    resultat = charger(["AAA", "BBB"], date(2030, 1, 2), date(2030, 2, 20), tmp_path, attendre=pauses.append,
                       limites=limites)
    assert set(resultat) == {"AAA", "BBB"} and not limites
    assert pauses == [30] and len(faux.appels_download) == 2


def test_charger_limite_persistante_est_comptee_a_part_des_titres_sans_donnees(tmp_path, monkeypatch):
    faux = SilencieuxYf({}, limites_restantes=100, limites=("AAA",))
    monkeypatch.setattr(module_cours, "yf", faux)
    pauses, limites = [], set()
    resultat = charger(["AAA"], date(2030, 1, 2), date(2030, 2, 20), tmp_path, attendre=pauses.append, limites=limites)
    assert resultat == {} and limites == {"AAA"}
    assert pauses == [30, 60, 120] and len(faux.appels_download) == 4
    # un titre sans données, sans message de limite, n'est pas compté comme limité et n'attend pas
    sans_limite = FauxYf({})
    monkeypatch.setattr(module_cours, "yf", sans_limite)
    pauses, limites = [], set()
    assert charger(["ZZZ"], date(2030, 1, 2), date(2030, 2, 20), tmp_path, attendre=pauses.append, limites=limites) == {}
    assert pauses == [] and limites == set()


def test_charger_ignore_l_avertissement_de_cookie_de_yfinance(tmp_path, monkeypatch):
    faux = FauxYf({})

    def download(tickers, **options):
        logging.getLogger("yfinance").warning("Crumb fetch rate-limited (HTTP 429), continuing without crumb")
        return pd.DataFrame()
    monkeypatch.setattr(faux, "download", download)
    monkeypatch.setattr(module_cours, "yf", faux)
    pauses, limites = [], set()
    assert charger(["ZZZ"], date(2030, 1, 2), date(2030, 2, 20), tmp_path, attendre=pauses.append, limites=limites) == {}
    assert pauses == [] and limites == set()


def test_actions_detecte_la_limite_journalisee_sans_exception(tmp_path, monkeypatch):
    faux = SilencieuxYf({}, limites_restantes=1, actions={"AAA": 5e8})
    monkeypatch.setattr(module_cours, "yf", faux)
    pauses, limites = [], set()
    assert actions_en_circulation(["AAA"], tmp_path, attendre=pauses.append, limites=limites) == {"AAA": 5e8}
    assert pauses == [30] and not limites
    faux = SilencieuxYf({}, limites_restantes=100, actions={"BBB": 5e8})
    monkeypatch.setattr(module_cours, "yf", faux)
    pauses, limites = [], set()
    assert actions_en_circulation(["BBB"], tmp_path, attendre=pauses.append, limites=limites) == {}
    assert pauses == [30, 60, 120] and limites == {"BBB"}
