from datetime import date

import numpy as np
import pandas as pd

import commun
from commun import charger_config
from court_terme.preglages import parametres
from court_terme import strategie
from court_terme.strategie import (configuration_a, configuration_b, exclu, indicateurs, niveaux,
                                   reperer, seances_avant)

p = parametres(charger_config(), "reference")


def test_niveaux():
    assert niveaux(100, 2, p) == {"entree": 99.5, "stop": 96.5, "objectif": 105.5}


def test_seances_avant():
    assert seances_avant(date(2026, 10, 19), date(2026, 10, 30)) == 9
    assert seances_avant(date(2026, 10, 19), date(2026, 11, 3)) == 11


def test_exclu_resultats_proches():
    ind = {"cloture": 100, "veille": 99, "atr": 2}
    assert exclu(ind, date(2026, 10, 19), date(2026, 10, 30), p) is not None
    assert exclu(ind, date(2026, 10, 19), date(2026, 11, 3), p) is None
    assert exclu(ind, date(2026, 10, 19), date(2026, 10, 10), p) is None  # résultats déjà passés
    assert exclu(ind, date(2026, 10, 19), date(2026, 10, 19), p) is not None  # résultats ce soir


def test_exclu_mouvement_trop_fort():
    assert exclu({"cloture": 105, "veille": 100, "atr": 2}, date(2026, 10, 19), None, p) is not None


def test_configuration_a():
    ind = dict(cloture=100, mm20=99.5, mm50=95, mm200=90, rsi=40, atr=2, perf_3m=12, perf_indice_3m=5)
    assert configuration_a(ind, p)
    assert not configuration_a({**ind, "rsi": 50}, p)
    assert not configuration_a({**ind, "mm50": 89}, p)       # MM50 sous MM200
    assert not configuration_a({**ind, "perf_3m": 3}, p)     # moins bien que l'indice
    assert not configuration_a({**ind, "mm20": 96.5}, p)     # trop loin des moyennes (MM50 à 2,5 ATR)


def test_configuration_b():
    ind = dict(cloture=110, plus_haut_20=108, volume=3e6, volume_moyen_20=1.5e6, rsi=70)
    assert configuration_b(ind, p)
    assert not configuration_b({**ind, "volume": 2e6}, p)


def _serie(n, depart=100.0):
    return pd.Series(np.linspace(depart, depart * 1.3, n))


def test_indicateurs_historique_complet():
    c = _serie(260)
    ind = indicateurs(c * 1.01, c * 0.99, c, pd.Series([1e6] * 260), _serie(260))
    assert ind["cloture"] == c.iloc[-1] and ind["veille"] == c.iloc[-2]
    assert ind["mm200"] < ind["mm50"] < ind["mm20"] < ind["cloture"]
    assert ind["atr"] > 0 and ind["plus_haut_20"] < ind["cloture"] * 1.01


def test_indicateurs_historique_trop_court_ou_nan():
    c = _serie(150)
    assert indicateurs(c, c, c, c, c) is None
    vide = pd.Series([np.nan] * 260)
    assert indicateurs(vide, vide, vide, vide, _serie(260)) is None


def test_reperer_trie_exclut_et_limite(monkeypatch):
    tickers = [f"T{i}" for i in range(7)]
    base = dict(cloture=100, veille=99.5, mm20=99.5, mm50=95, mm200=90, rsi=40, atr=2, perf_indice_3m=5,
                plus_haut_20=110, volume=1e6, volume_moyen_20=1e6)
    forces = {t: 10 + i for i, t in enumerate(tickers)}
    monkeypatch.setattr(strategie, "_indicateurs_titre", lambda hist, t, indice, jour: {**base, "perf_3m": 5 + forces[t]})
    hist = pd.DataFrame()
    candidats = reperer(hist, tickers, {}, date(2026, 10, 19), p, deja_pris={"T6"})
    assert [c["ticker"] for c in candidats] == ["T5", "T4", "T3", "T2", "T1"]
    assert all(c["configuration"] == "A" for c in candidats)
    assert candidats[0]["force_relative"] == 15
    assert {k: candidats[0][k] for k in ("entree", "stop", "objectif")} == niveaux(100, 2, p)


def test_historique_inclure_jour(monkeypatch):
    jours = pd.to_datetime(["2020-01-02", "2099-01-01"])
    df = pd.DataFrame({(champ, "AAA"): [1.0, 2.0] for champ in ("Close", "High", "Low", "Volume", "Open")},
                      index=jours)
    df.columns = pd.MultiIndex.from_tuples(df.columns)
    monkeypatch.setattr(commun.yf, "download", lambda *a, **k: df)
    assert len(commun.historique_journalier(["AAA"], cache=False)) == 1
    assert len(commun.historique_journalier(["AAA"], cache=False, inclure_jour=True)) == 2
