"""Ce qu'aurait rapporté la copie des achats de chaque personne, comparé à QQQ, et choix de qui copier.

Rien ici ne regarde l'avenir : un bilan ne compte que les copies dont la séance de sortie précède date_limite, et
l'univers n'utilise que des cours antérieurs à la publication.
"""
import math

import pandas as pd

from inities.cours import SEANCES_3M, SEANCES_6M
from inities.dirigeants import achat_retenu
from inities.modele import delai

SEANCES_UNIVERS = 20


def _nombre(valeur):
    """Nombre fini, ou None."""
    try:
        valeur = float(valeur)
    except (TypeError, ValueError):
        return None
    return valeur if math.isfinite(valeur) else None


def _valeur(df, jour, colonne):
    try:
        return _nombre(df.at[jour, colonne])
    except (KeyError, ValueError, AttributeError):
        return None


def _variation(entree, sortie):
    if entree is None or sortie is None or entree <= 0:
        return None
    return (sortie / entree - 1) * 100


def rendement_copie(op, cours, qqq, seances, date_limite):
    """(rendement du titre, rendement de QQQ) en %, entre l'ouverture de la première séance qui suit la
    publication et la clôture `seances` séances plus tard ; None si cette sortie n'est pas avant date_limite
    ou si une donnée manque."""
    df = cours.get(op.ticker)
    if df is None or qqq is None or df.empty:
        return None
    entree = df.index.searchsorted(pd.Timestamp(op.date_publication), side="right")
    sortie = entree + seances
    if sortie >= len(df):
        return None
    jour_entree, jour_sortie = df.index[entree], df.index[sortie]
    if jour_sortie.date() >= date_limite:
        return None
    titre = _variation(_valeur(df, jour_entree, "Open"), _valeur(df, jour_sortie, "Close"))
    reference = _variation(_valeur(qqq, jour_entree, "Open"), _valeur(qqq, jour_sortie, "Close"))
    if titre is None or reference is None:
        return None
    return titre, reference


def dans_univers(op, cours, actions, p):
    """Capitalisation (clôture de la veille de la publication × actions) et volume moyen en dollars des 20 séances
    avant la publication au-dessus des seuils ; False si une donnée manque."""
    df, nombre = cours.get(op.ticker), _nombre(actions.get(op.ticker))
    if df is None or nombre is None or nombre <= 0:
        return False
    avant = df[df.index < pd.Timestamp(op.date_publication)].tail(SEANCES_UNIVERS)
    if len(avant) < SEANCES_UNIVERS or not {"Close", "Volume"} <= set(avant.columns):
        return False
    dollars = avant["Close"] * avant["Volume"]
    cloture = _nombre(avant["Close"].iloc[-1])
    if cloture is None or cloture <= 0 or not dollars.notna().all():
        return False
    return cloture * nombre >= p["capitalisation_min"] and dollars.mean() >= p["volume_dollars_min"]


def _moyenne(valeurs):
    return sum(valeurs) / len(valeurs) if valeurs else None


def bilan(ops, cours, qqq, date_limite):
    """Résumé des achats de ops : nombre, délai moyen, dernier achat publié, rendements moyens à 3 et 6 mois et
    QQQ sur les mêmes dates. `mesures` garde (titre, QQQ) de chaque copie, à 6 mois sinon à 3 mois."""
    achats = [op for op in ops if op.sens == "achat"]
    m3 = [rendement_copie(op, cours, qqq, SEANCES_3M, date_limite) for op in achats]
    m6 = [rendement_copie(op, cours, qqq, SEANCES_6M, date_limite) for op in achats]
    mesures = [six or trois for trois, six in zip(m3, m6) if six or trois]
    m3, m6 = [m for m in m3 if m], [m for m in m6 if m]
    return {
        "achats": len(achats),
        "delai_moyen": _moyenne([delai(op) for op in achats]),
        "dernier_achat": max((op.date_publication for op in achats), default=None),
        "r3": _moyenne([m[0] for m in m3]), "q3": _moyenne([m[1] for m in m3]),
        "r6": _moyenne([m[0] for m in m6]), "q6": _moyenne([m[1] for m in m6]),
        "mesures": mesures,
        "r": _moyenne([m[0] for m in mesures]), "q": _moyenne([m[1] for m in mesures]),
        "part_battu": _moyenne([titre > reference for titre, reference in mesures]),
    }


def bat_qqq(b):
    """Rendement moyen supérieur à celui de QQQ et QQQ battu dans plus de la moitié des cas, sur les mêmes copies
    (`mesures` : 6 mois, sinon 3 mois). Sans les clés r et q, repli sur r6/q6 puis r3/q3."""
    if "r" in b and "q" in b:
        r, q = b["r"], b["q"]
    else:
        r, q = (b["r6"], b["q6"]) if b["r6"] is not None else (b["r3"], b["q3"])
    part = b["part_battu"]
    return r is not None and q is not None and part is not None and r > q and part > 0.5


def _pluriel(nombre, singulier, pluriel):
    return f"{nombre} {singulier if nombre <= 1 else pluriel}"


def _motif(famille, b, limite_recent, date_choix, p):
    """Premier critère manqué, ou « retenu »."""
    if famille == "politicien":
        minimum, nom = p["politicien_achats_min"], ("achat", "achats")
    else:
        minimum, nom = p["fonds_positions_min"], ("nouvelle position", "nouvelles positions")
    if b["achats"] < minimum:
        return f"{_pluriel(b['achats'], *nom)}, il en faut {minimum}"
    if b["dernier_achat"] < limite_recent:
        return (f"dernier achat publié le {b['dernier_achat'].isoformat()}, il en faut un depuis "
                f"{limite_recent.isoformat()}")
    if famille == "politicien" and not b["delai_moyen"] < p["politicien_delai_max_j"]:
        return f"délai moyen de {b['delai_moyen']:.0f} jours, il doit être sous {p['politicien_delai_max_j']}"
    if not b["mesures"]:
        return "aucune copie mesurable pour l'instant"
    if not bat_qqq(b):
        return "ne bat pas QQQ"
    return "retenu"


def selectionner(ops, cours, qqq, actions, date_choix, p):
    """Une ligne par politicien et par fonds, avec les critères appliqués à la date de choix.

    Seuls comptent les achats publiés dans les fenetre_mois mois avant date_choix (jour du choix exclu), dans
    l'univers, et pour les élus d'au moins politicien_montant_min. Chaque bilan s'arrête à date_choix."""
    debut = (pd.Timestamp(date_choix) - pd.DateOffset(months=p["fenetre_mois"])).date()
    limite_recent = (pd.Timestamp(date_choix) - pd.DateOffset(months=p["recent_mois"])).date()
    personnes = {}
    for op in ops:
        if (op.famille not in ("politicien", "fonds") or op.sens != "achat"
                or not debut <= op.date_publication < date_choix
                or (op.famille == "politicien" and op.montant_min < p["politicien_montant_min"])
                or not dans_univers(op, cours, actions, p)):
            continue
        personnes.setdefault(op.personne_id, []).append(op)
    lignes = []
    for personne_id, achats in sorted(personnes.items()):
        b = bilan(achats, cours, qqq, date_choix)
        motif = _motif(achats[0].famille, b, limite_recent, date_choix, p)
        lignes.append({"personne_id": personne_id, "personne": achats[0].personne, "famille": achats[0].famille,
                       "bilan": b, "retenu": motif == "retenu", "motif": motif})
    return lignes


def bilan_dirigeants(ops, cours, qqq, actions, date_limite, p):
    """Bilan des achats de dirigeants qui passent le filtre et l'univers, publiés avant date_limite."""
    retenus = [op for op in ops if achat_retenu(op, p) and op.date_publication < date_limite
               and dans_univers(op, cours, actions, p)]
    return bilan(retenus, cours, qqq, date_limite)
