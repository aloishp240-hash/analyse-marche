"""Statistiques des positions fermées et bilan hebdomadaire du portefeuille fictif."""
from court_terme.portefeuille import capital_courant


def _moyenne(valeurs):
    return round(sum(valeurs) / len(valeurs), 2) if valeurs else None


def statistiques(fermees):
    resultats = [f["resultat_eur"] for f in fermees]
    gains = [r for r in resultats if r > 0]
    pertes = [r for r in resultats if r < 0]  # un résultat nul n'est ni un gain ni une perte
    return {
        "nombre": len(resultats),
        "taux_reussite": round(len(gains) / len(resultats) * 100, 1) if resultats else None,
        "gain_moyen": _moyenne(gains),
        "perte_moyenne": _moyenne(pertes),
        "esperance": _moyenne(resultats),
        "resultat_total": round(sum(resultats), 2),
    }


ECHANTILLON_MIN = 30  # en dessous, les statistiques d'un backtest tiennent surtout du hasard


def _baisse_max_pct(valeurs):
    """Plus forte baisse, en %, depuis un plus haut précédent."""
    plus_haut, pire = None, 0.0
    for v in valeurs:
        plus_haut = v if plus_haut is None else max(plus_haut, v)
        pire = min(pire, (v / plus_haut - 1) * 100)
    return round(pire, 2)


def _annualise_pct(depart, arrivee, seances):
    return round(((arrivee / depart) ** (252 / seances) - 1) * 100, 2) if seances and depart > 0 < arrivee else None


def mesures(fermees, courbe, capital_initial):
    """Mesures d'un backtest : brut et net de frais, R, facteur de profit, baisse maximale, rendement annualisé.

    R = résultat net / risque prévu au départ (1 R = perte prévue si le stop est touché).
    courbe = [{"jour", "valeur", "qqq"}] : valeur quotidienne du portefeuille et de QQQ avec le même capital.
    """
    nets = [f["resultat_eur"] for f in fermees]
    gains, pertes = sum(r for r in nets if r > 0), -sum(r for r in nets if r < 0)
    r_multiples = [f["resultat_eur"] / f["risque_initial_eur"] for f in fermees if f.get("risque_initial_eur", 0) > 0]
    valeurs, qqq = [c["valeur"] for c in courbe], [c["qqq"] for c in courbe]
    return {
        **statistiques(fermees),
        "nuls": sum(1 for r in nets if r == 0),
        "brut_total": round(sum(f.get("resultat_brut_eur", f["resultat_eur"]) for f in fermees), 2),
        "frais_total": round(sum(f.get("frais_eur", 0) for f in fermees), 2),
        "r_moyen": round(sum(r_multiples) / len(r_multiples), 2) if r_multiples else None,
        "facteur_profit": round(gains / pertes, 2) if pertes else None,
        "baisse_max_pct": _baisse_max_pct(valeurs) if valeurs else None,
        "baisse_max_qqq_pct": _baisse_max_pct(qqq) if qqq else None,
        "annualise_pct": _annualise_pct(capital_initial, valeurs[-1], len(valeurs)) if valeurs else None,
        "annualise_qqq_pct": _annualise_pct(capital_initial, qqq[-1], len(qqq)) if qqq else None,
        "echantillon_faible": len(nets) < ECHANTILLON_MIN,
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
