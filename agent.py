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
import os
import subprocess
import sys
from datetime import datetime, time
from pathlib import Path

from commun import INDICE, NY, charger_config, charger_tickers, fondamentaux, historique_journalier, nom_societe, \
    prochains_resultats
from court_terme import attente
from court_terme.audit import message_audit, points_nouveaux, rapport
from court_terme.boite import deposer
from court_terme.evaluation import decision_du_jour
from court_terme.messages import LIBELLES, message_evenement, titre
from court_terme.portefeuille import capital_courant, charger, sauver
from court_terme.preglages import parametres
from court_terme.securite import ANOMALIES, controle_donnees, controler_etat, message_suspension, noter_anomalie, \
    suivi_en_retard, verrou
from court_terme.simulation import annuler_avant_resultats
from court_terme.strategie import lire_date_resultats, reperer

AGENT = Path(__file__).resolve().parent / "agent"
ETAT = AGENT / "positions.json"
CANDIDATS = AGENT / "candidats-du-jour.json"
CONSIGNE = AGENT / "consigne.md"
EN_ATTENTE = AGENT / "en-attente.json"
SESSIONS = AGENT / "sessions"
DECISIONS = AGENT / "decisions"
AUDIT = AGENT / "audit"
HEURE_AGENT = time(16, 45)
TENTATIVES_MAX = 2
DUREE_MAX_S = 1200
EXTRAIT_MAX = 1500  # caractères gardés de chaque réponse d'outil dans le journal
OUTILS = ["candidats", "actualites", "engouement", "reddit", "polymarket", "notes", "signal", "fermer", "info"]
# Outils accessibles sans autorisation en mode -p : retirés pour qu'un contenu web malveillant ne lise ni .git/config
# (jeton GitHub) ni aucun fichier, et ne modifie pas le dépôt.
INTERDITS = ["Read", "Glob", "Grep", "Edit", "Write", "NotebookEdit", "Task"]


def doit_lancer(maintenant, derniere_seance, session_existe):
    return maintenant.time() >= HEURE_AGENT and derniere_seance == maintenant.date() and not session_existe


def commande_claude(consigne):
    # stream-json : chaque appel d'outil et sa réponse arrivent sur stdout, ce qui permet d'écrire le journal
    return ["claude", "-p", consigne, "--model", "sonnet", "--max-turns", "30", "--output-format", "stream-json",
            "--verbose", "--allowedTools", *[f"Bash(python outils/{outil}.py:*)" for outil in OUTILS],
            "Bash(python fiche.py:*)", "WebSearch", "WebFetch", "Skill", "--disallowedTools", *INTERDITS]


def _texte_resultat(contenu):
    if isinstance(contenu, list):
        return "\n".join(b.get("text", "") for b in contenu if isinstance(b, dict))
    return str(contenu or "")


def _decrire_appel(nom, entree):
    if nom == "Bash":
        return f"Commande : `{entree.get('command', '')}`"
    if nom == "WebSearch":
        return f"Recherche web : « {entree.get('query', '')} »"
    if nom == "WebFetch":
        return f"Page lue : {entree.get('url', '')}\n\nQuestion posée : {entree.get('prompt', '')}"
    if nom == "Skill":
        return f"Skill chargé : {entree.get('skill', '')}"
    return f"{nom} : `{json.dumps(entree, ensure_ascii=False)}`"


def lire_flux(sortie):
    """Sépare la sortie stream-json de claude en (réponse finale, étapes du journal).

    Chaque étape est un texte de Claude ou un appel d'outil avec un extrait de sa réponse. Une sortie qui n'est pas
    du stream-json est rendue telle quelle comme réponse finale.
    """
    final, etapes, appels, bilan = None, [], {}, None
    for ligne in sortie.splitlines():
        try:
            evt = json.loads(ligne)
        except ValueError:
            continue
        if not isinstance(evt, dict):
            continue
        if evt.get("type") == "result":
            final = evt.get("result")
            bilan = f"{evt.get('num_turns', '?')} tours, {round(evt.get('duration_ms', 0) / 1000)} s"
            continue
        if evt.get("type") not in ("assistant", "user"):
            continue
        for bloc in (evt.get("message") or {}).get("content") or []:
            if not isinstance(bloc, dict):
                continue
            if bloc.get("type") == "text" and evt["type"] == "assistant" and bloc.get("text", "").strip():
                etapes.append({"texte": bloc["text"].strip()})
            elif bloc.get("type") == "tool_use" and bloc.get("name") != "ToolSearch":
                etape = {"appel": _decrire_appel(bloc.get("name"), bloc.get("input") or {})}
                appels[bloc.get("id")] = etape
                etapes.append(etape)
            elif bloc.get("type") == "tool_result" and bloc.get("tool_use_id") in appels:
                etape = appels[bloc["tool_use_id"]]
                etape["reponse"] = _texte_resultat(bloc.get("content")).strip()
                etape["erreur"] = bool(bloc.get("is_error"))
    return (sortie if final is None else final), etapes, bilan


def ecrire_journal(jour, etapes, bilan, reussie):
    """Journal lisible de la session : ce que Claude a cherché, lu et lancé, avec un extrait de chaque réponse."""
    lignes = [f"# Journal de la session du {jour.isoformat()}", ""]
    if not reussie:
        lignes += ["⚠️ Session échouée : rien n'a été appliqué ni envoyé.", ""]
    if bilan:
        lignes += [f"Durée : {bilan}.", ""]
    numero = 0
    for etape in etapes:
        if "texte" in etape:
            lignes += ["> 💬 " + etape["texte"].replace("\n", "\n> "), ""]
            continue
        numero += 1
        lignes += [f"## {numero}. {etape['appel']}", ""]
        reponse = etape.get("reponse")
        if reponse is None:
            lignes += ["_(pas de réponse)_", ""]
            continue
        if len(reponse) > EXTRAIT_MAX:
            reponse = reponse[:EXTRAIT_MAX] + " […]"
        titre_reponse = "Erreur" if etape.get("erreur") else "Réponse"
        lignes += [f"{titre_reponse} :", "", "```", reponse.replace("```", "'''"), "```", ""]
    if not etapes:
        lignes += ["Aucun outil appelé.", ""]
    SESSIONS.mkdir(parents=True, exist_ok=True)
    (SESSIONS / f"{jour.isoformat()}-journal.md").write_text("\n".join(lignes))


# Secrets retirés de l'environnement de claude : Telegram (les messages partent d'agent.py) et jetons GitHub
SECRETS = ("TELEGRAM_", "GH_TOKEN", "GITHUB_TOKEN", "ACTIONS_RUNTIME_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_")


def environnement_claude():
    """Environnement du processus claude, sans les secrets dont il n'a pas besoin.

    Restent CLAUDE_CODE_OAUTH_TOKEN, indispensable à claude, et les identifiants Reddit dont a besoin
    outils/reddit.py (lancé par claude, il hérite de son environnement).
    """
    return {k: v for k, v in os.environ.items() if not k.startswith(SECRETS)}


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


def envoyer(texte):
    """Message mis dans la boîte d'envoi : il part (envoyer.py) une fois l'état enregistré dans le dépôt."""
    deposer(texte)


def ecrire_decision(jour, sortie, final, reussie):
    """Trace de la session pour évaluer Claude (evaluation_claude.py) ; écrite avant d'appliquer ses signaux."""
    try:
        candidats = json.loads(CANDIDATS.read_text()).get("candidats", [])
    except (OSError, ValueError):
        candidats = []
    trace = decision_du_jour(jour.isoformat(), candidats, attente.lire(EN_ATTENTE), final, sortie, reussie)
    sauver(trace, DECISIONS / f"{jour.isoformat()}.json")


def appliquer_attente():
    """Enregistre les ordres validés pendant la session et envoie ses messages."""
    en_attente = attente.lire(EN_ATTENTE)
    if en_attente["etat"]:
        sauver(en_attente["etat"], ETAT)
    for message in en_attente["messages"]:
        envoyer(message)
    EN_ATTENTE.unlink(missing_ok=True)


def lancer_session(consigne, jour):
    """Session Claude ; ses signaux ne sont appliqués que si elle réussit.

    En cas d'échec, rien n'est envoyé : nouvelle tentative au passage suivant, puis abandon pour la journée.
    Le journal des outils appelés est écrit dans les deux cas.
    """
    EN_ATTENTE.unlink(missing_ok=True)
    try:
        resultat = subprocess.run(commande_claude(consigne), timeout=DUREE_MAX_S, capture_output=True, text=True,
                                  env=environnement_claude())
        code, sortie, erreur = resultat.returncode, resultat.stdout, resultat.stderr
    except (subprocess.TimeoutExpired, OSError) as exception:
        sortie = getattr(exception, "stdout", None) or ""
        if isinstance(sortie, bytes):
            sortie = sortie.decode(errors="replace")
        code, erreur = None, str(exception)
    final, etapes, bilan = lire_flux(sortie)
    ecrire_journal(jour, etapes, bilan, code == 0)
    ecrire_decision(jour, sortie, final, code == 0)
    if code == 0:
        appliquer_attente()
        ecrire_session(jour, final)
        return
    EN_ATTENTE.unlink(missing_ok=True)
    print(f"Session Claude échouée (code {code}) :\n{erreur[-2000:]}", file=sys.stderr)
    echecs = _echecs(jour)
    SESSIONS.mkdir(parents=True, exist_ok=True)
    nombre = (int(echecs.read_text() or 0) if echecs.exists() else 0) + 1
    echecs.write_text(str(nombre))
    suite = "nouvelle tentative dans 5 min" if nombre < TENTATIVES_MAX else "aucun signal aujourd'hui"
    envoyer(f"⚠️ AGENT COURT TERME — session échouée (code {code}), aucun signal envoyé ; {suite}")


def resume_portefeuille(etat):
    lignes = [f"Capital réalisé : {capital_courant(etat):.2f} € (départ {etat['capital_initial']} €)"]
    for o in etat["ordres"]:
        lignes.append(f"- Ordre {titre(o)} : entrée {o['entree']:.2f} $, stop {o['stop']:.2f} $, "
                      f"objectif {o['objectif']:.2f} $, signal du {o['date_signal']}\n"
                      f"  Thèse d'achat : {o.get('justification', '(inconnue)')}")
    for p in etat["positions"]:
        vente = f", vente déjà décidée le {p['sortie_demandee']}" if p.get("sortie_demandee") else ""
        lignes.append(f"- Position {titre(p)} : achetée {p['prix_entree']:.2f} $ le {p['date_entree']}, "
                      f"stop {p['stop']:.2f} $, objectif {p['objectif']:.2f} $, séance {p['seances']}{vente}\n"
                      f"  Thèse d'achat : {p.get('justification', '(inconnue)')}")
    if not etat["ordres"] and not etat["positions"]:
        lignes.append("Aucun ordre ni position en cours.")
    return "\n".join(lignes)


def lire_json(chemin, defaut=None):
    try:
        return json.loads(Path(chemin).read_text())
    except (OSError, ValueError):
        return defaut


def ecrire_audit(jour, dry_run=False, candidats=None):
    """Rapport d'audit de la séance ; seuls les points à vérifier nouveaux partent sur Telegram."""
    etat = charger(ETAT, parametres(charger_config()))
    echecs = _echecs(jour)
    texte, points = rapport(jour.isoformat(), etat, candidats or lire_json(CANDIDATS), lire_json(DECISIONS / f"{jour.isoformat()}.json"),
                            lire_json(ANOMALIES, []), int(echecs.read_text() or 0) if echecs.exists() else 0)
    if dry_run:
        print("\n--- Audit ---\n" + texte)
        return
    chemin = AUDIT / f"{jour.isoformat()}.md"
    nouveaux = points_nouveaux(points, chemin.read_text() if chemin.exists() else "")
    AUDIT.mkdir(parents=True, exist_ok=True)
    chemin.write_text(texte)
    if nouveaux:
        envoyer(message_audit(jour.isoformat(), nouveaux))


def main():
    with verrou():
        fait = preparer()
        if fait:
            ecrire_audit(*fait)


def entrees_bloquees(hist, tickers, etat, jour_attendu, jour):
    """(motifs qui interdisent tout nouvel ordre aujourd'hui, titres écartés pour données invalides).

    Échec prudent : un doute sur les données ou sur le suivi des positions n'aboutit jamais à un achat.
    """
    motifs, ecartes = controle_donnees(hist, tickers, jour_attendu, INDICE)
    retard = suivi_en_retard(etat, jour)
    if retard:
        motifs.append("suivi des positions pas à jour (" + " ; ".join(retard[:3]) + ")")
    if etat.get("suspension"):
        motifs.append(f"nouvelles entrées suspendues depuis le {etat['suspension']['depuis']} : "
                      f"{etat['suspension']['motif']}")
    return motifs, ecartes


def preparer():
    """Passage de l'agent ; renvoie (séance traitée, dry_run, candidats), ou None s'il n'y avait rien à faire."""
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

    p = parametres(charger_config())
    etat = charger(ETAT, p)
    resultats = prochains_resultats(tickers)
    annulations = annuler_avant_resultats(etat, jour, resultats, p)
    problemes, suspension_nouvelle = controler_etat(etat, jour)
    if (annulations or suspension_nouvelle) and not args.dry_run:
        sauver(etat, ETAT)
    if suspension_nouvelle:
        print(message_suspension(etat)) if args.dry_run else envoyer(message_suspension(etat))
    bloquants, ecartes = entrees_bloquees(hist, tickers, etat, jour if args.force else maintenant.date(), jour)
    for texte in problemes + bloquants + [f"{t} écarté : {m}" for t, m in ecartes.items()]:
        if args.dry_run:
            print(f"Anomalie : {texte}")
        else:
            noter_anomalie("agent.py", texte)
    for evt in annulations:
        if args.dry_run:
            print(message_evenement(evt))
        else:
            envoyer(message_evenement(evt))
    suivis = {e["ticker"] for e in etat["ordres"] + etat["positions"]}
    candidats = reperer(hist, [t for t in tickers if t not in ecartes], resultats, jour, p, suivis)
    # Entrées bloquées : les candidats sont gardés pour information, mais pas proposés à Claude
    non_proposes = [c["ticker"] for c in candidats] if bloquants else []
    if bloquants:
        candidats = []
    infos = fondamentaux([c["ticker"] for c in candidats], cache=False) if candidats else {}
    for c in candidats:
        c["industrie"] = (infos.get(c["ticker"]) or {}).get("industry")
        c["nom"] = nom_societe(infos.get(c["ticker"]), c["ticker"])
        # Contexte connu avant l'entrée, recopié dans l'ordre puis dans le trade
        date_resultats = lire_date_resultats(resultats, c["ticker"])
        c["date_resultats"] = date_resultats.isoformat() if date_resultats else None
    # donnees_au : dernière séance présente dans l'historique, pour voir d'un coup d'œil des données périmées
    contenu = json.dumps({"jour": jour.isoformat(), "donnees_au": hist.index[-1].date().isoformat(),
                          "entrees_bloquees": bloquants, "candidats_non_proposes": non_proposes,
                          "titres_ecartes": ecartes, "candidats": candidats}, indent=2, ensure_ascii=False)

    if args.dry_run:
        print(contenu)
    else:
        CANDIDATS.write_text(contenu + "\n")
    if bloquants:
        alerte = "⛔ AGENT COURT TERME — aucune nouvelle entrée aujourd'hui :\n" + "\n".join(f"- {m}" for m in bloquants)
        print(alerte) if args.dry_run else envoyer(alerte)
    if not candidats and not suivis:
        texte = "Aucun candidat aujourd'hui et aucune position en cours : pas de session Claude."
        print(texte)
        if not args.dry_run:
            ecrire_session(jour, texte)
            envoyer(f"🤖 AGENT COURT TERME — {texte}")
        return jour, args.dry_run, json.loads(contenu)

    liste = ", ".join(f"{titre(c)}, {LIBELLES.get(c['configuration'])}" for c in candidats) or "aucun"
    blocage = ("Nouvelles entrées bloquées aujourd'hui par le code (signal.py refusera tout signal) : "
               + " ; ".join(bloquants) + "\nSurveille seulement les ordres et positions en cours.\n") if bloquants else ""
    consigne = (f"{CONSIGNE.read_text().strip()}\n\n## Contexte\nDate : {jour.isoformat()}\n{blocage}"
                f"Candidats du jour : {liste}\n\nPortefeuille fictif :\n{resume_portefeuille(etat)}\n")
    if args.dry_run:
        print("\n--- Consigne ---\n" + consigne)
        print("--- Commande ---\n" + " ".join(commande_claude("<consigne>")))
        return jour, True, json.loads(contenu)
    lancer_session(consigne, jour)
    return jour, False, json.loads(contenu)


if __name__ == "__main__":
    main()
