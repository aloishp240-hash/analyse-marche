"""Simulation des exécutions du portefeuille fictif à partir de barres journalières.

Une barre : {"jour": date, "ouverture", "haut", "bas", "cloture", "terminee": bool}.
Une barre non terminée (séance en cours) peut déclencher une entrée, une sortie ou une annulation,
mais ne fait avancer ni le compteur de séances ni traite_jusqu_au, ni remonter le stop :
la repasser ne rejoue rien.

Règles prudentes (activées par les paramètres, neutres dans le préréglage reference) :
- frais par ordre (fixe + pourcentage, en euros) et glissement sur les ventes au marché (stop, temps, décision) ;
- un ordre limite n'est exécuté que si le cours passe nettement sous la limite (marge_limite_atr),
  ou si la séance ouvre déjà dessous ;
- le jour de l'achat, une barre journalière ne dit pas si le plus haut précède l'achat : ni objectif ni remontée
  du stop ce jour-là, alors que le stop reste possible (hypothèse défavorable) ;
- un ordre est annulé sur un gap d'ouverture trop profond ou une clôture sous la MM50 du jour du signal.
"""
from datetime import date

from court_terme.calendrier import seances_entre
from court_terme.etats import ANNULE, CLOTURE, EXECUTE, EXPIRE, noter, passer

# Champs exprimés en dollars par action, divisés par le ratio d'un split
CHAMPS_PRIX = ("entree", "stop", "objectif", "atr", "prix_entree", "stop_initial", "objectif_initial", "mm50",
               "cloture", "plus_haut", "plus_bas")


def _evenement(type_, element, jour, prix, **autres):
    """Événement à annoncer ; son id (titre, type, jour) permet de repérer un doublon avant l'envoi."""
    ident = f"{element.get('id', element['ticker'])}:{type_}:{jour.isoformat()}"
    return {"id": ident, "type": type_, "ticker": element["ticker"], "nom": element.get("nom"),
            "jour": jour.isoformat(), "prix": prix, **autres}


def frais(montant_eur, p):
    """Frais d'un ordre fictif, en euros."""
    return p.get("frais_fixes_eur", 0) + montant_eur * p.get("frais_pct", 0) / 100 if montant_eur else 0.0


def _au_marche(prix, p):
    """Prix d'une vente au marché, glissement compris."""
    return prix * (1 - p.get("glissement_pct", 0) / 100)


def _extremes(pos, haut, bas):
    """Plus haut et plus bas atteints depuis l'achat (mouvements favorable et défavorable maximaux)."""
    pos["plus_haut"] = max(pos.get("plus_haut", pos["prix_entree"]), haut)
    pos["plus_bas"] = min(pos.get("plus_bas", pos["prix_entree"]), bas)


def _en_r(ecart, pos):
    unitaire = pos["prix_entree"] - pos.get("stop_initial", pos["stop"])
    return round(ecart / unitaire, 2) if unitaire > 0 else None


def _fermer(etat, pos, jour, prix_vise, motif, taux_eurusd, p, au_marche=False):
    """Vend la position ; une vente au marché perd le glissement. Le trade fermé garde ses mesures détaillées."""
    prix = _au_marche(prix_vise, p) if au_marche else prix_vise
    brut = (prix - pos["prix_entree"]) * pos["quantite"] / taux_eurusd
    cout = pos.get("frais_entree_eur", 0) + frais(prix * pos["quantite"] / taux_eurusd, p)
    resultat = round(brut - cout, 2)
    etat["positions"].remove(pos)
    passer(pos, CLOTURE, jour.isoformat(), motif=motif, prix=round(prix, 4))
    risque = pos.get("risque_initial_eur", 0)
    etat["fermees"].append({
        **pos, "date_sortie": jour.isoformat(), "prix_sortie": prix, "motif": motif,
        "resultat_brut_eur": round(brut, 2), "frais_eur": round(cout, 2), "resultat_eur": resultat,
        "glissement_eur": round((prix_vise - prix) * pos["quantite"] / taux_eurusd, 2),
        "resultat_r": round(resultat / risque, 2) if risque > 0 else None,
        "duree_seances": seances_entre(date.fromisoformat(pos["date_entree"]), jour) + 1,
        "mfe_r": _en_r(pos.get("plus_haut", pos["prix_entree"]) - pos["prix_entree"], pos),
        "mae_r": _en_r(pos.get("plus_bas", pos["prix_entree"]) - pos["prix_entree"], pos),
    })
    return _evenement(motif, pos, jour, prix, resultat_eur=resultat)


def _jour_entree_prudent(pos, b, p):
    return p.get("jour_entree_prudent") and b["jour"].isoformat() == pos["date_entree"]


def _sortie(pos, b, p):
    """(motif, prix visé, vente au marché) si la barre fait sortir la position, sinon None.

    Le stop l'emporte sur l'objectif. Met aussi à jour les extrêmes atteints, sans compter ce qui suit la vente.
    """
    if b["jour"].isoformat() != pos["date_entree"]:
        # Le jour d'entrée, l'ouverture est déjà passée : pas de sortie sur écart d'ouverture
        if b["ouverture"] <= pos["stop"]:
            _extremes(pos, b["ouverture"], b["ouverture"])
            return "stop", b["ouverture"], True
        if b["ouverture"] >= pos["objectif"]:
            _extremes(pos, b["ouverture"], b["ouverture"])
            return "objectif", b["ouverture"], False
    prudent = _jour_entree_prudent(pos, b, p)
    if b["bas"] <= pos["stop"]:
        # Un stop remonté au prix d'entrée ne peut pas vendre au-dessus de l'achat
        prix = min(pos["stop"], pos["prix_entree"])
        _extremes(pos, pos["prix_entree"], prix)
        return "stop", prix, True
    if b["haut"] >= pos["objectif"] and not prudent:
        _extremes(pos, pos["objectif"], b["bas"])
        return "objectif", pos["objectif"], False
    # Le jour de l'achat, le plus haut a pu venir avant l'achat : il ne compte pas comme mouvement favorable
    _extremes(pos, pos["prix_entree"] if prudent else b["haut"], b["bas"])
    return None


def _traiter_position(etat, pos, b, taux_eurusd, p):
    if pos.get("sortie_demandee") and b["jour"].isoformat() > pos["sortie_demandee"]:
        # Vente décidée par Claude après la clôture : exécutée à l'ouverture de la séance suivante
        _extremes(pos, b["ouverture"], b["ouverture"])
        return [_fermer(etat, pos, b["jour"], b["ouverture"], "decision", taux_eurusd, p, au_marche=True)], True
    sortie = _sortie(pos, b, p)
    if sortie:
        motif, prix, au_marche = sortie
        return [_fermer(etat, pos, b["jour"], prix, motif, taux_eurusd, p, au_marche)], True
    evts = []
    # Sur une barre partielle, le plus bas peut précéder la hausse : le stop ne remonte qu'en fin de séance
    if b["terminee"] and not _jour_entree_prudent(pos, b, p) \
            and b["haut"] >= pos["prix_entree"] + p["remontee_stop_atr"] * pos["atr"] and pos["stop"] < pos["prix_entree"]:
        pos["stop"] = pos["prix_entree"]
        noter(pos, "stop_remonte", b["jour"].isoformat(), stop=pos["stop"])
        evts.append(_evenement("stop_remonte", pos, b["jour"], pos["stop"]))
    if b["terminee"]:
        pos["seances"] += 1
        pos["traite_jusqu_au"] = b["jour"].isoformat()
        if pos["seances"] >= p["stop_temporel_seances"]:
            evts.append(_fermer(etat, pos, b["jour"], b["cloture"], "temps", taux_eurusd, p, au_marche=True))
            return evts, True
    return evts, False


def _execute(ordre, b, p):
    """Vrai si l'ordre limite d'achat est servi sur la barre."""
    if b["ouverture"] <= ordre["entree"]:
        return True
    marge = p.get("marge_limite_atr")
    if marge is None:
        return b["bas"] <= ordre["entree"]
    return b["bas"] < ordre["entree"] - marge * ordre["atr"]


def clore_ordre(etat, ordre, nouvel_etat, jour, **detail):
    """Retire l'ordre des ordres en cours ; il reste consultable, avec son historique, dans ordres_clos."""
    etat["ordres"].remove(ordre)
    passer(ordre, nouvel_etat, jour.isoformat(), **detail)
    etat.setdefault("ordres_clos", []).append(ordre)


def _annuler(etat, ordre, jour, motif):
    clore_ordre(etat, ordre, ANNULE, jour, motif=motif)
    return _evenement("annule", ordre, jour, ordre["entree"], motif=motif)


def _traiter_ordre(etat, ordre, barres, taux_eurusd, p):
    evts = []
    for i, b in enumerate(barres):
        gap = p.get("annulation_gap_atr")
        if gap is not None and b["ouverture"] < ordre["entree"] - gap * ordre["atr"]:
            return evts + [_annuler(etat, ordre, b["jour"], "ouverture trop loin sous la limite (mauvaise nouvelle ?)")]
        if _execute(ordre, b, p):
            prix = min(b["ouverture"], ordre["entree"])
            etat["ordres"].remove(ordre)
            passer(ordre, EXECUTE, b["jour"].isoformat(), prix=prix)
            montant = prix * ordre["quantite"] / taux_eurusd
            pos = {**ordre, "prix_entree": prix, "date_entree": b["jour"].isoformat(), "stop_initial": ordre["stop"],
                   "objectif_initial": ordre["objectif"], "seances": 0, "taux_entree": taux_eurusd,
                   "frais_entree_eur": round(frais(montant, p), 2),
                   "risque_initial_eur": round((prix - ordre["stop"]) * ordre["quantite"] / taux_eurusd, 2)}
            etat["positions"].append(pos)
            evts.append(_evenement("entree", pos, b["jour"], prix))
            return evts + _parcourir_position(etat, pos, barres[i:], taux_eurusd, p)
        if b["terminee"]:
            if p.get("annulation_sous_mm50") and ordre.get("mm50") and b["cloture"] < ordre["mm50"]:
                return evts + [_annuler(etat, ordre, b["jour"], "clôture sous la moyenne mobile 50 jours")]
            ordre["seances"] += 1
            ordre["traite_jusqu_au"] = b["jour"].isoformat()
            if ordre["seances"] >= p["validite_ordre_seances"]:
                clore_ordre(etat, ordre, EXPIRE, b["jour"])
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


def ajuster_splits(etat, splits):
    """Ramène ordres et positions à l'échelle des cours après un split ; splits = {ticker: [(date, ratio)]}.

    Yahoo divise tout l'historique par le ratio, alors que l'état garde les prix d'avant : sans cet ajustement,
    un split 10 pour 1 ferait croire à une chute de 90 % (fausse vente au stop). Un split daté du jour du signal
    ou avant est déjà dans les prix du signal.
    """
    evts = []
    for e in etat["ordres"] + etat["positions"]:
        for jour, ratio in sorted(splits.get(e["ticker"], [])):
            faits = e.get("splits", [])
            if ratio <= 0 or ratio == 1 or jour.isoformat() <= e["date_signal"] or jour.isoformat() in faits:
                continue
            for cle in CHAMPS_PRIX:
                if e.get(cle) is not None:
                    e[cle] = round(e[cle] / ratio, 6)
            e["quantite"] = round(e["quantite"] * ratio, 6)
            e["splits"] = [*faits, jour.isoformat()]
            noter(e, "split", jour.isoformat(), ratio=ratio)
            evts.append(_evenement("split", e, jour, None, ratio=ratio))
    return evts


def annuler_avant_resultats(etat, jour, resultats, p):
    """Annule les ordres dont les résultats trimestriels tombent avant la fin possible du trade.

    resultats = {ticker: {"date": "AAAA-MM-JJ"}} ; une date inconnue n'annule rien (le filtre du signal l'a déjà
    exclue si exclure_resultats_inconnus est actif).
    """
    if not p.get("annulation_resultats"):
        return []
    evts = []
    for ordre in list(etat["ordres"]):
        texte = (resultats.get(ordre["ticker"]) or {}).get("date")
        try:
            quand = date.fromisoformat(str(texte)[:10]) if texte else None
        except ValueError:
            quand = None
        horizon = p["validite_ordre_seances"] - ordre["seances"] + p["stop_temporel_seances"]
        if quand and quand >= jour and seances_entre(jour, quand) <= horizon:
            evts.append(_annuler(etat, ordre, jour, f"résultats trimestriels le {quand.isoformat()}"))
    return evts
