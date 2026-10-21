"""Second portefeuille fictif : rotation momentum mensuelle sur le Nasdaq 100.

Chaque fin de mois, après la clôture : si QQQ est au-dessus de sa moyenne 200 jours, on vise les 5 titres les plus
forts sur 12 mois hors dernier mois ; sinon, tout en liquidités. Ordres exécutés à l'ouverture de la séance suivante.
Les titres conservés ne sont pas rééquilibrés ; l'argent disponible est réparti à parts égales entre les entrants.
"""
import math
from datetime import timedelta

SEANCES_MOIS = 21
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre",
        "décembre"]


def nouvel_etat(p):
    return {"capital_initial": p["capital_initial"], "debut": None, "liquidites_eur": p["capital_initial"],
            "lignes": [], "decision": None, "derniere_decision": None, "dernier_releve": None, "qqq_depart": None,
            "operations": [], "historique": []}


def jour_ouvre_suivant(jour):
    suivant = jour + timedelta(days=1)
    while suivant.weekday() >= 5:
        suivant += timedelta(days=1)
    return suivant


def mois_attendu(jour):
    """Mois dont la décision doit être prise au plus tard ce jour-là : le mois en cours si c'est son dernier jour
    ouvré, sinon le précédent (rattrapage quand la dernière séance a été manquée, un jour férié par exemple)."""
    if jour_ouvre_suivant(jour).month != jour.month:
        return f"{jour:%Y-%m}"
    precedent = jour.replace(day=1) - timedelta(days=1)
    return f"{precedent:%Y-%m}"


def doit_decider(etat, jour):
    attendu = mois_attendu(jour)
    if etat["derniere_decision"] is None:     # démarrage : on attend une vraie fin de mois
        return attendu == f"{jour:%Y-%m}"
    return etat["derniere_decision"] < attendu


def classement(clotures, indice, p):
    """(marché porteur ?, [(ticker, performance en %)] des p["titres"] meilleurs), sur des clôtures ajustées
    qui finissent à la séance de décision."""
    debut, fin = p["mois_perf"] * SEANCES_MOIS + 1, p["mois_exclus"] * SEANCES_MOIS + 1
    porteur = bool(indice.iloc[-1] > indice.iloc[-p["filtre_mm"]:].mean()) if len(indice) >= p["filtre_mm"] else False
    if len(clotures) < debut:
        return porteur, []
    complet = clotures.iloc[-debut:].notna().all()          # historique complet sur la fenêtre
    perf = (clotures.iloc[-fin] / clotures.iloc[-debut] - 1)[complet].dropna()
    return porteur, [(t, round(100 * float(v), 1)) for t, v in perf.nlargest(p["titres"]).items()]


def decider(etat, porteur, meilleurs, jour, noms):
    detenus = [l["ticker"] for l in etat["lignes"]]
    cibles = [t for t, _ in meilleurs] if porteur else []
    decision = {"date": jour.isoformat(), "mois": mois_attendu(jour), "porteur": porteur,
                "classement": [{"ticker": t, "nom": noms.get(t, t), "perf": v} for t, v in meilleurs],
                "ventes": [t for t in detenus if t not in cibles], "achats": [t for t in cibles if t not in detenus],
                "conserves": [t for t in detenus if t in cibles]}
    etat["decision"] = decision if decision["ventes"] or decision["achats"] else None
    etat["derniere_decision"] = decision["mois"]
    return decision


def frais(montant_eur, p):
    return p["frais_fixe_eur"] + p["frais_pct"] / 100 * montant_eur


def executer(etat, ouvertures, taux_eurusd, jour, noms, p):
    """Ventes puis achats de la décision en attente, aux prix d'ouverture ; renvoie les opérations.

    Un titre à vendre sans ouverture connue est vendu à son dernier prix connu (`ouvertures` doit alors le fournir) ;
    un titre à acheter sans ouverture est abandonné jusqu'au mois suivant."""
    d, ops = etat["decision"], []
    for ligne in [l for l in etat["lignes"] if l["ticker"] in d["ventes"]]:
        prix = ouvertures[ligne["ticker"]]
        brut = ligne["quantite"] * prix / taux_eurusd
        f = frais(brut, p)
        etat["liquidites_eur"] += brut - f
        etat["lignes"].remove(ligne)
        ops.append({"date": jour.isoformat(), "type": "vente", "ticker": ligne["ticker"], "nom": ligne["nom"],
                    "quantite": ligne["quantite"], "prix": round(prix, 2), "montant_eur": round(brut, 2),
                    "frais_eur": round(f, 2), "resultat_eur": round(brut - f - ligne["cout_eur"], 2)})
    achats = [t for t in d["achats"] if ouvertures.get(t)]
    for t in achats:
        montant = etat["liquidites_eur"] / (len(achats) - achats.index(t))
        f = frais(montant, p)
        quantite = math.floor((montant - f) * taux_eurusd / ouvertures[t] * 1e4) / 1e4
        etat["liquidites_eur"] -= montant
        etat["lignes"].append({"ticker": t, "nom": noms.get(t, t), "quantite": quantite,
                               "prix_entree": round(ouvertures[t], 2), "date_entree": jour.isoformat(),
                               "cout_eur": round(montant, 2)})
        ops.append({"date": jour.isoformat(), "type": "achat", "ticker": t, "nom": noms.get(t, t),
                    "quantite": quantite, "prix": round(ouvertures[t], 2), "montant_eur": round(montant, 2),
                    "frais_eur": round(f, 2)})
    etat["liquidites_eur"] = round(etat["liquidites_eur"], 2)
    etat["operations"] += ops
    etat["decision"] = None
    etat["debut"] = etat["debut"] or jour.isoformat()
    return ops


def valeur(etat, cours, taux_eurusd):
    """Valeur en euros : liquidités + lignes au dernier cours (prix d'achat à défaut)."""
    return etat["liquidites_eur"] + sum(l["quantite"] * cours.get(l["ticker"], l["prix_entree"])
                                        for l in etat["lignes"]) / taux_eurusd


def relever(etat, jour, valeur_eur, qqq):
    """Point quotidien de la courbe ; la performance de QQQ part de son ouverture au premier achat."""
    perf_qqq = round(100 * (qqq / etat["qqq_depart"] - 1), 2) if etat["qqq_depart"] else 0.0
    etat["historique"].append({"date": jour.isoformat(), "valeur_eur": round(valeur_eur, 2), "qqq_pct": perf_qqq})
    etat["dernier_releve"] = jour.isoformat()


def _titre(t, noms):
    nom = noms.get(t, t)
    return f"{nom} ({t})" if nom != t else t


def message_decision(decision, etat, cours, taux_eurusd, noms):
    an, mois = decision["mois"].split("-")
    lignes = [f"🔄 ROTATION MOMENTUM — fin {MOIS[int(mois) - 1]} {an}"
              + ("" if decision["date"].startswith(decision["mois"]) else " (rattrapage)")]
    if decision["porteur"]:
        lignes.append("Marché : QQQ au-dessus de sa moyenne 200 jours → investi sur les 5 plus forts")
    else:
        lignes.append("Marché : QQQ sous sa moyenne 200 jours → tout en liquidités")
    perfs = {c["ticker"]: c["perf"] for c in decision["classement"]}

    def liste(tickers):
        return ", ".join(_titre(t, noms) + (f" {perfs[t]:+.0f} %" if t in perfs else "") for t in tickers)

    total = valeur(etat, cours, taux_eurusd)
    if decision["ventes"]:
        lignes.append(f"À vendre à la prochaine ouverture : {liste(decision['ventes'])}")
    if decision["achats"]:
        gardes = sum(l["quantite"] * cours.get(l["ticker"], l["prix_entree"]) for l in etat["lignes"]
                     if l["ticker"] in decision["conserves"]) / taux_eurusd
        lignes.append(f"À acheter à la prochaine ouverture (≈ {(total - gardes) / len(decision['achats']):.0f} € "
                      f"chacun) : {liste(decision['achats'])}")
    if decision["conserves"]:
        lignes.append(f"Conservés : {liste(decision['conserves'])}")
    if not decision["ventes"] and not decision["achats"]:
        lignes.append("Aucun changement ce mois-ci.")
    if decision["classement"]:
        lignes += ["", "Classement 12 mois hors dernier mois :"]
        lignes += [f"{i}. {_titre(c['ticker'], noms)} {c['perf']:+.0f} %" for i, c in enumerate(decision["classement"], 1)]
    perf = (total / etat["capital_initial"] - 1) * 100
    qqq = etat["historique"][-1]["qqq_pct"] if etat["historique"] else 0.0
    lignes += ["", f"Portefeuille : {total:.0f} € ({perf:+.1f} %) · QQQ {qqq:+.1f} % sur la même période"]
    return "\n".join(lignes)


def message_execution(ops, etat):
    lignes = ["✅ ROTATION MOMENTUM EXÉCUTÉE à l'ouverture"]
    for o in ops:
        t = f"{o['nom']} ({o['ticker']})" if o["nom"] != o["ticker"] else o["ticker"]
        if o["type"] == "vente":
            lignes.append(f"🔴 Vente {t} : {o['quantite']:g} à {o['prix']:.2f} $ · {o['resultat_eur']:+.2f} €")
        else:
            lignes.append(f"🟢 Achat {t} : {o['quantite']:g} à {o['prix']:.2f} $ (≈ {o['montant_eur']:.0f} €)")
    lignes.append(f"Frais : {sum(o['frais_eur'] for o in ops):.2f} € · liquidités restantes {etat['liquidites_eur']:.2f} €")
    return "\n".join(lignes)
