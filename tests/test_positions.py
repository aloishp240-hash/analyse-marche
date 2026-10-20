from datetime import date, datetime

import pandas as pd

import positions
from commun import NY


def multi(donnees, index):
    df = pd.DataFrame(donnees, index=index)
    df.columns = pd.MultiIndex.from_tuples(df.columns)
    return df


def test_doit_faire_bilan():
    vendredi = datetime(2026, 10, 23, 16, 20, tzinfo=NY)
    assert positions.doit_faire_bilan(vendredi, {})
    assert not positions.doit_faire_bilan(datetime(2026, 10, 22, 16, 20, tzinfo=NY), {})
    assert not positions.doit_faire_bilan(datetime(2026, 10, 23, 15, 0, tzinfo=NY), {})
    assert not positions.doit_faire_bilan(vendredi, {"dernier_bilan": "2026-10-23"})


def fausses_donnees(monkeypatch):
    jours = pd.to_datetime(["2026-10-16", "2026-10-19", "2026-10-20"])
    journalier = multi({(c, "AAA"): [10.0, 11.0, 99.0] for c in ("Open", "High", "Low", "Close")}, jours)
    heures = pd.DatetimeIndex(["2026-10-19 15:55", "2026-10-20 09:30", "2026-10-20 09:35", "2026-10-20 09:40"],
                              tz=NY)
    intraday = multi({("Open", "AAA"): [1, 100, 101, 102], ("High", "AAA"): [1, 101, 104, 103],
                      ("Low", "AAA"): [1, 99, 100, 98], ("Close", "AAA"): [1, 100.5, 102, 102.5]}, heures)
    monkeypatch.setattr(positions.yf, "download",
                        lambda *a, **k: intraday if k.get("interval") == "5m" else journalier)


def test_barres_du_jour(monkeypatch):
    fausses_donnees(monkeypatch)
    barres = positions.barres_du_jour(["AAA"], datetime(2026, 10, 20, 15, 0, tzinfo=NY))["AAA"]
    assert [b["jour"] for b in barres] == [date(2026, 10, 16), date(2026, 10, 19), date(2026, 10, 20)]
    assert all(b["terminee"] for b in barres[:2]) and barres[0]["cloture"] == 10
    assert barres[-1] == {"jour": date(2026, 10, 20), "ouverture": 100, "haut": 104, "bas": 98,
                          "cloture": 102.5, "terminee": False}


def test_barres_du_jour_apres_cloture(monkeypatch):
    fausses_donnees(monkeypatch)
    barres = positions.barres_du_jour(["AAA"], datetime(2026, 10, 20, 16, 5, tzinfo=NY))["AAA"]
    assert barres[-1]["terminee"]
