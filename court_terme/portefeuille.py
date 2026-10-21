"""Portefeuille fictif : état JSON, capital courant, taille des positions et plafonds."""
import json
import math
import os
import tempfile


def charger(chemin, p):
    if chemin.exists():
        return json.loads(chemin.read_text())
    return {"capital_initial": p["capital_initial"], "debut": None, "ordres": [], "positions": [], "fermees": []}


def sauver(etat, chemin):
    """Écriture atomique : un état à moitié écrit ne doit jamais être commité."""
    chemin.parent.mkdir(parents=True, exist_ok=True)
    fd, temporaire = tempfile.mkstemp(dir=chemin.parent, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(etat, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(temporaire, chemin)


def capital_courant(etat):
    """Capital de départ + gains et pertes réalisés, en euros."""
    return etat["capital_initial"] + sum(f["resultat_eur"] for f in etat["fermees"])


def quantite(entree, stop, capital_eur, taux_eurusd, p):
    """Actions, fractions comprises (arrondi inférieur à decimales_quantite) : risque_pct du capital jusqu'au stop,
    sans dépasser plafond_position_pct du capital."""
    risque_usd = capital_eur * p["risque_pct"] / 100 * taux_eurusd
    plafond_usd = capital_eur * p["plafond_position_pct"] / 100 * taux_eurusd
    echelle = 10 ** p["decimales_quantite"]
    brute = min(risque_usd / (entree - stop), plafond_usd / entree)
    return max(0, round(math.floor(brute * echelle + 1e-9) / echelle, p["decimales_quantite"]))


def _engage_eur(suivis, taux_eurusd):
    return sum(e["quantite"] * e.get("prix_entree", e["entree"]) for e in suivis) / taux_eurusd


def verifier_ajout(etat, ticker, industrie, quantite, entree, taux_eurusd, p):
    """Motif de refus, ou None si l'ordre peut être ajouté."""
    if quantite <= 0:
        return "quantité nulle pour le plafond par position"
    if any(e["ticker"] == ticker for e in etat["ordres"] + etat["positions"]):
        return f"{ticker} déjà en ordre ou en position"
    # Une position dont Claude a décidé la vente libère sa place et son capital : elle part à l'ouverture
    suivis = etat["ordres"] + [e for e in etat["positions"] if not e.get("sortie_demandee")]
    if len(suivis) >= p["positions_max"]:
        return f"déjà {p['positions_max']} ordres ou positions"
    if industrie is not None and sum(e.get("industrie") == industrie for e in suivis) >= p["par_industrie_max"]:
        return f"déjà {p['par_industrie_max']} titres dans l'industrie {industrie}"
    if _engage_eur(suivis, taux_eurusd) + quantite * entree / taux_eurusd > capital_courant(etat):
        return "montant engagé supérieur au capital"
    return None


def ajouter_ordre(etat, candidat, industrie, justification, jour, taux_eurusd, p):
    """Ajoute un ordre d'entrée calculé par le code ; lève ValueError(motif) en cas de refus."""
    nombre = quantite(candidat["entree"], candidat["stop"], capital_courant(etat), taux_eurusd, p)
    motif = verifier_ajout(etat, candidat["ticker"], industrie, nombre, candidat["entree"], taux_eurusd, p)
    if motif:
        raise ValueError(motif)
    ordre = {**candidat, "industrie": industrie, "quantite": nombre, "date_signal": jour.isoformat(),
             "justification": justification, "traite_jusqu_au": jour.isoformat(), "seances": 0}
    etat["ordres"].append(ordre)
    etat["debut"] = etat.get("debut") or jour.isoformat()
    return ordre
