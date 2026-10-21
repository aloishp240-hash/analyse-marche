"""Backtest de la stratégie court terme sur l'historique du Nasdaq 100, sans Claude.

Usage : python backtest.py [--annees 3 | --mois 3 | --debut AAAA-MM-JJ [--fin AAAA-MM-JJ]]
                          [--configurations A|B|AB] [--preglage amelioree|reference|tous]
                          [--frais-pct X] [--glissement-pct X] [--memes-frais] [--enregistrer]
  --mois N          N périodes d'un mois jusqu'à la dernière séance terminée, chacune avec un portefeuille neuf
  --debut / --fin   une période précise (par exemple 2018-01-01 → 2022-12-31 pour régler, 2023 → 2026 pour tester)
  --preglage        reference = stratégie du commit 04973f7 (sans frais) ; amelioree = config.json ; tous = les deux
  --frais-pct, --glissement-pct  remplacent ceux du préréglage (pour tester des frais plus lourds)
  --memes-frais     la référence reçoit les frais et le glissement de la version améliorée (comparaison à frais égaux)
  --enregistrer     ajoute paramètres exacts, période et mesures à experiences/registre.json (jamais réécrit)
"""
import argparse
import bisect
import json
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

from commun import INDICE, NY, charger_config, charger_tickers, fondamentaux
from court_terme.portefeuille import ajouter_ordre, capital_courant
from court_terme.preglages import NOMS, VERSIONS, parametres
from court_terme.simulation import annuler_avant_resultats, avancer
from court_terme.stats import ECHANTILLON_MIN, _euros, mesures, statistiques
from court_terme.strategie import reperer

DEVISE = "EURUSD=X"
FENETRE = 260  # séances passées à reperer : assez pour la MM200 et la performance sur 3 mois
MODELE = Path(__file__).with_name("rapport_backtest.html")
RAPPORT = Path(__file__).with_name("donnees") / "backtest.html"
REGISTRE = Path(__file__).with_name("experiences") / "registre.json"
FRAIS = ("frais_fixes_eur", "frais_pct", "glissement_pct")
ECART_RESULTATS_MAX = 120  # jours : au-delà, une date de résultats manque dans l'historique Yahoo


def dates_resultats(tickers, limite=20):
    """Dates de résultats passées et à venir par titre (triées) ; titres en échec à part."""
    def un_ticker(ticker):
        try:
            df = yf.Ticker(ticker).get_earnings_dates(limit=limite)
            return ticker, sorted({d.date() for d in df.index}) if df is not None else None
        except Exception:
            return ticker, None

    with ThreadPoolExecutor(8) as pool:
        dates = dict(pool.map(un_ticker, tickers))
    return {t: d for t, d in dates.items() if d}, [t for t, d in dates.items() if not d]


def prochains(dates, jour):
    """Prochaine date de résultats connue par titre. Une date à plus de ECART_RESULTATS_MAX jours (résultats
    trimestriels : environ 91 jours d'écart) veut dire qu'une date manque : le titre est laissé sans date."""
    resultats = {}
    for ticker, liste in dates.items():
        i = bisect.bisect_left(liste, jour)
        if i < len(liste) and (liste[i] - jour).days <= ECART_RESULTATS_MAX:
            resultats[ticker] = {"date": liste[i].isoformat()}
    return resultats


def barre(df, ticker, i):
    valeurs = {cle: df[champ][ticker].iloc[i] for cle, champ in
               (("ouverture", "Open"), ("haut", "High"), ("bas", "Low"), ("cloture", "Close"))}
    if any(pd.isna(v) for v in valeurs.values()):
        return None
    return {"jour": df.index[i].date(), "terminee": True, **{k: float(v) for k, v in valeurs.items()}}


def periodes(index, mois):
    """Bornes (début, fin exclue) de `mois` périodes d'un mois consécutives finissant à la dernière séance."""
    bornes, fin = [], len(index)
    for _ in range(mois):
        debut = index.searchsorted(index[fin - 1] - pd.DateOffset(months=1), side="right")
        bornes.append((debut, fin))
        fin = debut
    return bornes[::-1]


def valeur(df, taux, etat, i):
    """Capital réalisé plus latent des positions ouvertes, valorisées à la clôture de la séance i."""
    latent = sum((float(df["Close"][pos["ticker"]].iloc[i]) - pos["prix_entree"]) * pos["quantite"]
                 for pos in etat["positions"]) / float(taux.iloc[i])
    return capital_courant(etat) + latent


def simuler(df, taux, tickers, industries, dates, p, debut, fin):
    """État final, compteurs d'événements et courbe quotidienne du portefeuille face à QQQ (même capital)."""
    etat = {"capital_initial": p["capital_initial"], "debut": None, "ordres": [], "positions": [], "fermees": []}
    compte, courbe = Counter(), []
    indice = df["Close"][INDICE]
    for i in range(debut, fin):
        jour = df.index[i].date()
        suivis = {e["ticker"] for e in etat["ordres"] + etat["positions"]}
        barres = {t: [b] for t in suivis if (b := barre(df, t, i))}
        compte.update(e["type"] for e in avancer(etat, barres, float(taux.iloc[i]), p))
        resultats = prochains(dates, jour)
        compte.update(e["type"] for e in annuler_avant_resultats(etat, jour, resultats, p))
        fenetre = df.iloc[i - FENETRE + 1:i + 1]
        suivis = {e["ticker"] for e in etat["ordres"] + etat["positions"]}
        for candidat in reperer(fenetre, tickers, resultats, jour, p, suivis):
            try:
                ajouter_ordre(etat, candidat, industries.get(candidat["ticker"]), "backtest", jour,
                              float(taux.iloc[i]), p)
                compte["signal"] += 1
            except ValueError:
                compte["refus"] += 1
        courbe.append({"jour": jour.isoformat(), "valeur": round(valeur(df, taux, etat, i), 2),
                       "qqq": round(p["capital_initial"] * indice.iloc[i] / indice.iloc[debut - 1], 2)})
    return etat, compte, courbe


def afficher(df, taux, etat, compte, debut, fin, p, configurations, courbe=None):
    """Bilan de la période ; les positions encore ouvertes sont valorisées à la dernière clôture."""
    s = statistiques(etat["fermees"])
    latent = valeur(df, taux, etat, fin - 1) - capital_courant(etat)
    indice = df["Close"][INDICE]
    perf_indice = (indice.iloc[fin - 1] / indice.iloc[debut - 1] - 1) * 100
    perf = ((capital_courant(etat) + latent) / p["capital_initial"] - 1) * 100
    print(f"Période : {df.index[debut].date()} → {df.index[fin - 1].date()} · configurations {configurations}")
    print(f"Signaux : {compte['signal']} · refusés par les plafonds : {compte['refus']} · "
          f"ordres expirés : {compte['expire']} · annulés : {compte['annule']}")
    print(f"Positions fermées : {s['nombre']} (stop {compte['stop']}, objectif {compte['objectif']}, "
          f"temps {compte['temps']}) · encore ouvertes : {len(etat['positions'])}")
    if s["nombre"]:
        print(f"Taux de réussite : {s['taux_reussite']} % · gain moyen {_euros(s['gain_moyen'])} · "
              f"perte moyenne {_euros(s['perte_moyenne'])}")
        print(f"Espérance par position : {s['esperance']:+.2f} € · total réalisé {s['resultat_total']:+.2f} €")
    print(f"Capital réalisé : {capital_courant(etat):.2f} € · latent {latent:+.2f} € · valeur "
          f"{capital_courant(etat) + latent:.2f} € ({perf:+.2f} %) · QQQ sur la période : {perf_indice:+.2f} %")
    if not courbe:
        return
    m = mesures(etat["fermees"], courbe, p["capital_initial"])
    print(f"Avant frais {m['brut_total']:+.2f} € · frais {m['frais_total']:.2f} € · après frais "
          f"{m['resultat_total']:+.2f} €")
    print(f"R moyen {m['r_moyen'] if m['r_moyen'] is not None else '—'} · facteur de profit "
          f"{m['facteur_profit'] if m['facteur_profit'] is not None else '—'} · baisse maximale "
          f"{m['baisse_max_pct']} % (QQQ {m['baisse_max_qqq_pct']} %) · rendement annualisé "
          f"{m['annualise_pct']} % (QQQ {m['annualise_qqq_pct']} %)")
    if m["echantillon_faible"]:
        print(f"⚠️ Moins de {ECHANTILLON_MIN} positions fermées : résultats trop peu nombreux pour conclure.")


def periode(df, etat, compte, courbe, debut, fin):
    """Données d'une période pour le rapport HTML."""
    champs = ("ticker", "configuration", "date_entree", "prix_entree", "quantite", "stop_initial", "objectif")
    return {
        "debut": df.index[debut].date().isoformat(), "fin": df.index[fin - 1].date().isoformat(),
        "capital_initial": etat["capital_initial"], "signaux": compte["signal"], "refus": compte["refus"],
        "stats": statistiques(etat["fermees"]), "mesures": mesures(etat["fermees"], courbe, etat["capital_initial"]),
        "courbe": courbe,
        "fermees": [{**{c: f.get(c) for c in champs}, "date_sortie": f["date_sortie"],
                     "prix_sortie": f["prix_sortie"], "motif": f["motif"], "resultat_eur": f["resultat_eur"]}
                    for f in etat["fermees"]],
        "ouvertes": [{**{c: pos.get(c) for c in champs},
                      "cours": round(float(df["Close"][pos["ticker"]].iloc[fin - 1]), 2)}
                     for pos in etat["positions"]],
    }


def ecrire_rapport(periodes, configurations, chemin=RAPPORT):
    """Insère les données dans le modèle HTML ; « </ » est échappé pour ne pas fermer la balise script."""
    donnees = json.dumps({"configurations": configurations, "periodes": periodes}, default=str, ensure_ascii=False)
    chemin = Path(chemin)
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_text(MODELE.read_text().replace("__DONNEES__", donnees.replace("</", "<\\/")))
    return chemin


def _git(*commande):
    try:
        return subprocess.run(["git", *commande], capture_output=True, text=True, cwd=Path(__file__).parent).stdout.strip()
    except OSError:
        return ""


def enregistrer(rapport, reglages, chemin=REGISTRE):
    """Ajoute chaque période testée au registre des expériences ; les entrées passées ne sont jamais modifiées."""
    try:
        registre = json.loads(chemin.read_text())
    except (OSError, ValueError):
        registre = []
    commit, modifie = _git("rev-parse", "--short", "HEAD") or None, bool(_git("status", "--porcelain"))
    for r in rapport:
        versions = [v["id"] for v in VERSIONS if v["preglage"] == r["preglage"]]
        registre.append({
            "date": datetime.now(NY).isoformat(timespec="minutes"), "version": versions[-1] if versions else None,
            "commit": commit, "code_modifie_depuis_le_commit": modifie, "commande": " ".join(sys.argv[1:]),
            "preglage": r["preglage"], "periode": [r["debut"], r["fin"]], "parametres": reglages[r["preglage"]],
            "signaux": r["signaux"], "refus": r["refus"], "mesures": r["mesures"],
        })
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_text(json.dumps(registre, ensure_ascii=False, indent=1))
    return chemin


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--annees", type=int, default=3)
    parser.add_argument("--mois", type=int)
    parser.add_argument("--debut", type=date.fromisoformat)
    parser.add_argument("--fin", type=date.fromisoformat)
    parser.add_argument("--configurations", default="A", choices=["A", "B", "AB"])
    parser.add_argument("--preglage", default="amelioree", choices=[*NOMS, "tous"])
    parser.add_argument("--frais-pct", type=float)
    parser.add_argument("--glissement-pct", type=float)
    parser.add_argument("--memes-frais", action="store_true")
    parser.add_argument("--enregistrer", action="store_true")
    args = parser.parse_args()

    config = charger_config()
    reglages = {}
    for nom in (NOMS if args.preglage == "tous" else [args.preglage]):
        p = parametres(config, nom)
        p["configurations"] = {c: c in args.configurations for c in "AB"}
        if args.memes_frais:
            p.update({cle: parametres(config, "amelioree")[cle] for cle in FRAIS})
        if args.frais_pct is not None:
            p["frais_pct"] = args.frais_pct
        if args.glissement_pct is not None:
            p["glissement_pct"] = args.glissement_pct
        reglages[nom] = p
    tickers = charger_tickers()

    print("Biais à garder en tête :")
    print("- la composition actuelle du Nasdaq 100 est appliquée au passé (les titres sortis de l'indice manquent :")
    print("  souvent ceux qui ont chuté, ce qui embellit les résultats) ;")
    print("- les dates de résultats passées viennent de Yahoo et sont approximatives ;")
    print("- chaque candidat est pris sans validation par Claude ; industries actuelles appliquées au passé.\n")

    if args.debut:
        # Assez d'historique avant le début pour la MM200 et la performance sur 3 mois
        debut_donnees = args.debut - timedelta(days=420)
        fin_donnees = (args.fin or date.today()) + timedelta(days=1)
        df = yf.download(tickers + [INDICE, DEVISE], start=debut_donnees, end=fin_donnees, interval="1d",
                         auto_adjust=False, progress=False, threads=True)
        annees = (fin_donnees - debut_donnees).days / 365
    else:
        annees = 1 + -(-args.mois // 12) if args.mois else args.annees + 1
        df = yf.download(tickers + [INDICE, DEVISE], period=f"{annees}y", interval="1d",
                         auto_adjust=False, progress=False, threads=True)
    df = df[df["Close"][INDICE].notna()]
    maintenant = datetime.now(NY)
    if df.index[-1].date() == maintenant.date() and maintenant.time() < time(16):
        df = df.iloc[:-1]  # séance en cours : barre incomplète
    taux = df["Close"][DEVISE].ffill().bfill()
    industries = {t: f.get("industry") for t, f in fondamentaux(tickers).items()}
    dates, sans_dates = dates_resultats(tickers, limite=max(20, int(4 * annees) + 12))
    if sans_dates:
        print(f"Dates de résultats indisponibles pour {len(sans_dates)} titres : {' '.join(sans_dates)}\n")

    if args.debut:
        debut = max(FENETRE, int(df.index.searchsorted(pd.Timestamp(args.debut))))
        fin = int(df.index.searchsorted(pd.Timestamp(args.fin), side="right")) if args.fin else len(df)
        bornes = [(debut, fin)]
    elif args.mois:
        bornes = [(FENETRE + d, FENETRE + f) for d, f in periodes(df.index[FENETRE:], args.mois)]
    else:
        bornes = [(max(FENETRE, len(df) - args.annees * 252), len(df))]
    rapport = []
    for nom, p in reglages.items():
        print(f"===== Préréglage {nom} (frais {p['frais_fixes_eur']} € + {p['frais_pct']} %, "
              f"glissement {p['glissement_pct']} %) =====")
        for debut, fin in bornes:
            etat, compte, courbe = simuler(df, taux, tickers, industries, dates, p, debut, fin)
            afficher(df, taux, etat, compte, debut, fin, p, args.configurations, courbe)
            print()
            rapport.append({**periode(df, etat, compte, courbe, debut, fin), "preglage": nom})
    print(f"Rapport : {ecrire_rapport(rapport, args.configurations)}")
    if args.enregistrer:
        print(f"Registre : {enregistrer(rapport, reglages)}")


if __name__ == "__main__":
    main()
