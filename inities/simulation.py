"""Portefeuille fictif qui copie, séance par séance, les achats des personnes suivies, comparé à QQQ.

Rien ici ne regarde l'avenir : à la séance d'un jour, on n'utilise que les déclarations publiées avant ce jour et les
cours jusqu'à la clôture précédente. Seul le prix d'exécution (ouverture du jour, stop atteint) vient du jour même.

Choix de modélisation :
- une entrée n'a lieu qu'à la première séance qui suit la publication (même règle que `rendement_copie`) ; si le titre
  est déjà détenu, si les places manquent ou si une donnée manque, l'achat est écarté et noté dans `ignores` ;
- un achat d'élu ou de fonds est copié si la personne est suivie dans la sélection du 1er du mois de sa publication ;
  un achat de dirigeant qui passe `achat_retenu` l'est toujours ; tous doivent être dans l'univers ;
- la quantité se calcule sur la valeur du compte et sur la clôture de la veille ; si le cash ne suffit pas au prix
  d'ouverture (écart de cours, frais), on réduit la quantité ;
- une vente de la personne copiée sur le titre, publiée à partir du jour de l'entrée, déclenche la sortie à
  l'ouverture de la première séance suivante ; aucune source n'est inventée, voir `ventes` ci-dessous ;
- sorties dans une séance : (1) vente publiée ou durée maximale, à l'ouverture ; (2) ouverture sous le stop, à
  l'ouverture ; puis les entrées, à l'ouverture ; (3) plus bas sous le stop, au prix du stop, y compris le jour de
  l'entrée. La sortie sur stop de (3) a lieu après les entrées : elle ne libère ni place ni cash avant le lendemain ;
- une position dont les cours manquent un jour est gardée et valorisée au dernier cours connu ;
- une position encore ouverte à la fin est comptée au dernier cours de clôture (raison `ouverte`, sans frais de
  sortie).
"""
import math
from bisect import bisect_left
from collections import defaultdict
from datetime import date

import pandas as pd

from inities.dirigeants import achat_retenu, groupes
from inities.mesure import dans_univers, selectionner

FRAIS_FIXE_EUR = 1.0
FRAIS_PROPORTIONNEL = 0.001
EURUSD_SECOURS = 1.0  # seulement quand la série EURUSD est vide


def quantite(valeur_compte, cours, p):
    """Actions entières pour part_par_achat_pct du compte ; 1 action si ce n'est pas assez pour une seule et que
    son cours ne dépasse pas part_max_une_action_pct du compte, sinon 0."""
    if not (valeur_compte > 0 and cours > 0) or not math.isfinite(cours):
        return 0
    nombre = math.floor(valeur_compte * p["part_par_achat_pct"] / 100 / cours)
    if nombre == 0 and cours <= valeur_compte * p["part_max_une_action_pct"] / 100:
        return 1
    return nombre


def frais_nuls(montant, jour):
    return 0.0


def frais_courtier(eurusd):
    """Fonction (montant, jour) -> 1 € converti au dernier EURUSD connu jusqu'à jour + 0,1 % du montant."""
    try:
        taux = pd.Series(eurusd, dtype=float).dropna().sort_index()
        taux = taux[taux > 0]
    except (TypeError, ValueError):
        taux = pd.Series(dtype=float)

    def frais(montant, jour):
        cours = EURUSD_SECOURS if taux.empty else float(taux.iloc[0])  # avant le premier point : le premier connu
        try:
            i = taux.index.searchsorted(pd.Timestamp(jour), side="right") - 1
            if i >= 0:
                cours = float(taux.iloc[i])
        except (TypeError, ValueError):
            pass
        return FRAIS_FIXE_EUR * cours + FRAIS_PROPORTIONNEL * abs(montant)
    return frais


def _prix(df, jour, colonne):
    """Prix strictement positif et fini de la colonne ce jour-là, ou None."""
    if df is None:
        return None
    try:
        valeur = float(df.at[jour, colonne])
    except (KeyError, ValueError, TypeError):
        return None
    return valeur if math.isfinite(valeur) and valeur > 0 else None


def _cloture_veille(df, jour):
    """Clôture de la dernière ligne strictement avant jour, ou None."""
    if df is None or df.empty:
        return None
    i = df.index.searchsorted(jour) - 1
    return _prix(df, df.index[i], "Close") if i >= 0 else None


def _seances(qqq, debut, fin):
    if qqq is None or getattr(qqq, "empty", True):
        return pd.DatetimeIndex([])
    index = qqq.index
    return index[(index >= pd.Timestamp(debut)) & (index <= pd.Timestamp(fin))]


def _premiers_du_mois(debut, fin):
    jour = date(debut.year, debut.month, 1)
    if jour < debut:
        jour = (pd.Timestamp(jour) + pd.DateOffset(months=1)).date()
    while jour <= fin:
        yield jour
        jour = (pd.Timestamp(jour) + pd.DateOffset(months=1)).date()


def _suivis(ops, cours, qqq, actions, debut, fin, p):
    """{1er du mois: personne_id suivies}, la sélection du 1er n'utilisant que les cours d'avant ce jour."""
    suivis = {}
    for premier in _premiers_du_mois(debut, fin):
        limite = pd.Timestamp(premier)
        avant = {t: df.iloc[:df.index.searchsorted(limite)] for t, df in cours.items() if df is not None}
        lignes = selectionner(ops, avant, qqq.iloc[:qqq.index.searchsorted(limite)], actions, premier, p)
        suivis[premier] = sorted(ligne["personne_id"] for ligne in lignes if ligne["retenu"])
    return suivis


def _ordre_du_jour(ops, seances, debut):
    """Achats regroupés par indice de la séance où on les copie : la première strictement après la publication.
    Un achat publié avant debut est écarté : il aurait dû être copié avant le début de la simulation."""
    jours = pd.DatetimeIndex(seances)
    par_seance = defaultdict(list)
    for op in ops:
        if op.sens != "achat":
            continue
        i = int(jours.searchsorted(pd.Timestamp(op.date_publication), side="right"))
        if i >= len(jours):
            continue
        if i > 0 or op.date_publication >= debut:
            par_seance[i].append(op)
    return par_seance


def _candidats(achats, suivis, retenus_connus, cours, actions, p):
    """(achats copiables ce jour, triés par priorité : publication la plus ancienne, puis achats groupés de
    dirigeants, puis montant le plus élevé ; achats écartés car hors univers ou sans cours)."""
    candidats, hors_univers = [], []
    for op in achats:
        if achat_retenu(op, p):
            copiable = True
        elif op.famille in ("politicien", "fonds"):
            mois = date(op.date_publication.year, op.date_publication.month, 1)
            copiable = op.personne_id in suivis.get(mois, ()) and (
                op.famille != "politicien" or op.montant_min >= p["politicien_montant_min"])
        else:
            copiable = False
        if copiable:
            (candidats if dans_univers(op, cours, actions, p) else hors_univers).append(op)
    en_groupe = set()
    if len(candidats) > 1 and any(achat_retenu(op, p) for op in candidats):
        en_groupe = groupes(retenus_connus, p)
    candidats.sort(key=lambda op: (op.date_publication, op.source_id not in en_groupe, -op.montant_min, op.source_id))
    return candidats, hors_univers


def simuler(ops, cours, qqq, actions, debut, fin, p, frais):
    """Portefeuille de p["capital_initial"] $ qui copie les achats, séance par séance, de debut à fin.

    Le 1er de chaque mois de [debut, fin], `selectionner` désigne les personnes suivies ; un dirigeant qui passe
    `achat_retenu` est copié sans être suivi. `frais(montant, jour)` donne les frais d'un ordre.

    Renvoie trades (une ligne par position : ticker, personne, entree, prix_entree, sortie, prix_sortie, raison,
    quantite, resultat_usd, frais), valeur (clôture de chaque séance), final, rendement_pct, qqq_pct (QQQ de
    l'ouverture de la première séance à la clôture de la dernière), ignores (ticker, personne, date, source_id,
    motif) et suivis ({1er du mois en ISO: personne_id}).

    Les ventes viennent des opérations de sens "vente" de la même personne_id : ventes de la Chambre et du Sénat,
    ventes (code S) d'un Form 4, et pour un fonds le titre absent du 13F suivant.
    """
    cours = cours or {}
    seances = _seances(qqq, debut, fin)
    capital = float(p["capital_initial"])
    trades, ignores, valeur = [], [], {}
    suivis = _suivis(ops, cours, qqq, actions, debut, fin, p) if len(seances) else {}
    par_seance = _ordre_du_jour(ops, seances, debut)
    ventes = defaultdict(list)
    for op in ops:
        if op.sens == "vente":
            ventes[(op.personne_id, op.ticker)].append(op.date_publication)
    for dates in ventes.values():
        dates.sort()
    retenus = sorted((op for op in ops if achat_retenu(op, p)), key=lambda op: op.date_publication)
    dates_retenus = [op.date_publication for op in retenus]
    cash, valeur_veille, positions = capital, capital, {}

    def sortir(position, prix, jour, raison):
        nonlocal cash
        montant = prix * position["quantite"]
        sortie_frais = frais(montant, jour)
        cash += montant - sortie_frais
        total_frais = position["frais"] + sortie_frais
        trades.append({
            "ticker": position["ticker"], "personne": position["personne"], "entree": position["entree"],
            "prix_entree": position["prix_entree"], "sortie": jour, "prix_sortie": prix, "raison": raison,
            "quantite": position["quantite"], "resultat_usd": (prix - position["prix_entree"]) * position["quantite"]
            - total_frais, "frais": total_frais})
        del positions[position["ticker"]]

    def ignorer(op, jour, motif):
        ignores.append({"ticker": op.ticker, "personne": op.personne, "date": jour, "source_id": op.source_id,
                        "motif": motif})

    for i, jour in enumerate(seances):
        aujourd_hui = jour.date()
        for ticker in list(positions):
            position = positions[ticker]
            ouverture = _prix(cours.get(ticker), jour, "Open")
            if ouverture is None:
                continue
            dates = ventes.get((position["personne_id"], ticker), [])
            j = bisect_left(dates, position["entree"])
            if j < len(dates) and dates[j] < aujourd_hui:
                sortir(position, ouverture, aujourd_hui, "vente")
            elif aujourd_hui >= position["echeance"]:
                sortir(position, ouverture, aujourd_hui, "6 mois")
            elif ouverture <= position["stop"]:
                sortir(position, ouverture, aujourd_hui, "stop")

        achats = par_seance.get(i, [])
        connus = retenus[:bisect_left(dates_retenus, aujourd_hui)] if achats else []
        candidats, hors_univers = _candidats(achats, suivis, connus, cours, actions, p)
        for op in hors_univers:
            ignorer(op, aujourd_hui, "hors univers ou sans cours")
        for op in candidats:
            df = cours.get(op.ticker)
            if op.ticker in positions:
                ignorer(op, aujourd_hui, "titre déjà détenu")
                continue
            if len(positions) >= p["positions_max"]:
                ignorer(op, aujourd_hui, f"plafond de {p['positions_max']} positions atteint")
                continue
            ouverture, veille = _prix(df, jour, "Open"), _cloture_veille(df, jour)
            if ouverture is None:
                ignorer(op, aujourd_hui, "pas de cours à l'ouverture")
                continue
            if veille is None:
                ignorer(op, aujourd_hui, "pas de clôture la veille pour dimensionner l'ordre")
                continue
            nombre = quantite(valeur_veille, veille, p)
            if nombre == 0:
                ignorer(op, aujourd_hui, f"une action coûte plus de {p['part_max_une_action_pct']} % du compte")
                continue
            nombre = min(nombre, int(cash // ouverture))
            while nombre > 0 and nombre * ouverture + frais(nombre * ouverture, aujourd_hui) > cash:
                nombre -= 1
            if nombre == 0:
                ignorer(op, aujourd_hui, "liquidités insuffisantes")
                continue
            entree_frais = frais(nombre * ouverture, aujourd_hui)
            cash -= nombre * ouverture + entree_frais
            positions[op.ticker] = {
                "ticker": op.ticker, "personne": op.personne, "personne_id": op.personne_id,
                "entree": aujourd_hui, "prix_entree": ouverture, "quantite": nombre, "frais": entree_frais,
                "stop": ouverture * (1 - p["stop_pct"] / 100), "dernier": ouverture,
                "echeance": (pd.Timestamp(aujourd_hui) + pd.DateOffset(months=p["duree_max_mois"])).date()}

        for position in list(positions.values()):
            bas = _prix(cours.get(position["ticker"]), jour, "Low")
            if bas is not None and bas <= position["stop"]:
                sortir(position, position["stop"], aujourd_hui, "stop")

        for position in positions.values():
            cloture = _prix(cours.get(position["ticker"]), jour, "Close")
            if cloture is not None:
                position["dernier"] = cloture
        valeur_veille = cash + sum(pos["quantite"] * pos["dernier"] for pos in positions.values())
        valeur[jour] = valeur_veille

    for position in list(positions.values()):
        resultat = (position["dernier"] - position["prix_entree"]) * position["quantite"] - position["frais"]
        trades.append({
            "ticker": position["ticker"], "personne": position["personne"], "entree": position["entree"],
            "prix_entree": position["prix_entree"], "sortie": seances[-1].date(), "prix_sortie": position["dernier"],
            "raison": "ouverte", "quantite": position["quantite"], "resultat_usd": resultat,
            "frais": position["frais"]})
    trades.sort(key=lambda t: (t["entree"], t["ticker"]))
    serie = pd.Series(valeur, dtype=float)
    final = float(serie.iloc[-1]) if len(serie) else capital
    qqq_pct = None
    if len(seances):
        ouverture, cloture = _prix(qqq, seances[0], "Open"), _prix(qqq, seances[-1], "Close")
        if ouverture is not None and cloture is not None:
            qqq_pct = (cloture / ouverture - 1) * 100
    return {"trades": trades, "valeur": serie, "final": final,
            "rendement_pct": (final / capital - 1) * 100 if capital > 0 else None, "qqq_pct": qqq_pct,
            "ignores": ignores, "suivis": {premier.isoformat(): ids for premier, ids in suivis.items()}}
