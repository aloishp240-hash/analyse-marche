"""Contrôle strict de ce que Claude transmet aux outils : ticker, justifications, notes, informations.

Claude peut avoir lu une page piégée : tout texte qui vient de lui est nettoyé (caractères de contrôle, liens) et
borné avant d'être enregistré ou envoyé. Le code décide seul des prix, des quantités et des refus : un texte ne peut
ni changer une règle, ni contourner un plafond, ni débloquer une entrée.
"""
import re

from court_terme.messages import RUBRIQUES

TICKER = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
LIEN = re.compile(r"(https?://|www\.)\S+", re.IGNORECASE)
CONTROLE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f​-‏‪-‮⁦-⁩]")
SIGNAL_MIN, SIGNAL_MAX = 200, 4000
FERMETURE_MIN, FERMETURE_MAX = 40, 2000
NOTE_MAX = 1500
RUBRIQUES_ATTENDUES = ("Technique", "Actualités", "Contexte", "Risque")


class TexteRefuse(ValueError):
    pass


def ticker(texte):
    valeur = texte.strip().upper()
    if not TICKER.fullmatch(valeur):
        raise TexteRefuse(f"ticker invalide : {texte[:20]!r}")
    return valeur


def nettoyer(texte, longueur_max=None, une_ligne=False):
    """Retire caractères de contrôle et caractères invisibles de direction, remplace les liens, borne la longueur."""
    texte = LIEN.sub("[lien retiré]", CONTROLE.sub("", texte))
    texte = " ".join(texte.split()) if une_ligne else "\n".join(" ".join(l.split()) for l in texte.splitlines()).strip()
    if longueur_max and len(texte) > longueur_max:
        texte = texte[:longueur_max - 1] + "…"
    return texte


def _longueur(texte, mini, maxi, quoi):
    if len(texte) < mini:
        raise TexteRefuse(f"{quoi} trop courte ({len(texte)} caractères, {mini} au moins)")
    if len(texte) > maxi:
        raise TexteRefuse(f"{quoi} trop longue ({len(texte)} caractères, {maxi} au plus)")


def justification_signal(texte):
    """Justification d'achat nettoyée, avec ses quatre rubriques ; lève TexteRefuse sinon."""
    texte = nettoyer(texte)
    _longueur(texte, SIGNAL_MIN, SIGNAL_MAX, "justification")
    presentes = set(RUBRIQUES.findall(texte))
    manquantes = [r for r in RUBRIQUES_ATTENDUES if r not in presentes]
    if manquantes:
        raise TexteRefuse("rubrique(s) manquante(s) : " + ", ".join(manquantes))
    return texte


def justification_fermeture(texte):
    texte = nettoyer(texte)
    _longueur(texte, FERMETURE_MIN, FERMETURE_MAX, "justification")
    return texte


def note(texte):
    return nettoyer(texte, NOTE_MAX)
