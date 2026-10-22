"""Textes Telegram de la copie d'initiés.

Tout ce qui vient de Claude (description d'un achat) est nettoyé une seconde fois ici : liens retirés, nombres d'actions
retirés, textes bornés. Les prix, quantités et stops viennent toujours du code, jamais d'une description.
"""
import re

from court_terme.validation import CONTROLE, nettoyer
from inities.modele import delai

LONGUEUR_MAX = 600
PISTES_MAX = 3
# Un nombre d'actions écrit par Claude pourrait passer pour un ordre : on le retire (« 1000 », « 1 000 », « 2,500 »)
QUANTITE = re.compile(r"\b\d+(?:[ \u00a0\u202f,]\d{3})*\s*(?:actions?|shares?|stocks?)\b", re.IGNORECASE)


def nettoyer_champ(texte, une_ligne=False):
    """Texte venu de Claude : sans caractère de contrôle, lien ni nombre d'actions, borné à 600 caractères.

    Les caractères invisibles sont retirés avant la recherche d'un nombre d'actions : « 1000 ac<invisible>tions » ne passe pas.
    """
    texte = QUANTITE.sub("[quantité retirée]", CONTROLE.sub("", texte))
    return nettoyer(texte, longueur_max=LONGUEUR_MAX, une_ligne=une_ligne)


def _nombre(valeur):
    return f"{valeur:,.0f}".replace(",", " ")


def _pourcentage(valeur):
    return f"{valeur:+.1f} %"


def _tranche(op):
    if op.montant_min == op.montant_max:
        return f"≈ {_nombre(op.montant_min)} $"
    return f"{_nombre(op.montant_min)} – {_nombre(op.montant_max)} $"


def _qui(op):
    if op.famille == "politicien":
        chambre = "Sénat" if ":senat:" in op.personne_id else "Chambre des représentants"
        return f"{op.personne} (élu, {chambre})"
    if op.famille == "dirigeant":
        poste = op.details.get("poste")
        societe = op.details.get("societe")
        precision = ", ".join(x for x in (poste, societe) if x)
        return f"{op.personne} ({precision})" if precision else op.personne
    return f"{op.personne} (fonds)"


def _combien(op):
    texte = _tranche(op)
    if op.famille == "fonds" and op.details.get("poids_pct") is not None:
        texte += f", soit {op.details['poids_pct']:g} % de son portefeuille"
    return texte


def _bilan(bilan):
    if not bilan:
        return "Historique de leurs achats : indisponible"
    if bilan.get("r6") is not None and bilan.get("q6") is not None:
        r, q, duree = bilan["r6"], bilan["q6"], "6 mois"
    elif bilan.get("r3") is not None and bilan.get("q3") is not None:
        r, q, duree = bilan["r3"], bilan["q3"], "3 mois"
    else:
        return f"Historique : {bilan.get('achats', 0)} achats, pas encore mesurable contre QQQ"
    return (f"Historique : {bilan.get('achats', 0)} achats, copie {_pourcentage(r)} contre {_pourcentage(q)} "
            f"pour QQQ à {duree}")


def achat(op, description, bilan, compte, variation_pct):
    """Message d'un nouvel achat : les faits d'abord, puis la description de Claude si elle existe."""
    lignes = [f"🔎 NOUVEL ACHAT — {op.personne}", "",
              f"Qui : {_qui(op)}",
              f"Quoi : {op.ticker}",
              f"Quand : acheté le {op.date_operation.isoformat()}, publié le {op.date_publication.isoformat()}",
              f"Combien : {_combien(op)}",
              f"Délai de déclaration : {delai(op)} jours"]
    if op.famille == "dirigeant" and op.details.get("groupe"):
        lignes.append("Achat de groupe : au moins deux dirigeants de la société ont acheté en 30 jours")
    lignes.append("Variation depuis leur achat : " + ("indisponible" if variation_pct is None
                                                      else _pourcentage(variation_pct)))
    lignes.append("")
    lignes_faits = len(lignes)
    description = description if isinstance(description, dict) else {}
    for titre, cle in (("Contexte", "contexte"), ("Société", "societe"), ("Actualités", "actualites")):
        valeur = description.get(cle)
        valeur = nettoyer_champ(valeur, une_ligne=True) if isinstance(valeur, str) else ""
        if valeur:
            lignes.append(f"{titre} : {valeur}")
    pistes = description.get("pistes")
    pistes = [nettoyer_champ(p, une_ligne=True) for p in pistes if isinstance(p, str)] if isinstance(pistes, list) else []
    pistes = [p for p in pistes if p][:PISTES_MAX]
    if len(lignes) == lignes_faits and not pistes:
        lignes.append("(description indisponible)")
    if pistes:
        lignes += ["", "Pistes possibles (hypothèses de Claude, pas des faits) :"] + [f"• {p}" for p in pistes]
    lignes += ["", _bilan(bilan), f"Compte fictif : {compte}", "", "Pas un conseil financier."]
    return "\n".join(lignes)


def _actions(quantite):
    return f"{quantite:g} action" + ("" if quantite <= 1 else "s")


def achat_copie(ticker, quantite, prix, stop):
    return "\n".join([f"📥 ACHAT COPIÉ — {ticker}",
                      f"{_actions(quantite)} à environ {prix:.2f} $ (compte paper)",
                      f"Stop posé à {stop:.2f} $"])


def vente(ticker, raison, resultat_pct):
    resultat = "résultat n/d" if resultat_pct is None else f"résultat {_pourcentage(resultat_pct)}"
    return f"📤 VENTE — {ticker} : {raison} ({resultat})"


def alerte(texte):
    return f"⚠️ COPIE D'INITIÉS — {texte}"
