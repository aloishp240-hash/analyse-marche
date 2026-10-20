#!/usr/bin/env python3
"""Agent court terme : repère les candidats du jour et lance une session Claude pour les valider.

Lancé toutes les 5 minutes par GitHub Actions ; ne fait rien avant 16h45 New York, les jours sans séance
ni une fois la session du jour faite. Claude ne passe que par les outils de outils/ et ne fixe aucun prix.
Usage : python agent.py [--force] [--dry-run]
  --force    lance même hors horaire ou si la session du jour est faite (sur la dernière séance disponible)
  --dry-run  affiche les candidats et la consigne sans appeler Claude ni rien écrire
"""
import argparse
import json
import subprocess
import sys
from datetime import datetime, time
from pathlib import Path

from commun import INDICE, NY, charger_config, charger_tickers, envoyer_telegram, fondamentaux, \
    historique_journalier, prochains_resultats
from court_terme.messages import LIBELLES
from court_terme.portefeuille import capital_courant, charger
from court_terme.strategie import reperer

AGENT = Path(__file__).resolve().parent / "agent"
ETAT = AGENT / "positions.json"
CANDIDATS = AGENT / "candidats-du-jour.json"
CONSIGNE = AGENT / "consigne.md"
SESSIONS = AGENT / "sessions"
HEURE_AGENT = time(16, 45)
TENTATIVES_MAX = 2
DUREE_MAX_S = 1200
OUTILS = ["candidats", "actualites", "engouement", "reddit", "notes", "signal", "info"]


def doit_lancer(maintenant, derniere_seance, session_existe):
    return maintenant.time() >= HEURE_AGENT and derniere_seance == maintenant.date() and not session_existe


def commande_claude(consigne):
    return ["claude", "-p", consigne, "--model", "sonnet", "--max-turns", "30", "--allowedTools",
            *[f"Bash(python outils/{outil}.py:*)" for outil in OUTILS], "Bash(python fiche.py:*)",
            "WebSearch", "WebFetch", "Skill"]


def _echecs(jour):
    return SESSIONS / f"{jour.isoformat()}.echecs"


def session_faite(jour):
    """Vrai si la session du jour a eu lieu, ou a échoué TENTATIVES_MAX fois."""
    echecs = _echecs(jour)
    return (SESSIONS / f"{jour.isoformat()}.md").exists() or \
        (echecs.exists() and int(echecs.read_text() or 0) >= TENTATIVES_MAX)


def ecrire_session(jour, texte):
    SESSIONS.mkdir(parents=True, exist_ok=True)
    (SESSIONS / f"{jour.isoformat()}.md").write_text(f"# Session du {jour.isoformat()}\n\n{texte.strip()}\n")


def lancer_session(consigne, jour):
    """Session Claude ; en cas d'échec, une nouvelle tentative au passage suivant, puis abandon pour la journée."""
    try:
        resultat = subprocess.run(commande_claude(consigne), timeout=DUREE_MAX_S, capture_output=True, text=True)
        code, erreur = resultat.returncode, resultat.stderr
    except (subprocess.TimeoutExpired, OSError) as exception:
        code, erreur = None, str(exception)
    if code == 0:
        ecrire_session(jour, resultat.stdout)
        return
    print(f"Session Claude échouée (code {code}) :\n{erreur[-2000:]}", file=sys.stderr)
    echecs = _echecs(jour)
    SESSIONS.mkdir(parents=True, exist_ok=True)
    nombre = (int(echecs.read_text() or 0) if echecs.exists() else 0) + 1
    echecs.write_text(str(nombre))
    suite = "nouvelle tentative dans 5 min" if nombre < TENTATIVES_MAX else "aucun signal aujourd'hui"
    try:
        envoyer_telegram(f"⚠️ AGENT COURT TERME — session échouée (code {code}), {suite}")
    except Exception as exception:
        print(f"Échec de l'envoi Telegram : {exception}", file=sys.stderr)


def resume_portefeuille(etat):
    lignes = [f"Capital réalisé : {capital_courant(etat):.2f} € (départ {etat['capital_initial']} €)"]
    for o in etat["ordres"]:
        lignes.append(f"- Ordre {o['ticker']} : entrée {o['entree']:.2f} $, stop {o['stop']:.2f} $, "
                      f"objectif {o['objectif']:.2f} $, signal du {o['date_signal']}")
    for p in etat["positions"]:
        lignes.append(f"- Position {p['ticker']} : achetée {p['prix_entree']:.2f} $ le {p['date_entree']}, "
                      f"stop {p['stop']:.2f} $, objectif {p['objectif']:.2f} $, séance {p['seances']}")
    if not etat["ordres"] and not etat["positions"]:
        lignes.append("Aucun ordre ni position en cours.")
    return "\n".join(lignes)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    maintenant = datetime.now(NY)
    if not args.force and (maintenant.time() < HEURE_AGENT or session_faite(maintenant.date())):
        return
    tickers = charger_tickers()
    hist = historique_journalier(tickers + [INDICE], inclure_jour=True)
    jour = hist.index[-1].date()
    if not args.force and not doit_lancer(maintenant, jour, session_faite(jour)):
        return

    p = charger_config()["court_terme"]
    etat = charger(ETAT, p)
    suivis = {e["ticker"] for e in etat["ordres"] + etat["positions"]}
    candidats = reperer(hist, tickers, prochains_resultats(tickers), jour, p, suivis)
    infos = fondamentaux([c["ticker"] for c in candidats], cache=False) if candidats else {}
    for c in candidats:
        c["industrie"] = (infos.get(c["ticker"]) or {}).get("industry")
    contenu = json.dumps({"jour": jour.isoformat(), "candidats": candidats}, indent=2, ensure_ascii=False)

    if args.dry_run:
        print(contenu)
    else:
        CANDIDATS.write_text(contenu + "\n")
    if not candidats and not suivis:
        texte = "Aucun candidat aujourd'hui et aucune position en cours : pas de session Claude."
        print(texte)
        if not args.dry_run:
            ecrire_session(jour, texte)
            try:
                envoyer_telegram(f"🤖 AGENT COURT TERME — {texte}")
            except Exception as exception:
                print(f"Échec de l'envoi Telegram : {exception}", file=sys.stderr)
        return

    liste = ", ".join(f"{c['ticker']} ({LIBELLES.get(c['configuration'])})" for c in candidats) or "aucun"
    consigne = (f"{CONSIGNE.read_text().strip()}\n\n## Contexte\nDate : {jour.isoformat()}\n"
                f"Candidats du jour : {liste}\n\nPortefeuille fictif :\n{resume_portefeuille(etat)}\n")
    if args.dry_run:
        print("\n--- Consigne ---\n" + consigne)
        print("--- Commande ---\n" + " ".join(commande_claude("<consigne>")))
        return
    lancer_session(consigne, jour)


if __name__ == "__main__":
    main()
