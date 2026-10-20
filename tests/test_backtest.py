from datetime import date

import pandas as pd

import backtest


def test_periodes_d_un_mois_consecutives():
    index = pd.bdate_range("2026-06-01", "2026-10-20")
    bornes = backtest.periodes(index, 3)
    assert [(index[d].date(), index[f - 1].date()) for d, f in bornes] == [
        (date(2026, 7, 20), date(2026, 8, 18)),
        (date(2026, 8, 19), date(2026, 9, 18)),
        (date(2026, 9, 21), date(2026, 10, 20)),
    ]
    assert bornes[-1][1] == len(index)


def test_afficher_sans_gain_avec_position_ouverte(capsys):
    index = pd.bdate_range("2026-10-15", periods=3)
    colonnes = pd.MultiIndex.from_tuples([("Close", "QQQ"), ("Close", "AAA"), ("Close", backtest.DEVISE)])
    df = pd.DataFrame([[100, 10, 1.0], [101, 11, 1.0], [102, 12, 1.0]], index=index, columns=colonnes)
    taux = df["Close"][backtest.DEVISE]
    etat = {"capital_initial": 3000, "fermees": [{"resultat_eur": -20.0}],
            "positions": [{"ticker": "AAA", "prix_entree": 10.0, "quantite": 5}]}
    backtest.afficher(df, taux, etat, backtest.Counter(), 1, 3, {"capital_initial": 3000}, "A")
    sortie = capsys.readouterr().out
    assert "gain moyen —" in sortie
    assert "latent +10.00 €" in sortie and "valeur 2990.00 €" in sortie


def petit_df():
    index = pd.bdate_range("2026-10-15", periods=3)
    colonnes = pd.MultiIndex.from_tuples([("Close", "QQQ"), ("Close", "AAA"), ("Close", backtest.DEVISE)])
    return pd.DataFrame([[100, 10, 1.0], [101, 11, 1.0], [102, 12, 1.0]], index=index, columns=colonnes)


def test_valeur_avec_latent():
    df = petit_df()
    etat = {"capital_initial": 3000, "fermees": [{"resultat_eur": -20.0}],
            "positions": [{"ticker": "AAA", "prix_entree": 10.0, "quantite": 5}]}
    assert backtest.valeur(df, df["Close"][backtest.DEVISE], etat, 1) == 2985.0


def test_rapport_contient_les_positions(tmp_path):
    periode = {"debut": "2026-10-15", "fin": "2026-10-19", "courbe": [],
               "fermees": [{"ticker": "AAA", "motif": "stop", "justification": "</script><script>alert(1)"}]}
    chemin = backtest.ecrire_rapport([periode], "A", tmp_path / "backtest.html")
    html = chemin.read_text()
    assert '"ticker": "AAA"' in html and "__DONNEES__" not in html
    assert "</script><script>alert" not in html
