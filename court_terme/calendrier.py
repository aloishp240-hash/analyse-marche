"""Jours fériés de la Bourse de New York, calculés sans dépendance (règles NYSE en vigueur depuis 2022)."""
from datetime import date, timedelta
from functools import lru_cache

import numpy as np


def _paques(annee):
    """Dimanche de Pâques (calendrier grégorien, algorithme de Meeus)."""
    a, b, c = annee % 19, annee // 100, annee % 100
    d, e = b // 4, b % 4
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 19 * l) // 433
    mois = (h + l - 7 * m + 90) // 25
    return date(annee, mois, (h + l - 7 * m + 33 * mois + 19) % 32)


def _neme(annee, mois, jour_semaine, n):
    """n-ième jour_semaine (0 = lundi) du mois ; n = -1 pour le dernier."""
    if n > 0:
        premier = date(annee, mois, 1)
        return premier + timedelta(days=(jour_semaine - premier.weekday()) % 7 + 7 * (n - 1))
    dernier = date(annee + mois // 12, mois % 12 + 1, 1) - timedelta(days=1)
    return dernier - timedelta(days=(dernier.weekday() - jour_semaine) % 7)


def _observe(jour):
    """Férié un samedi → vendredi chômé ; un dimanche → lundi chômé."""
    if jour.weekday() == 5:
        return jour - timedelta(days=1)
    if jour.weekday() == 6:
        return jour + timedelta(days=1)
    return jour


@lru_cache(maxsize=None)
def jours_feries(annee):
    feries = {
        _neme(annee, 1, 0, 3),               # Martin Luther King
        _neme(annee, 2, 0, 3),               # Presidents' Day
        _paques(annee) - timedelta(days=2),  # Vendredi saint
        _neme(annee, 5, 0, -1),              # Memorial Day
        _observe(date(annee, 7, 4)),         # Fête nationale
        _neme(annee, 9, 0, 1),               # Labor Day
        _neme(annee, 11, 3, 4),              # Thanksgiving
        _observe(date(annee, 12, 25)),       # Noël
    }
    if annee >= 2022:
        feries.add(_observe(date(annee, 6, 19)))  # Juneteenth
    # Le NYSE ne chôme pas le vendredi 31 décembre quand le 1er janvier tombe un samedi
    if date(annee, 1, 1).weekday() != 5:
        feries.add(_observe(date(annee, 1, 1)))
    return frozenset(feries)


def seance(jour):
    return jour.weekday() < 5 and jour not in jours_feries(jour.year)


def seances_entre(debut, fin):
    """Séances dans ]debut, fin]."""
    if fin <= debut:
        return 0
    feries = [j for annee in range(debut.year, fin.year + 1) for j in jours_feries(annee)]
    return int(np.busday_count(debut + timedelta(days=1), fin + timedelta(days=1), holidays=feries))
