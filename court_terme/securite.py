"""Garde-fous du portefeuille fictif : en cas de doute technique, aucune nouvelle entrée.

- données du jour absentes, incomplètes ou incohérentes : pas de nouvel ordre ce jour-là ;
- suivi en retard (un passage de positions.py a échoué) : pas de nouvel ordre tant que le suivi n'a pas rattrapé ;
- baisse du capital réalisé au-delà d'un seuil, ou état incohérent : suspension des nouvelles entrées jusqu'à ce que
  l'utilisateur la lève (python suspension.py lever). Le suivi des ordres et positions continue pendant la suspension.
"""
import fcntl
import json
import math
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from commun import NY
from court_terme.etats import anomalies

AGENT = Path(__file__).resolve().parent.parent / "agent"
ANOMALIES = AGENT / "anomalies.json"
ANOMALIES_MAX = 300
COUVERTURE_MIN = 0.9  # part des titres de l'univers qui doivent avoir des cours du jour valides


def controle_donnees(hist, tickers, jour, indice):
    """(motifs qui bloquent toute entrée, {titre: motif} des titres aux données du jour inutilisables)."""
    if hist.empty:
        return ["historique vide"], {}
    bloquants, ecartes = [], {}
    derniere = hist.index[-1].date()
    if derniere != jour:
        bloquants.append(f"dernière séance disponible le {derniere}, pas le {jour}")
    ligne = hist[hist.index.date == jour]
    for ticker in [*tickers, indice]:
        try:
            haut, bas, cloture = (float(ligne[champ][ticker].iloc[-1]) for champ in ("High", "Low", "Close"))
        except (KeyError, IndexError):
            ecartes[ticker] = "cours du jour absent"
            continue
        if any(math.isnan(x) for x in (haut, bas, cloture)):
            ecartes[ticker] = "cours du jour incomplet"
        elif min(haut, bas, cloture) <= 0:
            ecartes[ticker] = "prix nul ou négatif"
        elif bas > haut:
            ecartes[ticker] = "plus bas au-dessus du plus haut"
        elif not bas * (1 - 1e-6) <= cloture <= haut * (1 + 1e-6):
            ecartes[ticker] = "clôture hors du plus haut et du plus bas"
    if indice in ecartes:
        bloquants.append(f"{indice} : {ecartes.pop(indice)}")
    if tickers and len(ecartes) > (1 - COUVERTURE_MIN) * len(tickers):
        bloquants.append(f"{len(ecartes)} titres sur {len(tickers)} sans cours du jour valides")
    return bloquants, ecartes


def suivi_en_retard(etat, jour):
    """Ordres et positions dont la séance `jour` n'a pas été traitée : le suivi a manqué un passage."""
    jour = str(jour)
    return [f"{e['ticker']} suivi jusqu'au {e['traite_jusqu_au']} seulement"
            for e in etat["ordres"] + etat["positions"]
            if e["traite_jusqu_au"] < jour and e["date_signal"] != jour]


def baisse_realisee_pct(etat):
    """Recul du capital réalisé depuis son plus haut (≤ 0), compté depuis la dernière reprise."""
    depart = (etat.get("reprise") or {}).get("nombre_fermees", 0)
    valeur = etat["capital_initial"] + sum(f["resultat_eur"] for f in etat["fermees"][:depart])
    sommet = valeur
    for f in etat["fermees"][depart:]:
        valeur += f["resultat_eur"]
        sommet = max(sommet, valeur)
    return round((valeur / sommet - 1) * 100, 2) if sommet > 0 else -100.0


def suspendre(etat, motif, jour):
    """Suspend les nouvelles entrées ; renvoie True si la suspension est nouvelle."""
    if etat.get("suspension"):
        return False
    etat["suspension"] = {"depuis": str(jour), "motif": motif}
    return True


def lever_suspension(etat, jour):
    """Lève la suspension ; le seuil de baisse repart du capital du jour."""
    etat.pop("suspension", None)
    etat["reprise"] = {"jour": str(jour), "nombre_fermees": len(etat["fermees"])}


def controler_baisse(etat, p, jour):
    """Suspend les entrées si le capital réalisé a reculé de suspension_baisse_pct depuis son plus haut."""
    seuil = p.get("suspension_baisse_pct")
    baisse = baisse_realisee_pct(etat)
    if seuil and baisse <= -seuil:
        return suspendre(etat, f"capital réalisé en recul de {-baisse:.1f} % depuis son plus haut "
                               f"(seuil {seuil:g} %)", jour)
    return False


def message_suspension(etat):
    return (f"⛔ NOUVELLES ENTRÉES SUSPENDUES — {etat['suspension']['motif']}\n"
            "Les ordres et positions en cours restent suivis. Pour reprendre après vérification : "
            "python suspension.py lever")


def noter_anomalie(source, texte, chemin=ANOMALIES):
    """Ajoute une anomalie au journal lu par le rapport d'audit (les plus anciennes partent d'abord)."""
    try:
        liste = json.loads(Path(chemin).read_text())
    except (OSError, ValueError):
        liste = []
    aujourd_hui = datetime.now(NY).date().isoformat()
    if any(a["source"] == source and a["texte"] == texte and a["quand"].startswith(aujourd_hui) for a in liste):
        return
    liste.append({"quand": datetime.now(NY).isoformat(timespec="minutes"), "source": source, "texte": texte})
    Path(chemin).parent.mkdir(parents=True, exist_ok=True)
    Path(chemin).write_text(json.dumps(liste[-ANOMALIES_MAX:], indent=1, ensure_ascii=False) + "\n")


@contextmanager
def verrou(chemin=AGENT / ".passage.verrou"):
    """Empêche deux passages de suivi ou d'agent de tourner en même temps sur la même machine.

    Sur GitHub Actions, le groupe de concurrence du workflow fait déjà passer les lancements l'un après l'autre.
    """
    Path(chemin).parent.mkdir(parents=True, exist_ok=True)
    with open(chemin, "w") as fichier:
        try:
            fcntl.flock(fichier, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Un autre passage est en cours : celui-ci s'arrête sans rien faire")
        try:
            yield
        finally:
            fcntl.flock(fichier, fcntl.LOCK_UN)


def filtrer_doublons(etat, evenements, garder=300):
    """(événements jamais annoncés, doublons) ; les identifiants annoncés sont gardés dans l'état.

    La simulation ne rejoue jamais une séance déjà traitée : un doublon signale un défaut, il n'est pas renvoyé.
    """
    deja = etat.setdefault("evenements_annonces", [])
    nouveaux, doublons = [], []
    for evt in evenements:
        if evt.get("id") and evt["id"] in deja:
            doublons.append(evt)
            continue
        nouveaux.append(evt)
        if evt.get("id"):
            deja.append(evt["id"])
    del deja[:-garder]
    return nouveaux, doublons


def controler_etat(etat, jour):
    """Suspend les entrées si l'état enregistré est incohérent ; renvoie (anomalies, suspension nouvelle)."""
    problemes = anomalies(etat, jour)
    nouvelle = suspendre(etat, "état du portefeuille incohérent : " + " ; ".join(problemes[:3]), jour) \
        if problemes else False
    return problemes, nouvelle
