"""Mesures détaillées de chaque trade fermé et variantes rejouées, pour apprendre sans toucher au résultat officiel.

Une variante repart de l'ordre tel qu'il était le jour du signal et rejoue les mêmes barres journalières avec la même
simulation (court_terme/simulation.avancer) ; une seule règle change. Ce sont des hypothèses : elles ne modifient ni
le portefeuille, ni ses statistiques, ni config.json. Aucun paramètre n'est réglé automatiquement à partir d'elles :
une variante qui fait mieux sur quelques trades tient surtout du hasard.
"""
import copy
import math

from court_terme.etats import EN_ATTENTE, EXECUTE, CLOTURE
from court_terme.simulation import avancer
from court_terme.stats import ECHANTILLON_MIN

VARIANTES = {
    "officielle": "mêmes règles que le trade réel (contrôle : doit retrouver le résultat officiel)",
    "entree_au_marche": "achat à l'ouverture du lendemain du signal, sans attendre le prix limite",
    "sans_objectif": "pas de vente à l'objectif : seuls le stop et le stop temporel font sortir",
    "sans_remontee_stop": "le stop n'est jamais remonté au prix d'achat",
    "objectif_2_atr": "objectif plus proche, à 2 ATR au-dessus du prix d'entrée prévu",
    "stop_temporel_10": "vente au bout de 10 séances au lieu de 15",
}
CHAMPS_ORDRE = ("id", "ticker", "nom", "configuration", "entree", "atr", "mm50", "quantite", "date_signal",
                "industrie", "sortie_demandee")


def _changer(nom, ordre, p):
    if nom == "entree_au_marche":
        ordre["entree"] = math.inf
        p["annulation_gap_atr"] = None
    elif nom == "sans_objectif":
        ordre["objectif"] = math.inf
    elif nom == "sans_remontee_stop":
        p["remontee_stop_atr"] = math.inf
    elif nom == "objectif_2_atr":
        ordre["objectif"] = ordre["entree"] + 2 * ordre["atr"]
    elif nom == "stop_temporel_10":
        p["stop_temporel_seances"] = 10
    elif nom != "officielle":
        raise ValueError(f"variante inconnue : {nom}")


def ordre_initial(trade):
    """L'ordre tel qu'il a été validé, avant exécution, stop remonté ou vente."""
    ordre = {c: trade[c] for c in CHAMPS_ORDRE if c in trade}
    ordre.update(stop=trade.get("stop_initial", trade["stop"]), objectif=trade.get("objectif_initial", trade["objectif"]),
                 seances=0, traite_jusqu_au=trade["date_signal"], etat=EN_ATTENTE, historique=[])
    return ordre


def rejouer(trade, barres, taux, p, nom="officielle"):
    """Résultat de la variante `nom` ; barres = barres journalières terminées du titre, taux = {jour: EUR/USD}.

    Renvoie {"statut": "ferme" | "non_execute" | "en_cours" | "non_rejoue", ...}.
    """
    if trade.get("splits"):
        return {"statut": "non_rejoue", "raison": "division d'actions pendant le trade"}
    ordre, p = ordre_initial(trade), copy.deepcopy(p)
    _changer(nom, ordre, p)
    etat = {"capital_initial": p["capital_initial"], "ordres": [ordre], "positions": [], "fermees": [],
            "ordres_clos": []}
    defaut = trade.get("taux_entree") or 1.0
    for b in sorted((b for b in barres if b["jour"].isoformat() > trade["date_signal"]), key=lambda b: b["jour"]):
        avancer(etat, {trade["ticker"]: [b]}, taux.get(b["jour"], defaut), p)
        if not etat["ordres"] and not etat["positions"]:
            break
    if etat["fermees"]:
        f = etat["fermees"][0]
        return {"statut": "ferme", "prix_entree": round(f["prix_entree"], 4), "date_sortie": f["date_sortie"],
                "motif": f["motif"], "resultat_eur": f["resultat_eur"], "resultat_r": f["resultat_r"],
                "duree_seances": f["duree_seances"]}
    if etat["positions"]:
        return {"statut": "en_cours"}
    return {"statut": "non_execute", "raison": etat["ordres_clos"][0]["etat"] if etat["ordres_clos"] else "données"}


def mesures_trade(trade):
    """Fiche d'un trade fermé : prévu et exécuté, frais, résultat en € et en R, extrêmes, événements."""
    pendant = [h for h in trade.get("historique", [])
               if h["evenement"] not in (EXECUTE, CLOTURE) and trade["date_entree"] <= h["jour"] <= trade["date_sortie"]]
    prevu, execute = trade["entree"], trade["prix_entree"]
    return {
        "id": trade.get("id"), "ticker": trade["ticker"], "configuration": trade.get("configuration"),
        "date_signal": trade["date_signal"], "date_entree": trade["date_entree"], "date_sortie": trade["date_sortie"],
        "entree_prevue": prevu, "entree_executee": round(execute, 4),
        "ecart_entree_pct": round((execute / prevu - 1) * 100, 2) if prevu else None,
        "stop_initial": trade.get("stop_initial"), "objectif_initial": trade.get("objectif_initial"),
        "prix_sortie": round(trade["prix_sortie"], 4), "motif": trade["motif"],
        "frais_eur": trade.get("frais_eur"), "glissement_eur": trade.get("glissement_eur"),
        "resultat_eur": trade["resultat_eur"], "resultat_r": trade.get("resultat_r"),
        "duree_seances": trade.get("duree_seances"), "mfe_r": trade.get("mfe_r"), "mae_r": trade.get("mae_r"),
        # Ce qui était connu au moment de la décision, puis ce qui est arrivé pendant le trade
        "connu_avant": {"date_resultats": trade.get("date_resultats")},
        "pendant": pendant,
    }


def synthese(analyses):
    """Moyennes par variante sur les trades rejoués ; écart en R face au résultat officiel, trade par trade."""
    resume = {}
    for nom in VARIANTES:
        fermes = [(a["officiel"]["resultat_r"], a["variantes"][nom]["resultat_r"]) for a in analyses
                  if a["variantes"].get(nom, {}).get("statut") == "ferme"
                  and a["variantes"][nom]["resultat_r"] is not None and a["officiel"]["resultat_r"] is not None]
        ecarts = [v - o for o, v in fermes]
        resume[nom] = {
            "description": VARIANTES[nom], "trades": len(fermes),
            "non_executes": sum(1 for a in analyses if a["variantes"].get(nom, {}).get("statut") == "non_execute"),
            "r_moyen": round(sum(v for _, v in fermes) / len(fermes), 2) if fermes else None,
            "ecart_r_moyen": round(sum(ecarts) / len(ecarts), 2) if ecarts else None,
            "echantillon_faible": len(fermes) < ECHANTILLON_MIN,
        }
    return resume
