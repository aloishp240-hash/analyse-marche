"""Le préréglage reference doit redonner exactement les trades du commit 04973f7 sur un marché synthétique."""
import json
from pathlib import Path

import pytest

import backtest
from commun import charger_config
from court_terme.preglages import parametres
from tests.marche_synthetique import TICKERS, marche

ATTENDU = json.loads((Path(__file__).parent / "golden_reference.json").read_text())
CHAMPS = ("ticker", "date_signal", "date_entree", "prix_entree", "quantite", "stop_initial", "objectif",
          "date_sortie", "prix_sortie", "motif", "resultat_eur")


def simuler(nom):
    df = marche()
    industries = {t: f"I{int(t[1:]) % 4}" for t in TICKERS}
    return backtest.simuler(df, df["Close"][backtest.DEVISE], TICKERS, industries, {},
                            parametres(charger_config(), nom), backtest.FENETRE, len(df))


def test_reference_identique_au_commit_04973f7():
    etat, compte, _ = simuler("reference")
    assert dict(sorted(compte.items())) == ATTENDU["compte"]
    fermees = [{c: f[c] for c in CHAMPS} for f in etat["fermees"]]
    assert len(fermees) == len(ATTENDU["fermees"])
    for obtenu, attendu in zip(fermees, ATTENDU["fermees"]):
        for c in CHAMPS:
            if isinstance(attendu[c], float):
                assert obtenu[c] == pytest.approx(attendu[c], abs=1e-6), (attendu["ticker"], c)
            else:
                assert obtenu[c] == attendu[c], (attendu["ticker"], c)
    ouvertes = [{c: x.get(c) for c in ("ticker", "date_entree", "prix_entree", "quantite")} for x in etat["positions"]]
    assert ouvertes == pytest.approx(ATTENDU["ouvertes"]) if ouvertes else ouvertes == ATTENDU["ouvertes"]


def test_amelioree_tourne_et_compte_les_frais():
    etat, compte, _ = simuler("amelioree")
    assert all(f["frais_eur"] > 0 for f in etat["fermees"])
    assert all(f["resultat_eur"] < f["resultat_brut_eur"] for f in etat["fermees"])
