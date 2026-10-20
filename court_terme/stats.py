"""Statistiques des positions fermées et bilan hebdomadaire du portefeuille fictif."""
from court_terme.portefeuille import capital_courant


def _moyenne(valeurs):
    return round(sum(valeurs) / len(valeurs), 2) if valeurs else None


def statistiques(fermees):
    resultats = [f["resultat_eur"] for f in fermees]
    gains = [r for r in resultats if r > 0]
    pertes = [r for r in resultats if r <= 0]
    return {
        "nombre": len(resultats),
        "taux_reussite": round(len(gains) / len(resultats) * 100, 1) if resultats else None,
        "gain_moyen": _moyenne(gains),
        "perte_moyenne": _moyenne(pertes),
        "esperance": _moyenne(resultats),
        "resultat_total": round(sum(resultats), 2),
    }


def _euros(valeur):
    return "—" if valeur is None else f"{valeur:+.2f} €"


def bilan_hebdo(etat, perf_indice_semaine, perf_indice_depuis_debut, taux_eurusd, cours):
    """Texte du bilan du vendredi ; cours = dernier prix de chaque position ouverte."""
    latent = sum((cours.get(p["ticker"], p["prix_entree"]) - p["prix_entree"]) * p["quantite"]
                 for p in etat["positions"]) / taux_eurusd
    valeur = capital_courant(etat) + latent
    perf = (valeur / etat["capital_initial"] - 1) * 100
    s = statistiques(etat["fermees"])
    taux = "—" if s["taux_reussite"] is None else f"{s['taux_reussite']:g} %"
    return "\n".join([
        "📊 BILAN HEBDO — portefeuille fictif",
        f"Valeur {valeur:.2f} € ({perf:+.2f} % depuis le {etat.get('debut') or 'début'})",
        f"QQQ sur la même période : {perf_indice_depuis_debut:+.2f} % · cette semaine : {perf_indice_semaine:+.2f} %",
        f"Positions fermées : {s['nombre']} · réussite {taux}",
        f"Gain moyen {_euros(s['gain_moyen'])} · perte moyenne {_euros(s['perte_moyenne'])}",
        f"Espérance par position {_euros(s['esperance'])} · total réalisé {_euros(s['resultat_total'])}",
        f"En cours : {len(etat['positions'])} positions, {len(etat['ordres'])} ordres en attente",
    ])
