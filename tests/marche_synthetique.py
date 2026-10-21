"""Marché fictif reproductible (graine fixe) pour les tests de bout en bout du backtest : aucune donnée réelle."""
import numpy as np
import pandas as pd

from backtest import DEVISE
from commun import INDICE

TICKERS = [f"T{i:02d}" for i in range(12)]


def marche(seances=700, graine=7):
    """Cours journaliers de 12 titres, de QQQ et de l'euro, au format de yf.download (colonnes champ × symbole)."""
    alea = np.random.default_rng(graine)
    index = pd.bdate_range("2022-01-03", periods=seances)
    colonnes = {}
    for k, symbole in enumerate([INDICE, *TICKERS]):
        tendance = 0.0004 if symbole == INDICE else alea.uniform(-0.0002, 0.0012)
        vol = 0.011 if symbole == INDICE else alea.uniform(0.014, 0.026)
        rendements = tendance + vol * alea.standard_t(4, seances) / np.sqrt(2)
        cloture = 50 * (1 + k) * np.exp(np.cumsum(rendements))
        # Ouverture = clôture de la veille ± un écart : crée des gaps, y compris sous les stops
        ouverture = np.r_[cloture[0], cloture[:-1] * np.exp(vol * 0.4 * alea.standard_normal(seances - 1))]
        haut = np.maximum(ouverture, cloture) * np.exp(np.abs(vol * 0.6 * alea.standard_normal(seances)))
        bas = np.minimum(ouverture, cloture) * np.exp(-np.abs(vol * 0.6 * alea.standard_normal(seances)))
        volume = alea.integers(1_000_000, 3_000_000, seances).astype(float)
        for champ, valeurs in (("Open", ouverture), ("High", haut), ("Low", bas), ("Close", cloture),
                               ("Volume", volume)):
            colonnes[(champ, symbole)] = valeurs
    euro = 1.08 + np.cumsum(alea.normal(0, 0.003, seances))
    for champ in ("Open", "High", "Low", "Close", "Volume"):
        colonnes[(champ, DEVISE)] = euro if champ != "Volume" else np.zeros(seances)
    df = pd.DataFrame(colonnes, index=index)
    return df.sort_index(axis=1)
