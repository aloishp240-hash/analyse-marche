"""Simulation des exécutions du portefeuille fictif à partir de barres journalières.

Une barre : {"jour": date, "ouverture", "haut", "bas", "cloture", "terminee": bool}.
Une barre non terminée (séance en cours) peut déclencher une entrée ou une sortie,
mais ne fait avancer ni le compteur de séances ni traite_jusqu_au, ni remonter le stop :
la repasser ne rejoue rien.
"""


def _evenement(type_, element, jour, prix, **autres):
    return {"type": type_, "ticker": element["ticker"], "jour": jour.isoformat(), "prix": prix, **autres}


def _fermer(etat, pos, jour, prix, motif, taux_eurusd):
    resultat = round((prix - pos["prix_entree"]) * pos["quantite"] / taux_eurusd, 2)
    etat["positions"].remove(pos)
    etat["fermees"].append({**pos, "date_sortie": jour.isoformat(), "prix_sortie": prix, "motif": motif,
                            "resultat_eur": resultat})
    return _evenement(motif, pos, jour, prix, resultat_eur=resultat)


def _sortie(pos, b):
    """(motif, prix) si la barre fait sortir la position, sinon None. Le stop l'emporte sur l'objectif."""
    if b["jour"].isoformat() != pos["date_entree"]:
        # Le jour d'entrée, l'ouverture est déjà passée : pas de sortie sur écart d'ouverture
        if b["ouverture"] <= pos["stop"]:
            return "stop", b["ouverture"]
        if b["ouverture"] >= pos["objectif"]:
            return "objectif", b["ouverture"]
    if b["bas"] <= pos["stop"]:
        return "stop", min(pos["stop"], pos["prix_entree"])
    if b["haut"] >= pos["objectif"]:
        return "objectif", pos["objectif"]
    return None


def _traiter_position(etat, pos, b, taux_eurusd, p):
    sortie = _sortie(pos, b)
    if sortie:
        return [_fermer(etat, pos, b["jour"], sortie[1], sortie[0], taux_eurusd)], True
    evts = []
    # Sur une barre partielle, le plus bas peut précéder la hausse : le stop ne remonte qu'en fin de séance
    if b["terminee"] and b["haut"] >= pos["prix_entree"] + p["remontee_stop_atr"] * pos["atr"] and pos["stop"] < pos["prix_entree"]:
        pos["stop"] = pos["prix_entree"]
        evts.append(_evenement("stop_remonte", pos, b["jour"], pos["stop"]))
    if b["terminee"]:
        pos["seances"] += 1
        pos["traite_jusqu_au"] = b["jour"].isoformat()
        if pos["seances"] >= p["stop_temporel_seances"]:
            evts.append(_fermer(etat, pos, b["jour"], b["cloture"], "temps", taux_eurusd))
            return evts, True
    return evts, False


def _traiter_ordre(etat, ordre, barres, taux_eurusd, p):
    evts = []
    for i, b in enumerate(barres):
        if b["bas"] <= ordre["entree"]:
            prix = min(b["ouverture"], ordre["entree"])
            etat["ordres"].remove(ordre)
            pos = {**ordre, "prix_entree": prix, "date_entree": b["jour"].isoformat(), "stop_initial": ordre["stop"],
                   "seances": 0}
            etat["positions"].append(pos)
            evts.append(_evenement("entree", pos, b["jour"], prix))
            return evts + _parcourir_position(etat, pos, barres[i:], taux_eurusd, p)
        if b["terminee"]:
            ordre["seances"] += 1
            ordre["traite_jusqu_au"] = b["jour"].isoformat()
            if ordre["seances"] >= p["validite_ordre_seances"]:
                etat["ordres"].remove(ordre)
                return evts + [_evenement("expire", ordre, b["jour"], ordre["entree"])]
    return evts


def _parcourir_position(etat, pos, barres, taux_eurusd, p):
    evts = []
    for b in barres:
        nouveaux, fermee = _traiter_position(etat, pos, b, taux_eurusd, p)
        evts += nouveaux
        if fermee:
            break
    return evts


def avancer(etat, barres, taux_eurusd, p):
    """Applique les barres non encore traitées aux ordres et positions ; renvoie les événements."""
    def a_traiter(element):
        return sorted((b for b in barres.get(element["ticker"], []) if b["jour"].isoformat() > element["traite_jusqu_au"]),
                      key=lambda b: b["jour"])

    evts = []
    for ordre in list(etat["ordres"]):
        evts += _traiter_ordre(etat, ordre, a_traiter(ordre), taux_eurusd, p)
    nouvelles = {e["ticker"] for e in evts if e["type"] == "entree"}
    for pos in list(etat["positions"]):
        if pos["ticker"] not in nouvelles:
            evts += _parcourir_position(etat, pos, a_traiter(pos), taux_eurusd, p)
    return evts
