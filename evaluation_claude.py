#!/usr/bin/env python3
"""Évalue les décisions de Claude sur le portefeuille fictif : candidats validés, rejetés, et ce qu'ils sont devenus.

Chaque candidat d'une session (agent/decisions/) est rejoué avec les mêmes règles, qu'il ait été validé ou non :
on compare la suite des validés, des rejetés et de tous les candidats (= la stratégie sans IA). Écrit
agent/analyses/claude.json. Aucun réglage n'est modifié.

Usage : python evaluation_claude.py
"""
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from analyse_trades import telecharger
from commun import charger_config
from court_terme.analyse import rejouer
from court_terme.evaluation import bilan
from court_terme.portefeuille import charger, quantite, sauver
from court_terme.preglages import parametres
from court_terme.stats import ECHANTILLON_MIN

AGENT = Path(__file__).with_name("agent")
SORTIE = AGENT / "analyses" / "claude.json"
NOMS = {"ordres_crees": "Ordres créés (validés par Claude)", "rejetes": "Rejetés par Claude",
        "valides_refuses_par_le_code": "Validés mais refusés par le code", "tous": "Tous les candidats (sans IA)"}


def lire_decisions(dossier=AGENT / "decisions"):
    decisions = []
    for chemin in sorted(dossier.glob("*.json")):
        try:
            decisions.append(json.loads(chemin.read_text()))
        except (OSError, ValueError):
            print(f"Fichier illisible ignoré : {chemin.name}", file=sys.stderr)
    return decisions


def rejouer_candidats(decisions, barres, taux, p):
    rejoues = {}
    for d in decisions:
        for c in d["candidats"]:
            if c.get("entree") is None or c.get("stop") is None or c.get("objectif") is None:
                continue
            t = taux.get(date.fromisoformat(d["jour"]), 1.0)
            ordre = {**c, "date_signal": d["jour"], "quantite": quantite(c["entree"], c["stop"], p["capital_initial"], t, p)}
            rejoues[f"{d['jour']}:{c['ticker']}"] = rejouer(ordre, barres.get(c["ticker"], []), taux, p)
    return rejoues


def main():
    decisions = [d for d in lire_decisions() if d.get("session_reussie")]
    candidats = {c["ticker"] for d in decisions for c in d["candidats"]}
    if not candidats:
        print("Aucune session de Claude avec des candidats pour l'instant : rien à évaluer.")
        return 0
    p = parametres(charger_config(), "amelioree")
    debut = date.fromisoformat(min(d["jour"] for d in decisions)) - timedelta(days=5)
    barres, taux = telecharger(candidats, debut)
    resultat = bilan(decisions, rejouer_candidats(decisions, barres, taux, p),
                     charger(AGENT / "positions.json", p)["fermees"], ECHANTILLON_MIN)
    sauver(resultat, SORTIE)
    print(f"{resultat['sessions']} session(s) évaluée(s) → {SORTIE.relative_to(AGENT.parent)}\n")
    for nom, g in resultat["groupes"].items():
        r = "—" if g["r_moyen"] is None else f"{g['r_moyen']:+.2f} R en moyenne, {g['gagnants_pct']:.0f} % gagnants"
        print(f"- {NOMS[nom]} : {g['candidats']} candidat(s), {g['trades']} trade(s) rejoué(s) ; {r}")
    if resultat["apport_r_par_trade"] is not None:
        print(f"\nApport de Claude face à la stratégie sans IA : {resultat['apport_r_par_trade']:+.2f} R par trade")
    c = resultat["cout_total"]
    print(f"Coût total : {c['tours']} tours, {c['duree_s'] // 60} min, {c['WebSearch']} recherches, "
          f"{c['WebFetch']} pages lues, {c['cout_usd']} $ (coût équivalent API, inclus dans l'abonnement)")
    for titre, liste in (("Incohérences", resultat["incoherences"]), ("À relire", resultat["a_relire"])):
        if liste:
            print(f"\n{titre} :\n- " + "\n- ".join(liste))
    if resultat["echantillon_faible"]:
        print(f"\nAttention : moins de {ECHANTILLON_MIN} trades. Un bon trade n'est pas une bonne décision : "
              "ces chiffres tiennent surtout du hasard tant que l'échantillon est petit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
