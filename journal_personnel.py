#!/usr/bin/env python3
"""Journal personnel facultatif : vos propres opérations réelles, saisies à la main, à part du portefeuille fictif.

Aucun accès à un courtier, aucun ordre passé : ce script ne fait qu'enregistrer ce que vous tapez. Les données restent
sur votre ordinateur, dans personnel/journal.json (ignoré par git, jamais commité ni envoyé). Le bilan ne reprend
que vos saisies : la performance du portefeuille fictif n'y figure jamais et n'est pas la vôtre.

Usage :
  python journal_personnel.py ajouter TICKER achat|vente PRIX QUANTITE [--date AAAA-MM-JJ] [--frais 0]
                                      [--devise USD] [--ref RÉF_DU_SIGNAL] [--note "texte"]
  python journal_personnel.py liste
  python journal_personnel.py bilan
"""
import argparse
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

from court_terme.portefeuille import sauver
from court_terme.validation import TexteRefuse, nettoyer, ticker

JOURNAL = Path(__file__).with_name("personnel") / "journal.json"


def lire(chemin=None):
    try:
        return json.loads((chemin or JOURNAL).read_text())
    except (OSError, ValueError):
        return {"operations": []}


def ajouter(operation, chemin=None):
    journal = lire(chemin)
    journal["operations"].append(operation)
    sauver(journal, chemin or JOURNAL)
    return journal


def bilan(operations):
    """Résultat réalisé par titre (prix moyen d'achat), quantité restante, frais ; dans la devise saisie."""
    titres = defaultdict(lambda: {"quantite": 0.0, "cout": 0.0, "realise": 0.0, "frais": 0.0, "devise": None})
    alertes = []
    for o in sorted(operations, key=lambda o: o["date"]):
        t = titres[o["ticker"]]
        t["devise"] = t["devise"] or o["devise"]
        t["frais"] += o["frais"]
        if o["sens"] == "achat":
            t["quantite"] += o["quantite"]
            t["cout"] += o["prix"] * o["quantite"]
            continue
        if o["quantite"] > t["quantite"] + 1e-9:
            alertes.append(f"{o['date']} {o['ticker']} : vente de {o['quantite']:g} pour {t['quantite']:g} détenue(s)")
        vendue = min(o["quantite"], t["quantite"])
        moyen = t["cout"] / t["quantite"] if t["quantite"] else 0
        t["realise"] += (o["prix"] - moyen) * vendue
        t["cout"] -= moyen * vendue
        t["quantite"] -= vendue
    return {k: {**v, "realise_net": round(v["realise"] - v["frais"], 2)} for k, v in titres.items()}, alertes


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sous = parser.add_subparsers(dest="commande", required=True)
    a = sous.add_parser("ajouter")
    a.add_argument("ticker")
    a.add_argument("sens", choices=["achat", "vente"])
    a.add_argument("prix", type=float)
    a.add_argument("quantite", type=float)
    a.add_argument("--date", type=date.fromisoformat, default=date.today())
    a.add_argument("--frais", type=float, default=0.0)
    a.add_argument("--devise", default="USD")
    a.add_argument("--ref", help="référence du signal fictif qui vous a inspiré, s'il y en a un")
    a.add_argument("--note", default="")
    sous.add_parser("liste")
    sous.add_parser("bilan")
    args = parser.parse_args(arguments)

    if args.commande == "ajouter":
        try:
            valeur = ticker(args.ticker)
        except TexteRefuse as erreur:
            print(f"Refusé : {erreur}")
            return 1
        if args.prix <= 0 or args.quantite <= 0 or args.frais < 0:
            print("Refusé : le prix et la quantité doivent être positifs, les frais positifs ou nuls.")
            return 1
        ajouter({"date": args.date.isoformat(), "ticker": valeur, "sens": args.sens, "prix": args.prix,
                 "quantite": args.quantite, "frais": args.frais, "devise": args.devise.upper()[:3],
                 "ref_signal_fictif": nettoyer(args.ref, 60, une_ligne=True) if args.ref else None,
                 "note": nettoyer(args.note, 500)})
        print(f"Noté dans {JOURNAL} (sur votre ordinateur uniquement). Aucun ordre n'a été passé.")
        return 0
    operations = lire()["operations"]
    if not operations:
        print("Journal personnel vide.")
        return 0
    if args.commande == "liste":
        for o in sorted(operations, key=lambda o: o["date"]):
            ref = f" · réf. {o['ref_signal_fictif']}" if o.get("ref_signal_fictif") else ""
            print(f"{o['date']} {o['sens']} {o['quantite']:g} {o['ticker']} à {o['prix']:.2f} {o['devise']}"
                  f" (frais {o['frais']:.2f}){ref}{' — ' + o['note'] if o['note'] else ''}")
        return 0
    titres, alertes = bilan(operations)
    print("Vos opérations réelles, telles que vous les avez saisies (le portefeuille fictif n'y figure pas) :")
    for nom, t in sorted(titres.items()):
        print(f"- {nom} : résultat réalisé {t['realise_net']:+.2f} {t['devise']} après {t['frais']:.2f} de frais ; "
              f"{t['quantite']:g} encore détenue(s)")
    for alerte in alertes:
        print(f"⚠️ {alerte}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
