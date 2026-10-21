"""Évaluation des décisions de Claude : ce qu'il a validé ou rejeté, ce que le code en a fait, ce que ça a coûté.

Chaque session laisse agent/decisions/AAAA-MM-JJ.json. La décision réelle est celle du code (ordre créé ou non) :
le texte de Claude n'est qu'un avis, comparé à cette décision pour repérer les incohérences.

Pour juger l'apport de Claude, tous les candidats du jour (validés comme rejetés) sont rejoués avec les mêmes règles
(court_terme/analyse.rejouer) : la stratégie sans IA prendrait tous les candidats. Un bon trade n'est pas une bonne
décision, ni un mauvais une erreur : seul un grand échantillon dit si les rejets de Claude ajoutent quelque chose.
Claude ne donne aucun score de confiance ; s'il en donnait un, ce ne serait pas une probabilité tant qu'il n'aurait
pas été comparé aux résultats sur des centaines de cas (calibration).
"""
import json
import re

from court_terme.validation import nettoyer

VERDICT = re.compile(r"\(([A-Z][A-Z0-9.\-]{0,9})\)\s*:\s*(?:✅|❌)?\s*(validé|rejeté)\s*[—–-]\s*(.*)")
OUTILS_COMPTES = ("WebSearch", "WebFetch", "Bash", "Skill")
SOURCE_MAX = 300


def verdicts(texte):
    """{ticker: {"verdict": "valide" | "rejete", "motif"}} lus dans le compte rendu final de Claude."""
    resultat = {}
    for ligne in (texte or "").splitlines():
        m = VERDICT.search(ligne)
        if m:
            resultat[m.group(1)] = {"verdict": "valide" if m.group(2) == "validé" else "rejete",
                                    "motif": nettoyer(m.group(3), 400, une_ligne=True)}
    return resultat


def cout_et_sources(sortie):
    """Coût de la session (tours, durée, coût équivalent API) et sources consultées, lus dans le stream-json."""
    cout = {"tours": None, "duree_s": None, "cout_usd": None, **{o: 0 for o in OUTILS_COMPTES}}
    sources = []
    for ligne in (sortie or "").splitlines():
        try:
            evt = json.loads(ligne)
        except ValueError:
            continue
        if not isinstance(evt, dict):
            continue
        if evt.get("type") == "result":
            cout.update(tours=evt.get("num_turns"), duree_s=round(evt.get("duration_ms", 0) / 1000),
                        cout_usd=evt.get("total_cost_usd"))
        if evt.get("type") != "assistant":
            continue
        for bloc in (evt.get("message") or {}).get("content") or []:
            if not isinstance(bloc, dict) or bloc.get("type") != "tool_use":
                continue
            nom, entree = bloc.get("name"), bloc.get("input") or {}
            if nom in OUTILS_COMPTES:
                cout[nom] += 1
            source = {"WebSearch": entree.get("query"), "WebFetch": entree.get("url"),
                      "Bash": entree.get("command")}.get(nom)
            if source:
                sources.append({"outil": nom, "source": str(source)[:SOURCE_MAX]})
    return cout, sources


def decision_du_jour(jour, candidats, attente, texte_final, sortie, reussie):
    """Trace de la session : avis de Claude, décision du code et refus pour chaque candidat, coût et sources."""
    avis = verdicts(texte_final)
    ordres = {o["ticker"]: o for o in ((attente.get("etat") or {}).get("ordres") or [])
              if o.get("date_signal") == jour} if reussie else {}
    refus = attente.get("refus") or []
    lignes, incoherences = [], []
    for c in candidats:
        t = c["ticker"]
        claude = avis.get(t, {}).get("verdict")
        ordre = ordres.get(t)
        lignes.append({**{k: c.get(k) for k in ("ticker", "nom", "configuration", "cloture", "entree", "stop",
                                                "objectif", "atr", "mm50", "date_resultats")},
                       "avis_claude": claude, "motif_claude": avis.get(t, {}).get("motif"),
                       "ordre_cree": ordre is not None, "id_ordre": ordre and ordre.get("id"),
                       "refus_code": [r["motif"] for r in refus if r.get("ticker") == t]})
        if claude is None:
            incoherences.append(f"{t} : aucun avis de Claude dans le compte rendu")
        elif claude == "valide" and not ordre:
            incoherences.append(f"{t} : validé par Claude mais aucun ordre créé")
        elif claude == "rejete" and ordre:
            incoherences.append(f"{t} : rejeté par Claude mais un ordre a été créé")
    cout, sources = cout_et_sources(sortie)
    return {"jour": jour, "session_reussie": reussie, "candidats": lignes, "incoherences": incoherences,
            "refus_hors_candidats": [r for r in refus if r.get("ticker") not in {c["ticker"] for c in candidats}],
            "fermetures": attente.get("fermetures", 0), "cout": cout, "sources": sources}


def _groupe(resultats):
    fermes = [r for r in resultats if r.get("statut") == "ferme" and r.get("resultat_r") is not None]
    return {"candidats": len(resultats), "trades": len(fermes),
            "non_executes": sum(1 for r in resultats if r.get("statut") == "non_execute"),
            "r_moyen": round(sum(r["resultat_r"] for r in fermes) / len(fermes), 2) if fermes else None,
            "gagnants_pct": round(sum(1 for r in fermes if r["resultat_r"] > 0) / len(fermes) * 100, 1)
            if fermes else None}


def bilan(decisions, rejoues, fermees, echantillon_min):
    """Bilan des décisions de Claude.

    rejoues = {"jour:ticker": résultat de rejouer()} pour chaque candidat ; fermees = trades réels du portefeuille.
    Compare les candidats rejoués selon l'avis de Claude ; « tous » = la stratégie sans IA.
    """
    groupes = {"ordres_crees": [], "rejetes": [], "valides_refuses_par_le_code": [], "tous": []}
    a_relire, motifs = [], []
    for d in decisions:
        if not d.get("session_reussie"):
            continue
        for c in d["candidats"]:
            r = rejoues.get(f"{d['jour']}:{c['ticker']}") or {"statut": "non_rejoue"}
            groupes["tous"].append(r)
            if c["ordre_cree"]:
                groupes["ordres_crees"].append(r)
            elif c["avis_claude"] == "rejete":
                groupes["rejetes"].append(r)
                motifs.append({"jour": d["jour"], "ticker": c["ticker"], "motif": c["motif_claude"],
                               "suite": r.get("motif") or r["statut"], "resultat_r": r.get("resultat_r")})
            elif c["avis_claude"] == "valide":
                groupes["valides_refuses_par_le_code"].append(r)
            # Cas à relire à la main : ils ne prouvent pas une erreur, un seul trade ne juge pas une décision
            if c["avis_claude"] == "rejete" and r.get("motif") == "objectif":
                a_relire.append(f"{d['jour']} {c['ticker']} : rejeté, aurait atteint l'objectif ({c['motif_claude']})")
            if c["ordre_cree"] and r.get("motif") == "stop" and (r.get("resultat_r") or 0) < 0:
                a_relire.append(f"{d['jour']} {c['ticker']} : validé, vendu au stop")
    resume = {nom: _groupe(liste) for nom, liste in groupes.items()}
    ids = {c.get("id_ordre") for d in decisions for c in d["candidats"] if c.get("id_ordre")}
    reels = [f["resultat_r"] for f in fermees if f.get("id") in ids and f.get("resultat_r") is not None]
    sessions = [d for d in decisions if d.get("cout")]
    cout = {cle: sum(d["cout"].get(cle) or 0 for d in sessions) for cle in ("tours", "duree_s", "cout_usd",
                                                                             "WebSearch", "WebFetch", "Bash")}
    ordres, tous = resume["ordres_crees"]["r_moyen"], resume["tous"]["r_moyen"]
    return {
        "sessions": len(sessions), "groupes": resume,
        "resultats_reels": {"trades": len(reels), "r_moyen": round(sum(reels) / len(reels), 2) if reels else None},
        "apport_r_par_trade": round(ordres - tous, 2) if ordres is not None and tous is not None else None,
        "motifs_de_rejet": motifs, "a_relire": a_relire,
        "incoherences": [f"{d['jour']} {i}" for d in decisions for i in d.get("incoherences", [])],
        "cout_total": {**cout, "cout_usd": round(cout["cout_usd"], 2)},
        "echantillon_faible": resume["tous"]["trades"] < echantillon_min,
    }
