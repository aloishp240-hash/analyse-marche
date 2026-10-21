#!/usr/bin/env python3
"""Portefeuille fictif momentum : décision de fin de mois après la clôture, exécution à l'ouverture suivante.

Lancé toutes les 5 minutes par GitHub Actions, à côté de positions.py ; sort aussitôt s'il n'y a rien à faire.
N'envoie rien sur Telegram : les messages ne vont que dans le journal du workflow ; on suit le portefeuille
sur le tableau de bord.
Après la clôture de chaque séance, relève aussi la valeur du portefeuille pour la courbe du tableau de bord.
Usage : python momentum.py [--force] [--dry-run]
  --force    décide tout de suite sur la dernière séance, même hors fin de mois
  --dry-run  ne sauve pas l'état
"""
import argparse
import copy
import json
from datetime import datetime, time
from pathlib import Path

import yfinance as yf

from commun import INDICE, NY, charger_config, charger_tickers, nom_societe
from court_terme.momentum import (classement, decider, doit_decider, executer, message_decision, message_execution,
                                  nouvel_etat, relever, valeur)
from court_terme.portefeuille import sauver
from positions import taux_eurusd
from surveillance import seance_ouverte

ETAT = Path(__file__).resolve().parent / "agent" / "momentum.json"
HEURE_RELEVE = time(16, 15)
DEBUT_EXECUTION = time(9, 35)  # première bougie de 5 min terminée


def charger(p):
    return json.loads(ETAT.read_text()) if ETAT.exists() else nouvel_etat(p)


def ouvertures(tickers, jour):
    """Prix d'ouverture du jour (première bougie de 5 min), pour les titres qui ont déjà coté."""
    df = yf.download(tickers, period="1d", interval="5m", auto_adjust=False, progress=False)["Open"]
    index = df.index.tz_convert(NY) if df.index.tz else df.index.tz_localize(NY)
    df = df[index.date == jour]
    return {t: float(df[t].dropna().iloc[0]) for t in tickers if t in df and df[t].notna().any()}


def derniers_cours(tickers):
    """(date de la dernière séance de QQQ, dernière clôture non ajustée de chaque titre)."""
    df = yf.download(sorted(set(tickers) | {INDICE}), period="5d", interval="1d", auto_adjust=False,
                     progress=False)["Close"]
    return df[INDICE].dropna().index[-1].date(), {t: float(df[t].dropna().iloc[-1]) for t in df if df[t].notna().any()}


def noms_de(tickers):
    """Noms usuels lus directement (le cache des fondamentaux peut dater d'avant l'ajout des noms)."""
    noms = {}
    for t in tickers:
        try:
            noms[t] = nom_societe(yf.Ticker(t).info, t)
        except Exception:
            noms[t] = t
    return noms


def executer_decision(etat, maintenant, p):
    d = etat["decision"]
    ouv = ouvertures(d["ventes"] + d["achats"] + [INDICE], maintenant.date())
    if INDICE not in ouv:
        return  # pas encore de cotation aujourd'hui (ou jour férié)
    manquants = [t for t in d["ventes"] if t not in ouv]
    if manquants:  # titre qui ne cote plus (racheté, suspendu) : vendu à son dernier cours
        ouv |= derniers_cours(manquants)[1]
    noms = {l["ticker"]: l["nom"] for l in etat["lignes"]} | {c["ticker"]: c["nom"] for c in d["classement"]}
    etat["qqq_depart"] = etat["qqq_depart"] or ouv[INDICE]
    ops = executer(etat, ouv, taux_eurusd(), maintenant.date(), noms, p)
    print(message_execution(ops, etat))


def apres_cloture(etat, maintenant, p, force):
    decide = force or doit_decider(etat, maintenant.date())
    if not decide and not etat["lignes"]:
        etat["dernier_releve"] = maintenant.date().isoformat()
        return
    detenus = [l["ticker"] for l in etat["lignes"]]
    jour, cours = derniers_cours(detenus)
    if jour != maintenant.date() and not force:
        etat["dernier_releve"] = maintenant.date().isoformat()
        return  # pas de séance aujourd'hui
    taux = taux_eurusd()
    if etat["debut"]:
        relever(etat, jour, valeur(etat, cours, taux), cours[INDICE])
    else:
        etat["dernier_releve"] = jour.isoformat()
    if not decide:
        return
    tickers = sorted(set(charger_tickers()) | set(detenus))
    clotures = yf.download(tickers + [INDICE], period="15mo", interval="1d", auto_adjust=True,
                           progress=False, threads=True)["Close"]
    clotures = clotures[clotures.index.date <= jour]
    porteur, meilleurs = classement(clotures[tickers], clotures[INDICE].dropna(), p)
    noms = noms_de(sorted({t for t, _ in meilleurs} | set(detenus)))
    decision = decider(etat, porteur, meilleurs, jour, noms)
    cours |= {t: float(clotures[t].dropna().iloc[-1]) for t in decision["achats"]}  # dernière clôture, pour info
    print(message_decision(decision, etat, cours, taux, noms))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    maintenant = datetime.now(NY)
    p = charger_config()["momentum"]
    etat = charger(p)
    avant = copy.deepcopy(etat)

    if (etat["decision"] and seance_ouverte(maintenant) and maintenant.time() >= DEBUT_EXECUTION
            and etat["decision"]["date"] < maintenant.date().isoformat()):
        executer_decision(etat, maintenant, p)
    elif args.force or (maintenant.weekday() < 5 and maintenant.time() >= HEURE_RELEVE
                        and etat["dernier_releve"] != maintenant.date().isoformat()):
        apres_cloture(etat, maintenant, p, args.force)

    if etat != avant and not args.dry_run:
        sauver(etat, ETAT)


if __name__ == "__main__":
    main()
