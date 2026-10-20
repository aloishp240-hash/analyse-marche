"""Textes Telegram du portefeuille fictif."""
import re

LIBELLES = {"A": "repli en tendance", "B": "cassure"}
# Rubriques de la justification d'un signal, chacune mise sur sa propre ligne
RUBRIQUES = re.compile(r"\s*\b(Technique|Actualités|Contexte|Risque)\s*:\s*")


def titre(element):
    """« SpaceX (SPCX) » ; le ticker seul pour un ordre enregistré avant l'ajout des noms."""
    nom = element.get("nom")
    return f"{nom} ({element['ticker']})" if nom and nom != element["ticker"] else element["ticker"]


def detailler(justification):
    return RUBRIQUES.sub(lambda m: f"\n• {m.group(1)} : ", justification).strip()


def message_signal(ordre, taux_eurusd):
    risque = (ordre["entree"] - ordre["stop"]) * ordre["quantite"] / taux_eurusd
    return "\n".join([
        f"🟢 SIGNAL BUY — {titre(ordre)} ({LIBELLES.get(ordre['configuration'], ordre['configuration'])})",
        f"Entrée {ordre['entree']:.2f} $ · Objectif {ordre['objectif']:.2f} $ · Stop {ordre['stop']:.2f} $",
        f"Quantité {ordre['quantite']:g} · Risque {risque:.2f} € · Valable 3 séances",
        "",
        "Pourquoi Claude achète :",
        detailler(ordre["justification"]),
    ])


def message_evenement(evt):
    t, prix, r = titre(evt), f"{evt['prix']:.2f} $", evt.get("resultat_eur")
    resultat = f" · {r:+.2f} €" if r is not None else ""
    return {
        "entree": f"📥 ENTRÉE — {t} achetée à {prix}",
        "expire": f"⌛ ORDRE EXPIRÉ — {t} : limite {prix} non atteinte en 3 séances",
        "stop": f"🔴 STOP — {t} vendue à {prix}{resultat}",
        "objectif": f"🎯 OBJECTIF — {t} vendue à {prix}{resultat}",
        "temps": f"⏱️ STOP TEMPOREL — {t} vendue à {prix} (15e séance){resultat}",
        "stop_remonte": f"🛡️ STOP REMONTÉ — {t} : stop au prix d'entrée {prix}",
    }[evt["type"]]
