#!/usr/bin/env python3
"""Copie d'initiés, passage du matin : lit les nouvelles déclarations, copie les achats sur le compte Alpaca paper
(fictif) si `copie.alpaca_actif` vaut vrai, puis dépose les messages Telegram dans la boîte d'envoi.

Usage : python copie_matin.py [--force] [--dry-run]
  --force    ignore l'heure et le « déjà passé aujourd'hui »
  --dry-run  affiche les messages ; ni Claude, ni Alpaca, aucune écriture dans copie/, aucun dépôt dans la boîte

Le passage n'a lieu qu'une fois par jour ouvré, à partir de `copie.heure_paris` (heure de Paris). `copie/dernier_lancement.json`
({"jour": "AAAA-MM-JJ"}) garde le dernier passage complet. `copie/vus.json` est écrit en dernier : tout ce qui précède
(copie, messages) est refait au passage suivant si le script s'arrête avant. Les secrets (SEC_CONTACT, ALPACA_*,
CLAUDE_CODE_OAUTH_TOKEN) ne sont jamais affichés ni écrits.
"""
import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta

import pandas as pd

from commun import PARIS, charger_config
from court_terme import alpaca, boite
from court_terme.alpaca import ErreurAlpaca
from inities import copie_alpaca, cours, messages, nouveautes, redaction, sec
from inities.modele import depuis_dict, vers_dict
from inities.nouveautes import ErreurVus
from inities.sec import ErreurSec

DOSSIER = copie_alpaca.DOSSIER
SUIVIS = DOSSIER / "suivis.json"
BILANS = DOSSIER / "bilans.json"
DERNIER_LANCEMENT = DOSSIER / "dernier_lancement.json"
VENTES_EN_ATTENTE = DOSSIER / "ventes_en_attente.json"
DOSSIER_COURS = nouveautes.DOSSIER_CACHE / "cours"
JOURS_COURS = 10  # recul pour trouver le dernier cours
COMPTE_DESACTIVE = "copie désactivée"
MOTIF_DESACTIVE = "copie désactivée"
MOTIF_SUSPENDUE = "copie suspendue"
MOTIF_ESSAI = "essai à vide, aucun ordre passé"


class ErreurFichier(Exception):
    """Un fichier de copie/ existe mais n'est pas lisible : on s'arrête au lieu de repartir de zéro en silence."""


def doit_lancer(maintenant_paris, dernier_passage, heure_paris="08:00"):
    """Jour ouvré, heure de Paris ≥ heure_paris, et pas déjà passé ce jour."""
    if maintenant_paris.weekday() >= 5:
        return False
    heure, minute = (int(x) for x in heure_paris.split(":"))
    if (maintenant_paris.hour, maintenant_paris.minute) < (heure, minute):
        return False
    return dernier_passage != maintenant_paris.date()


def _lire_json(chemin, defaut):
    try:
        texte = chemin.read_text()
    except FileNotFoundError:
        return defaut
    try:
        return json.loads(texte)
    except ValueError:
        raise ErreurFichier(f"{chemin.name} illisible (JSON invalide) : à corriger ou à supprimer à la main") from None


def _ecrire_json(chemin, donnees):
    """Écriture atomique : fichier temporaire puis os.replace."""
    chemin.parent.mkdir(parents=True, exist_ok=True)
    temporaire = chemin.with_name(chemin.name + ".tmp")
    try:
        temporaire.write_text(json.dumps(donnees, indent=1, ensure_ascii=False) + "\n")
        os.replace(temporaire, chemin)
    finally:
        temporaire.unlink(missing_ok=True)


def _dernier_passage():
    """Date du dernier passage complet, ou None si le fichier manque ou est illisible (le passage est alors refait)."""
    try:
        return date.fromisoformat(_lire_json(DERNIER_LANCEMENT, {}).get("jour", ""))
    except (ErreurFichier, ValueError, AttributeError, TypeError):
        return None


def _lire_ventes_en_attente():
    brut = _lire_json(VENTES_EN_ATTENTE, [])
    try:
        return [depuis_dict(d) for d in brut]
    except (TypeError, KeyError, ValueError):
        raise ErreurFichier(f"{VENTES_EN_ATTENTE.name} illisible (opérations invalides) : "
                            "à corriger ou à supprimer à la main") from None


def _fusionner_ventes(en_attente, nouvelles):
    vues, ventes = set(), []
    for vente in en_attente + nouvelles:
        if vente.source_id not in vues:
            vues.add(vente.source_id)
            ventes.append(vente)
    return ventes


def _charger_cours(achats, aujourd_hui):
    """({ticker: dernier Close sur les 10 derniers jours}, {source_id: variation en %}), ou deux dict vides si Yahoo
    ne répond pas. La variation compare le dernier Close à celui du jour de l'opération, ou du premier jour après."""
    if not achats:
        return {}, {}, None
    recent = aujourd_hui - timedelta(days=JOURS_COURS)
    debut = min([recent] + [op.date_operation for op in achats])
    try:
        prix = cours.charger(sorted({op.ticker for op in achats}), debut, aujourd_hui, DOSSIER_COURS)
    except Exception as erreur:
        return {}, {}, f"Cours : indisponibles ({type(erreur).__name__})"
    derniers, variations = {}, {}
    for ticker, df in prix.items():
        fermetures = df["Close"].dropna()
        if not fermetures.empty and fermetures.index[-1] >= pd.Timestamp(recent):
            derniers[ticker] = float(fermetures.iloc[-1])
    for op in achats:
        fermetures = prix[op.ticker]["Close"].dropna() if op.ticker in prix else None
        if fermetures is None or op.ticker not in derniers:
            continue
        depuis = fermetures[fermetures.index >= pd.Timestamp(op.date_operation)]
        if not depuis.empty and float(depuis.iloc[0]) > 0:
            variations[op.source_id] = (derniers[op.ticker] / float(depuis.iloc[0]) - 1) * 100
    return derniers, variations, None


def _compte(client):
    """Valeur du compte paper en texte, comme copie_alpaca la lit (equity, sinon portfolio_value)."""
    try:
        compte = client.compte()
        valeur = float(compte.get("equity") or compte.get("portfolio_value"))
    except (ErreurAlpaca, TypeError, ValueError, AttributeError):
        return "indisponible"
    return f"{valeur:,.2f}".replace(",", " ").replace(".", ",") + " $ (compte paper)"


def _motifs(achats, signales, etat, actif, dry_run):
    """{source_id: motif} des achats seulement signalés."""
    if not actif:
        return {op.source_id: MOTIF_DESACTIVE for op in achats}
    if dry_run:
        return {op.source_id: MOTIF_ESSAI for op in achats}
    motifs = {op.source_id: motif for op, motif in signales}
    copies = {pos["source_id"] for pos in etat["positions"] + etat["historique"]}
    for op in achats:
        if op.source_id not in motifs and op.source_id not in copies:
            motifs[op.source_id] = MOTIF_SUSPENDUE  # copie suspendue : copier n'a rien tenté
    return motifs


def _textes_achats(achats, descriptions, bilans, compte, variations, motifs):
    textes = []
    for op in achats:
        texte = messages.achat(op, descriptions.get(op.source_id), bilans.get(op.personne_id), compte,
                               variations.get(op.source_id))
        if op.source_id in motifs:
            texte += f"\n\nCopie : seulement signalé ({motifs[op.source_id]})"
        textes.append(texte)
    return textes


def _textes_copier(textes):
    """Les textes de copier ; une suspension de la copie part comme une alerte."""
    return [messages.alerte(t) if t.startswith("Copie suspendue") else t for t in textes]


def _alerter(texte, dry_run):
    message = messages.alerte(texte)
    if dry_run:
        print(message, file=sys.stderr)
    else:
        boite.deposer(message)


def _arreter(texte, dry_run):
    _alerter(texte, dry_run)
    return 1


def main(argv=None, maintenant=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    dry_run = args.dry_run
    maintenant = maintenant or datetime.now(PARIS)
    config = charger_config()
    p = config["copie"]
    if not args.force and not doit_lancer(maintenant, _dernier_passage(), p.get("heure_paris", "08:00")):
        return 0
    try:
        suivis = _lire_json(SUIVIS, None)
    except ErreurFichier as erreur:
        return _arreter(str(erreur), dry_run)
    if suivis is None:
        print("Liste non validée (copie/suivis.json absent) : rien à faire.")
        return 0

    aujourd_hui = maintenant.date()
    actif = p.get("alpaca_actif") is True
    try:
        vus = nouveautes.charger_vus()
        client_sec = sec.ClientSec()
        en_attente = _lire_ventes_en_attente() if actif else []
    except (ErreurVus, ErreurSec, ErreurFichier) as erreur:
        return _arreter(str(erreur), dry_run)
    try:
        achats, ventes, notes, nouveaux_vus = nouveautes.collecter(suivis, vus, aujourd_hui, config, client_sec)
    except Exception as erreur:
        return _arreter(f"lecture des nouveautés impossible ({type(erreur).__name__})", dry_run)

    derniers_cours, variations, note_cours = _charger_cours(achats, aujourd_hui)
    notes = list(notes) + ([note_cours] if note_cours else [])
    bilans = _lire_bilans()
    a_deposer, signales, etat = [], [], {"positions": [], "historique": []}
    client, erreur_copie = None, None
    ventes_a_copier = _fusionner_ventes(en_attente, ventes) if actif else []

    if actif and not dry_run:
        try:
            client = alpaca.Client()
        except Exception as erreur:  # copier n'a pas tourné : rien n'est suspendu, aucun achat n'est étiqueté ainsi
            detail = str(erreur)[:200] if isinstance(erreur, ErreurAlpaca) else type(erreur).__name__
            return _arreter(f"client Alpaca impossible à créer, nouvel essai au prochain passage ({detail})", dry_run)
        try:
            plus, signales = copie_alpaca.copier(achats, ventes_a_copier, derniers_cours, client, p, aujourd_hui)
            a_deposer += _textes_copier(plus)
        except ErreurAlpaca as erreur:
            return _arreter(f"copie impossible, nouvel essai au prochain passage ({str(erreur)[:200]})", dry_run)
        except Exception as erreur:  # copier a déjà suspendu la copie : on signale les achats
            erreur_copie = type(erreur).__name__
            a_deposer.append(messages.alerte(
                f"erreur inattendue pendant la copie ({erreur_copie}) : copie suspendue, achats seulement signalés"))
        etat = copie_alpaca.charger_etat()

    if not actif:
        compte = COMPTE_DESACTIVE
    elif dry_run:
        compte = "non lu (essai à vide)"
    else:
        compte = _compte(client) if erreur_copie is None else "indisponible"
    descriptions = {} if dry_run or not achats else redaction.decrire(achats)
    motifs = _motifs(achats, signales, etat, actif, dry_run)
    textes = _textes_achats(achats, descriptions, bilans, compte, variations, motifs) + a_deposer
    if notes:
        liste = notes[0] if len(notes) == 1 else "\n" + "\n".join(f"• {n}" for n in notes)
        textes.append(messages.alerte(liste))

    if dry_run:
        for texte in textes:
            print(texte + "\n")
        print(f"[essai à vide] {len(achats)} achat(s), {len(ventes)} vente(s), {len(notes)} note(s) : rien écrit.")
        return 0

    for texte in textes:
        boite.deposer(texte)
    if actif:
        positions = {(pos["personne_id"], pos["ticker"]) for pos in etat["positions"]}
        _ecrire_json(VENTES_EN_ATTENTE, [vers_dict(v) for v in ventes_a_copier
                                         if (v.personne_id, v.ticker) in positions])
    nouveautes.enregistrer_vus(nouveaux_vus)
    _ecrire_json(DERNIER_LANCEMENT, {"jour": aujourd_hui.isoformat()})
    return 1 if erreur_copie else 0  # passage complet, mais la copie s'est suspendue : l'exécution apparaît en échec


def _lire_bilans():
    try:
        bilans = _lire_json(BILANS, {})
    except ErreurFichier:
        return {}
    return bilans if isinstance(bilans, dict) else {}


if __name__ == "__main__":
    sys.exit(main())
