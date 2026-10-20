"""Textes Telegram du portefeuille fictif."""
LIBELLES = {"A": "repli en tendance", "B": "cassure"}


def message_signal(ordre, taux_eurusd):
    risque = (ordre["entree"] - ordre["stop"]) * ordre["quantite"] / taux_eurusd
    return "\n".join([
        f"🟢 SIGNAL — {ordre['ticker']} ({LIBELLES.get(ordre['configuration'], ordre['configuration'])})",
        f"Entrée {ordre['entree']:.2f} $ · Objectif {ordre['objectif']:.2f} $ · Stop {ordre['stop']:.2f} $",
        f"Quantité {ordre['quantite']} · Risque {risque:.2f} € · Valable 3 séances",
        f"Claude : ✅ validé — {ordre['justification']}",
    ])


def message_evenement(evt):
    t, prix, r = evt["ticker"], f"{evt['prix']:.2f} $", evt.get("resultat_eur")
    resultat = f" · {r:+.2f} €" if r is not None else ""
    return {
        "entree": f"📥 ENTRÉE — {t} achetée à {prix}",
        "expire": f"⌛ ORDRE EXPIRÉ — {t} : limite {prix} non atteinte en 3 séances",
        "stop": f"🔴 STOP — {t} vendue à {prix}{resultat}",
        "objectif": f"🎯 OBJECTIF — {t} vendue à {prix}{resultat}",
        "temps": f"⏱️ STOP TEMPOREL — {t} vendue à {prix} (15e séance){resultat}",
        "stop_remonte": f"🛡️ STOP REMONTÉ — {t} : stop au prix d'entrée {prix}",
    }[evt["type"]]
