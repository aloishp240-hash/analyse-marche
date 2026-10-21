"""Rapport quotidien d'audit du portefeuille fictif : bref, limité à ce qui demande un coup d'œil.

agent.py l'écrit dans agent/audit/AAAA-MM-JJ.md à chaque passage qui travaille sur la séance (il est réécrit si un
second passage suit un échec). Les rubriques vides sont omises. Seuls les points « à vérifier » nouveaux partent sur
Telegram : le reste a déjà son propre message (entrées bloquées, session échouée, suspension) ou n'est qu'informatif.
"""
from datetime import date, timedelta

from court_terme.etats import ANNULE, CANDIDAT, EXPIRE, anomalies
from court_terme.messages import titre
from court_terme.securite import suivi_en_retard

JOURS_RESULTATS = 7  # résultats trimestriels annoncés dans ce délai : signalés pour les titres suivis
SOURCES_MAX = 8
TITRE_A_VERIFIER = "## À vérifier"


def _evenements_du_jour(etat, jour):
    """Lignes d'historique datées du jour, pour tous les titres de l'état (en cours ou clos)."""
    lignes = []
    for liste in ("ordres", "positions", "fermees", "ordres_clos"):
        for e in etat.get(liste, []):
            for h in e.get("historique", []):
                if h["jour"] != jour or h["evenement"] in (ANNULE, EXPIRE, CANDIDAT):
                    continue
                detail = {"execute": f"achat simulé à {h.get('prix', '?')} $",
                          "cloture": f"vente simulée ({h.get('motif', '?')})",
                          "stop_remonte": f"stop remonté à {h.get('stop', '?')} $",
                          "en_attente": "ordre enregistré"}.get(h["evenement"], h["evenement"])
                lignes.append(f"{titre(e)} : {detail}")
    return lignes


def _resultats_proches(etat, jour):
    fin = (date.fromisoformat(jour) + timedelta(days=JOURS_RESULTATS)).isoformat()
    return [f"{titre(e)} : résultats annoncés le {e['date_resultats']}"
            for e in etat["ordres"] + etat["positions"] if e.get("date_resultats") and jour <= e["date_resultats"] <= fin]


def _ecarts(etat, decision, jour):
    """Différences entre ce que la session a décidé et ce que l'état enregistré contient."""
    if not decision or not decision.get("session_reussie"):
        return []
    ids = {e.get("id") for liste in ("ordres", "positions", "fermees", "ordres_clos") for e in etat.get(liste, [])}
    ecarts = [f"{c['ticker']} : ordre {c['id_ordre']} décidé par la session, absent de positions.json"
              for c in decision["candidats"] if c.get("ordre_cree") and c.get("id_ordre") not in ids]
    decides = {c.get("id_ordre") for c in decision["candidats"] if c.get("ordre_cree")}
    ecarts += [f"{o['ticker']} : ordre {o.get('id')} du jour enregistré sans décision de session correspondante"
               for o in etat["ordres"] + etat["positions"] + etat.get("ordres_clos", []) + etat["fermees"]
               if o.get("date_signal") == jour and o.get("id") not in decides]
    return ecarts


def _decisions(decision):
    lignes = []
    for c in decision["candidats"]:
        if c["ordre_cree"]:
            lignes.append(f"{c['ticker']} : validé par Claude → ordre {c['id_ordre']}")
        elif c["avis_claude"] == "valide":
            lignes.append(f"{c['ticker']} : validé par Claude, refusé par le code ({' ; '.join(c['refus_code']) or '?'})")
        elif c["avis_claude"] == "rejete":
            lignes.append(f"{c['ticker']} : rejeté par Claude — {c.get('motif_claude') or 'sans motif'}")
        else:
            lignes.append(f"{c['ticker']} : aucun avis de Claude")
    if decision.get("fermetures"):
        lignes.append(f"Ventes demandées par Claude : {decision['fermetures']}")
    cout = decision.get("cout") or {}
    if cout.get("tours") is not None:
        lignes.append(f"Coût : {cout['tours']} tours, {cout.get('duree_s') or 0} s, {cout.get('WebSearch', 0)} recherches, "
                      f"{cout.get('WebFetch', 0)} pages lues")
    sources = decision.get("sources") or []
    for s in sources[:SOURCES_MAX]:
        lignes.append(f"Source ({s['outil']}) : {s['source']}")
    if len(sources) > SOURCES_MAX:
        lignes.append(f"… et {len(sources) - SOURCES_MAX} autres sources (journal de session)")
    return lignes


def rapport(jour, etat, candidats, decision, anomalies_notees, echecs_session):
    """(texte Markdown du rapport, points à vérifier).

    jour : séance traitée (AAAA-MM-JJ) ; candidats : contenu de candidats-du-jour.json (None si illisible) ;
    decision : trace de agent/decisions/ (None sans session) ; anomalies_notees : contenu de anomalies.json.
    """
    donnees, donnees_ok = [], False
    if not candidats or candidats.get("jour") != jour:
        donnees.append("fichier des candidats absent ou pas écrit pour cette séance")
    elif not candidats.get("donnees_au"):
        donnees.append("date des cours non enregistrée dans le fichier des candidats")
    else:
        donnees_ok = candidats["donnees_au"] == jour and not candidats.get("entrees_bloquees")
        donnees.append(f"cours du {candidats['donnees_au']} : {'à jour' if candidats['donnees_au'] == jour else 'PÉRIMÉS'}")
        donnees += [f"aucune nouvelle entrée : {m}" for m in candidats.get("entrees_bloquees", [])]
        ecartes = candidats.get("titres_ecartes") or {}
        if ecartes:
            donnees.append(f"{len(ecartes)} titre(s) écarté(s) pour données invalides : "
                           + ", ".join(f"{t} ({m})" for t, m in list(ecartes.items())[:5]))

    erreurs = list(dict.fromkeys(f"{a['source']} : {a['texte']}" for a in anomalies_notees
                                 if a.get("quand", "")[:10] >= jour))
    if echecs_session:
        erreurs.append(f"session Claude échouée {echecs_session} fois")

    a_verifier = suivi_en_retard(etat, jour) + anomalies(etat, jour) + _ecarts(etat, decision, jour)
    if decision:
        a_verifier += decision.get("incoherences", [])
        a_verifier += [f"refus hors candidats : {r.get('ticker')} ({r.get('motif')})"
                       for r in decision.get("refus_hors_candidats", [])]
    if etat.get("suspension"):
        a_verifier.append(f"nouvelles entrées suspendues depuis le {etat['suspension']['depuis']} : "
                          f"{etat['suspension']['motif']} (python suspension.py lever après vérification)")

    clos = [f"{titre(o)} : {'annulé' if o['etat'] == ANNULE else 'expiré'}"
            + (f" — {h.get('motif')}" if h.get("motif") else "")
            for o in etat.get("ordres_clos", []) for h in o.get("historique", [])[-1:] if h["jour"] == jour]
    evenements = _evenements_du_jour(etat, jour) + _resultats_proches(etat, jour)

    rubriques = [("## Données", donnees), ("## Erreurs et tâches échouées", erreurs), (TITRE_A_VERIFIER, a_verifier),
                 ("## Événements", evenements), ("## Signaux annulés ou expirés", clos),
                 ("## Décisions de Claude", _decisions(decision) if decision else [])]
    lignes = [f"# Audit du {jour} — portefeuille fictif (simulation)", ""]
    for entete, contenu in rubriques:
        if contenu:
            lignes += [entete, *[f"- {c}" for c in contenu], ""]
    if donnees_ok and not (erreurs or a_verifier):
        lignes.append("Rien d'anormal.")
    return "\n".join(lignes).rstrip() + "\n", a_verifier


def points_nouveaux(points, ancien_texte):
    """Points à vérifier absents d'un rapport déjà écrit pour la même séance (pour ne pas les renvoyer)."""
    return [p for p in points if f"- {p}\n" not in (ancien_texte or "")]


def message_audit(jour, points):
    return (f"🧾 AUDIT DU {jour} — {len(points)} point(s) à vérifier :\n" + "\n".join(f"- {p}" for p in points[:5])
            + ("\n…" if len(points) > 5 else "") + f"\nDétail : agent/audit/{jour}.md")
