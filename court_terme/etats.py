"""Cycle de vie d'un titre du portefeuille fictif : identifiant unique, états, transitions permises, historique.

candidat → en_attente (ordre validé) → execute (position ouverte) → cloture (position vendue)
                                     ↘ annule / expire
L'état partiellement_execute est prévu pour un suivi plus fin, mais la simulation exécute un ordre en entier ou pas
du tout : aucun code ne le produit aujourd'hui.
"""
import uuid
from collections import Counter

CANDIDAT, EN_ATTENTE, PARTIEL, EXECUTE = "candidat", "en_attente", "partiellement_execute", "execute"
ANNULE, EXPIRE, CLOTURE = "annule", "expire", "cloture"
TRANSITIONS = {
    CANDIDAT: {EN_ATTENTE},
    EN_ATTENTE: {PARTIEL, EXECUTE, ANNULE, EXPIRE},
    PARTIEL: {EXECUTE, ANNULE, EXPIRE},
    EXECUTE: {CLOTURE},
    ANNULE: set(), EXPIRE: set(), CLOTURE: set(),
}
# Liste de l'état du portefeuille → états qu'un élément de cette liste peut avoir
LISTES = {"ordres": {EN_ATTENTE, PARTIEL}, "positions": {EXECUTE}, "fermees": {CLOTURE},
          "ordres_clos": {ANNULE, EXPIRE}}


class TransitionInterdite(ValueError):
    pass


def nouvel_identifiant(ticker, jour):
    return f"{jour}-{ticker}-{uuid.uuid4().hex[:6]}"


def etat_de(element):
    """État de l'élément ; un élément enregistré avant l'ajout des états est déduit de ses champs."""
    return element.get("etat") or (EXECUTE if "prix_entree" in element else EN_ATTENTE)


def noter(element, evenement, jour, **detail):
    """Ajoute une ligne à l'historique de l'élément ; une ligne identique déjà présente n'est pas doublée."""
    historique = element.setdefault("historique", [])
    ligne = {"jour": str(jour), "evenement": evenement, **detail}
    if ligne in historique:
        return False
    historique.append(ligne)
    return True


def passer(element, nouvel_etat, jour, **detail):
    """Change l'état de l'élément et l'inscrit dans son historique ; lève TransitionInterdite sinon."""
    actuel = etat_de(element)
    if nouvel_etat not in TRANSITIONS[actuel]:
        raise TransitionInterdite(f"{element.get('id', element['ticker'])} : {actuel} → {nouvel_etat} interdit")
    element["etat"] = nouvel_etat
    noter(element, nouvel_etat, jour, **detail)


def anomalies(etat, jour=None):
    """Incohérences de l'état enregistré (liste de textes, vide si tout va bien)."""
    problemes = []
    ids = Counter()
    for liste, permis in LISTES.items():
        for e in etat.get(liste, []):
            nom = e.get("id") or e.get("ticker", "?")
            if e.get("id"):
                ids[e["id"]] += 1
            if etat_de(e) not in permis:
                problemes.append(f"{nom} est « {etat_de(e)} » mais rangé dans {liste}")
            if liste in ("ordres", "positions"):
                if not e.get("quantite", 0) > 0:
                    problemes.append(f"{nom} : quantité nulle ou négative")
                if jour and e.get("traite_jusqu_au", "") > str(jour):
                    problemes.append(f"{nom} : suivi daté du {e['traite_jusqu_au']}, après aujourd'hui")
            try:
                if liste == "ordres" and not e["stop"] < e["entree"] < e["objectif"]:
                    problemes.append(f"{nom} : stop, entrée et objectif dans le désordre")
                if liste == "positions" and not e["stop"] <= e["prix_entree"] * 1.000001 < e["objectif"]:
                    problemes.append(f"{nom} : stop au-dessus de l'achat ou objectif sous l'achat")
            except (KeyError, TypeError):
                problemes.append(f"{nom} : prix manquants ou illisibles dans {liste}")
    problemes += [f"identifiant {i} présent {n} fois" for i, n in ids.items() if n > 1]
    suivis = Counter(e["ticker"] for e in etat.get("ordres", []) + etat.get("positions", []))
    problemes += [f"{t} suivi {n} fois à la fois" for t, n in suivis.items() if n > 1]
    return problemes
