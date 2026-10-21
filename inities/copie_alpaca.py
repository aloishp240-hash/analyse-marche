"""Copie sur le compte Alpaca **paper** : taille, ordres d'entrée, recalage du stop, sorties et rapprochement.

Le code seul décide : Claude ne fixe ni prix ni quantité, ne touche à aucun stop et ne décide d'aucune vente.
L'état (`copie/positions.json`) ne contient que des identifiants et des dates ; les messages ne citent que le ticker.
Au moindre écart avec le compte, ou quand Alpaca refuse un ordre, `copie/suspension.json` est écrit : plus aucun
ordre n'est passé tant que l'utilisateur ne l'a pas supprimé (le rapprochement continue).
"""
import copy
import json
import math
from datetime import date
from pathlib import Path

import pandas as pd

from court_terme.alpaca import ErreurAlpaca
from inities.dirigeants import groupes
from inities.simulation import quantite, trier_par_priorite

DOSSIER = Path(__file__).resolve().parent.parent / "copie"
SUSPENSION = DOSSIER / "suspension.json"
POSITIONS = DOSSIER / "positions.json"

VALIDITE = "gtc"  # validité de l'ordre oto : un seul endroit à changer si Alpaca la refuse
STOP_PCT_DEFAUT = 15
LONGUEUR_ID_CLIENT = 48
_ECHECS = ("canceled", "expired", "rejected")


def charger_etat():
    try:
        etat = json.loads(POSITIONS.read_text())
    except FileNotFoundError:
        etat = {}
    return {"positions": list(etat.get("positions", [])), "historique": list(etat.get("historique", []))}


def enregistrer_etat(etat):
    POSITIONS.parent.mkdir(parents=True, exist_ok=True)
    POSITIONS.write_text(json.dumps(etat, indent=1, ensure_ascii=False) + "\n")


def suspendu():
    return SUSPENSION.exists()


def _suspendre(motif, jour):
    """Écrit la suspension et renvoie le message d'alerte ; rien de plus si elle existe déjà (une alerte suffit)."""
    if suspendu():
        return []
    SUSPENSION.parent.mkdir(parents=True, exist_ok=True)
    SUSPENSION.write_text(json.dumps({"depuis": jour.isoformat(), "motif": motif}, ensure_ascii=False) + "\n")
    return [f"Copie suspendue : {motif}. Plus aucun ordre tant que copie/suspension.json n'est pas supprimé."]


def _jambe_stop(ordre):
    return next((jambe for jambe in ordre.get("legs") or [] if jambe.get("type") == "stop"), None)


def _id_client(source_id):
    return ("copie-" + source_id.replace(":", "-"))[:LONGUEUR_ID_CLIENT]


def _valeur_compte(client):
    compte = client.compte()
    try:
        valeur = float(compte.get("equity") or compte.get("portfolio_value"))
    except (TypeError, ValueError):
        raise ErreurAlpaca("valeur du compte illisible") from None
    return valeur


def _virgule(nombre):
    return f"{nombre:.2f}".replace(".", ",")


def rapprocher(etat, client, p=None, aujourd_hui=None):
    """Compare l'état au compte : stop exécuté (sortie), recalage unique du stop à (100 - stop_pct) % du prix payé,
    écart (suspension). Renvoie (nouvel état, messages)."""
    etat, jour = copy.deepcopy(etat), aujourd_hui or date.today()
    coefficient = 1 - ((p or {}).get("stop_pct", STOP_PCT_DEFAUT)) / 100
    messages, ecarts, restantes = [], [], []
    chez_alpaca = {pos["symbol"] for pos in client.positions()}

    en_vente = set()  # ventes demandées et pas encore exécutées : la position peut encore apparaître chez Alpaca
    for ligne in etat["historique"]:
        if ligne.get("id_vente") and not ligne.get("vente_confirmee"):
            statut = client.ordre(ligne["id_vente"]).get("status")
            if statut == "filled":
                ligne["vente_confirmee"] = True
            elif statut in _ECHECS:
                ecarts.append(f"la vente de {ligne['ticker']} n'a pas été exécutée ({statut})")
            else:
                en_vente.add(ligne["ticker"])

    for position in etat["positions"]:
        ticker = position["ticker"]
        ordre = client.ordre(position["id_ordre"])
        jambe = _jambe_stop(ordre)
        if jambe is None:
            ecarts.append(f"{ticker} : stop introuvable sur l'ordre d'achat")
        elif jambe.get("status") == "filled":
            etat["historique"].append({
                "ticker": ticker, "personne_id": position["personne_id"], "source_id": position["source_id"],
                "date_entree": position["date_entree"], "date_sortie": jour.isoformat(), "raison": "stop"})
            messages.append(f"Stop exécuté sur {ticker} : position fermée.")
            continue
        else:
            position["id_stop"] = jambe.get("id") or position["id_stop"]
            statut = ordre.get("status")
            if statut in _ECHECS:
                ecarts.append(f"{ticker} : l'ordre d'achat est {statut}")
            elif statut == "filled":
                if jambe.get("status") in _ECHECS:
                    ecarts.append(f"{ticker} : le stop est {jambe['status']}, la position n'est plus protégée")
                elif ticker not in chez_alpaca:
                    ecarts.append(f"{ticker} : position absente du compte sans stop exécuté")
                elif not position["stop_recale"]:
                    stop = float(ordre["filled_avg_price"]) * coefficient
                    client.modifier_stop(position["id_stop"], stop)
                    position["stop_recale"] = True
                    messages.append(f"Achat de {ticker} exécuté : stop recalé à {_virgule(stop)} $.")
        restantes.append(position)
    etat["positions"] = restantes

    connus = {pos["ticker"] for pos in restantes} | en_vente
    for ticker in sorted(chez_alpaca - connus):
        ecarts.append(f"{ticker} : position du compte absente de l'état")
    if ecarts:
        messages += _suspendre("écart avec le compte (" + " ; ".join(ecarts) + ")", jour)
    return etat, messages


def _raison_sortie(position, ventes, aujourd_hui):
    entree = date.fromisoformat(position["date_entree"])
    if any(v.sens == "vente" and v.personne_id == position["personne_id"] and v.ticker == position["ticker"]
           and v.date_publication >= entree for v in ventes):
        return "vente"
    if date.fromisoformat(position["sortie_prevue"]) <= aujourd_hui:
        return "6 mois"
    return None


def sorties(etat, ventes, aujourd_hui, client):
    """Ferme les positions dont la personne copiée a vendu le titre, ou arrivées à sortie_prevue : le stop est
    annulé, puis la position est vendue au marché. Renvoie (nouvel état, messages)."""
    etat = copy.deepcopy(etat)
    messages = []
    for position in list(etat["positions"]):
        raison = _raison_sortie(position, ventes, aujourd_hui)
        if raison is None:
            continue
        ticker, stop_annule = position["ticker"], False
        try:
            if position.get("id_stop"):
                client.annuler(position["id_stop"])
                stop_annule = True
            ordre = client.fermer_position(ticker)
        except ErreurAlpaca as erreur:
            suite = " ; son stop est annulé, la position n'est plus protégée" if stop_annule else ""
            messages += _suspendre(f"sortie de {ticker} impossible ({erreur}){suite}", aujourd_hui)
            break
        etat["positions"].remove(position)
        etat["historique"].append({
            "ticker": ticker, "personne_id": position["personne_id"], "source_id": position["source_id"],
            "date_entree": position["date_entree"], "date_sortie": aujourd_hui.isoformat(), "raison": raison,
            "id_vente": ordre.get("id"), "vente_confirmee": False})
        messages.append(f"Sortie de {ticker} : " + ("vente publiée par la personne copiée."
                                                    if raison == "vente" else "6 mois de détention atteints."))
    return etat, messages


def entrees(etat, achats, derniers_cours, client, p, aujourd_hui=None):
    """Passe un ordre `oto` (achat au marché + stop à stop_pct sous le dernier cours) pour chaque achat copiable,
    dans l'ordre de priorité de la simulation. Renvoie (nouvel état, messages, achats seulement signalés avec leur
    motif). Un ErreurAlpaca sur un ordre suspend la copie : les achats restants sont signalés, pas passés."""
    etat, jour = copy.deepcopy(etat), aujourd_hui or date.today()
    messages, signales = [], []
    deja = {pos["source_id"] for pos in etat["positions"] + etat["historique"]}
    nouveaux = [op for op in achats if op.sens == "achat" and op.source_id not in deja]
    if not nouveaux:
        return etat, messages, signales
    valeur = _valeur_compte(client)
    detenus = {pos["ticker"] for pos in etat["positions"]}
    suspendue = False
    for op in trier_par_priorite(nouveaux, groupes(achats, p)):
        cours = derniers_cours.get(op.ticker)
        if suspendue:
            signales.append((op, "copie suspendue"))
        elif op.ticker in detenus:
            signales.append((op, "titre déjà détenu"))
        elif len(etat["positions"]) >= p["positions_max"]:
            signales.append((op, f"plafond de {p['positions_max']} positions atteint"))
        elif not isinstance(cours, (int, float)) or not math.isfinite(cours) or cours <= 0:
            signales.append((op, "pas de cours récent pour dimensionner l'ordre"))
        elif not (nombre := quantite(valeur, cours, p)):
            signales.append((op, f"une action coûte plus de {p['part_max_une_action_pct']} % du compte"))
        else:
            stop = cours * (1 - p["stop_pct"] / 100)
            try:
                ordre = client.passer_oto(op.ticker, nombre, stop, _id_client(op.source_id), VALIDITE)
            except ErreurAlpaca as erreur:
                suspendue = True
                signales.append((op, "ordre refusé par Alpaca"))
                messages += _suspendre(f"ordre d'achat de {op.ticker} refusé ({str(erreur)[:200]})", jour)
                continue
            jambe = _jambe_stop(ordre)
            etat["positions"].append({
                "ticker": op.ticker, "personne_id": op.personne_id, "source_id": op.source_id,
                "id_ordre": ordre["id"], "id_stop": jambe.get("id") if jambe else None,
                "date_entree": jour.isoformat(),
                "sortie_prevue": (pd.Timestamp(jour) + pd.DateOffset(months=p["duree_max_mois"])).date().isoformat(),
                "stop_recale": False})
            detenus.add(op.ticker)
            messages.append(f"Ordre d'achat passé : {nombre} {op.ticker}, stop à {_virgule(stop)} $.")
    return etat, messages, signales


def copier(achats, ventes, derniers_cours, client, p, aujourd_hui):
    """Passage du matin : rapprochement, sorties, puis entrées. Dès qu'une suspension existe, plus rien d'autre n'est
    fait. Enregistre l'état. Renvoie (messages, achats seulement signalés avec leur motif)."""
    etat, messages = rapprocher(charger_etat(), client, p, aujourd_hui)
    signales = []
    if not suspendu():
        etat, plus = sorties(etat, ventes, aujourd_hui, client)
        messages += plus
    if not suspendu():
        etat, plus, signales = entrees(etat, achats, derniers_cours, client, p, aujourd_hui)
        messages += plus
    enregistrer_etat(etat)
    return messages, signales
