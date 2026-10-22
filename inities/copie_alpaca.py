"""Copie sur le compte Alpaca **paper** : taille, ordres d'entrée, recalage du stop, sorties et rapprochement.

Le code seul décide : Claude ne fixe ni prix ni quantité, ne touche à aucun stop et ne décide d'aucune vente.
L'état (`copie/positions.json`) ne contient que des identifiants et des dates ; les messages ne citent que le ticker.
Les fonctions modifient l'état qu'on leur passe, au fil des ordres, puis le renvoient : si une exception les
interrompt, `copier` enregistre quand même ce qui a déjà été fait. Au moindre écart avec le compte, ou quand
Alpaca refuse un ordre, `copie/suspension.json` est écrit : plus aucun ordre n'est passé tant que l'utilisateur ne
l'a pas supprimé (le rapprochement continue).
Les messages sont ajoutés à une liste partagée au fil des opérations : si une exception interrompt le passage,
`copier` la joint à l'exception (attribut `messages`) pour que les sorties déjà faites soient quand même annoncées.
"""
import hashlib
import json
import math
import os
import time
from datetime import date
from pathlib import Path

import pandas as pd

from court_terme.alpaca import ErreurAlpaca
from inities.dirigeants import groupes
from inities.messages import achat_copie, vente as texte_vente
from inities.simulation import quantite, trier_par_priorite

DOSSIER = Path(__file__).resolve().parent.parent / "copie"
SUSPENSION = DOSSIER / "suspension.json"
POSITIONS = DOSSIER / "positions.json"

VALIDITE = "gtc"  # validité de l'ordre oto : un seul endroit à changer si Alpaca la refuse
STOP_PCT_DEFAUT = 15
LONGUEUR_ID_CLIENT_MAX = 128  # limite d'Alpaca
_ECHECS = ("canceled", "expired", "rejected")
ESSAIS_ANNULATION = 10  # relectures du stop après son annulation, avant de renoncer à vendre
PAUSE_ANNULATION_S = 0.5
_RAISONS = {"vente": "vente publiée par la personne copiée", "6 mois": "6 mois de détention atteints",
            "stop": "stop exécuté"}


def charger_etat():
    try:
        etat = json.loads(POSITIONS.read_text())
    except FileNotFoundError:
        etat = {}
    return {"positions": list(etat.get("positions", [])), "historique": list(etat.get("historique", []))}


def ecrire_json(chemin, donnees):
    """Écriture atomique : fichier temporaire puis os.replace (un fichier d'état n'est jamais à moitié écrit)."""
    chemin.parent.mkdir(parents=True, exist_ok=True)
    temporaire = chemin.with_name(chemin.name + ".tmp")
    try:
        temporaire.write_text(json.dumps(donnees, indent=1, ensure_ascii=False) + "\n")
        os.replace(temporaire, chemin)
    finally:
        temporaire.unlink(missing_ok=True)


def enregistrer_etat(etat):
    ecrire_json(POSITIONS, etat)


def suspendu():
    return SUSPENSION.exists()


def _suspendre(motif, jour):
    """Écrit la suspension et renvoie le message d'alerte ; rien de plus si elle existe déjà (une alerte suffit)."""
    if suspendu():
        return []
    ecrire_json(SUSPENSION, {"depuis": jour.isoformat(), "motif": motif})
    return [f"Copie suspendue : {motif}. Plus aucun ordre tant que copie/suspension.json n'est pas supprimé."]


def _jambe_stop(ordre):
    """Jambe stop de l'ordre : la jambe en vigueur (ni remplacée ni annulée), sinon la dernière jambe stop."""
    jambes = [jambe for jambe in ordre.get("legs") or [] if jambe.get("type") == "stop"]
    en_vigueur = [jambe for jambe in jambes if jambe.get("status") not in ("replaced", "canceled")]
    if en_vigueur:
        return en_vigueur[0]
    return jambes[-1] if jambes else None


def _id_client(source_id, prefixe="copie-"):
    """Identifiant d'ordre lisible, ou condensé (SHA-256) s'il dépasse la limite d'Alpaca."""
    texte = prefixe + source_id.replace(":", "-")
    if len(texte) <= LONGUEUR_ID_CLIENT_MAX:
        return texte
    return prefixe + hashlib.sha256(source_id.encode()).hexdigest()[:40]


def _valeur_compte(client):
    compte = client.compte()
    try:
        valeur = float(compte.get("equity") or compte.get("portfolio_value"))
    except (TypeError, ValueError):
        raise ErreurAlpaca("valeur du compte illisible") from None
    return valeur


def _prix(valeur):
    """Prix lisible et positif, sinon None (ordre pas encore exécuté, champ absent ou invalide)."""
    try:
        prix = float(valeur)
    except (TypeError, ValueError):
        return None
    return prix if math.isfinite(prix) and prix > 0 else None


def _sortie(position, jour, raison, prix_entree, prix_sortie, **champs):
    """Ligne d'historique d'une position fermée, avec de quoi annoncer le résultat."""
    return {"ticker": position["ticker"], "personne_id": position["personne_id"], "source_id": position["source_id"],
            "date_entree": position["date_entree"], "date_sortie": jour.isoformat(), "raison": raison,
            "prix_entree": prix_entree, "prix_sortie": prix_sortie, "quantite": position.get("quantite"), **champs}


def _annonce(ligne, complement=""):
    return texte_vente(ligne["ticker"], _RAISONS[ligne["raison"]] + complement, ligne.get("prix_entree"),
                       ligne.get("prix_sortie"), ligne.get("quantite"))


def _virgule(nombre):
    return f"{nombre:.2f}".replace(".", ",")


def rapprocher(etat, client, p=None, aujourd_hui=None, messages=None):
    """Compare l'état au compte : stop exécuté (sortie), recalage unique du stop à (100 - stop_pct) % du prix payé,
    vente confirmée, écart (suspension). Le stop est lu directement par son identifiant (`id_stop`, tenu à jour
    après chaque recalage ou stop reposé) ; les jambes de l'ordre d'achat ne servent qu'à le trouver la première
    fois. Renvoie (état, messages)."""
    jour = aujourd_hui or date.today()
    coefficient = 1 - ((p or {}).get("stop_pct", STOP_PCT_DEFAUT)) / 100
    messages = [] if messages is None else messages
    ecarts = []
    chez_alpaca = {pos["symbol"] for pos in client.positions()}

    en_vente = set()  # ventes demandées et pas encore exécutées : la position peut encore apparaître chez Alpaca
    for ligne in etat["historique"]:
        if ligne.get("id_vente") and not ligne.get("vente_confirmee"):
            ordre_vente = client.ordre(ligne["id_vente"])
            statut = ordre_vente.get("status")
            if statut == "filled":
                ligne["vente_confirmee"] = True
                ligne["prix_sortie"] = _prix(ordre_vente.get("filled_avg_price"))
                messages.append(_annonce(ligne))
            elif statut in _ECHECS:
                ecarts.append(f"la vente de {ligne['ticker']} n'a pas été exécutée ({statut})")
            else:
                en_vente.add(ligne["ticker"])

    for position in list(etat["positions"]):
        ticker = position["ticker"]
        ordre = client.ordre(position["id_ordre"])
        if position.get("id_stop"):
            jambe = client.ordre(position["id_stop"])
        else:  # pas d'identifiant de stop connu : on le prend une fois dans les jambes de l'ordre d'achat
            jambe = _jambe_stop(ordre)
            position["id_stop"] = (jambe or {}).get("id") or None
        if jambe is None:
            ecarts.append(f"{ticker} : stop introuvable sur l'ordre d'achat")
        elif jambe.get("status") == "filled":
            ligne = _sortie(position, jour, "stop", _prix(ordre.get("filled_avg_price")),
                            _prix(jambe.get("filled_avg_price")))
            etat["historique"].append(ligne)
            etat["positions"].remove(position)
            messages.append(_annonce(ligne))
        else:
            statut = ordre.get("status")
            if statut in _ECHECS:
                ecarts.append(f"{ticker} : l'ordre d'achat est {statut}")
            elif statut == "filled":
                if jambe.get("status") in _ECHECS + ("replaced",):
                    ecarts.append(f"{ticker} : le stop est {jambe['status']}, la position n'est plus protégée")
                elif ticker not in chez_alpaca:
                    ecarts.append(f"{ticker} : position absente du compte sans stop exécuté")
                elif not position["stop_recale"]:
                    stop = float(ordre["filled_avg_price"]) * coefficient
                    try:
                        reponse = client.modifier_stop(position["id_stop"], stop)
                    except ErreurAlpaca as erreur:
                        ecarts.append(f"{ticker} : stop non recalé ({str(erreur)[:200]})")
                    else:
                        position["id_stop"] = (reponse or {}).get("id") or position["id_stop"]
                        position["stop"], position["stop_recale"] = stop, True
                        messages.append(f"Achat de {ticker} exécuté : stop recalé à {_virgule(stop)} $.")

    connus = {pos["ticker"] for pos in etat["positions"]} | en_vente
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


def _attendre_annulation(id_stop, client, pause):
    """Relit le stop jusqu'à ce qu'il ne soit plus en cours d'annulation (au plus ESSAIS_ANNULATION lectures, une
    pause entre deux). Renvoie le dernier état lu : `canceled` (on peut vendre), `filled` (le stop s'est exécuté) ou
    autre chose (annulation jamais confirmée : on ne vend pas)."""
    for essai in range(ESSAIS_ANNULATION):
        stop = client.ordre(id_stop)
        if stop.get("status") in ("canceled", "filled"):
            return stop
        if essai < ESSAIS_ANNULATION - 1:
            pause(PAUSE_ANNULATION_S)
    return stop


def sorties(etat, ventes, aujourd_hui, client, pause=time.sleep, messages=None):
    """Ferme les positions dont la personne copiée a vendu le titre, ou arrivées à sortie_prevue : le stop est
    annulé, on attend qu'Alpaca le confirme `canceled`, puis la position est vendue au marché. Si le stop s'exécute
    entre-temps, c'est une sortie par stop, sans vente. S'il n'est jamais confirmé annulé, on ne vend pas et on
    suspend. Si l'achat n'est pas encore exécuté, on annule l'ordre parent (et sa jambe) ; on ne vend que ce qui a
    déjà été acheté. Si la vente est refusée alors que le stop est annulé, on tente une fois de reposer le stop,
    puis on suspend. Le résultat d'une vente (marché fermé le matin) est annoncé quand `rapprocher` la voit exécutée.
    Renvoie (état, messages)."""
    messages = [] if messages is None else messages
    for position in list(etat["positions"]):
        raison = _raison_sortie(position, ventes, aujourd_hui)
        if raison is None:
            continue
        ticker, stop_annule, parent_annule, execute, incertain = position["ticker"], False, False, True, False
        stop_execute, ordre_achat = None, {}
        try:
            ordre_achat = client.ordre(position["id_ordre"])
            execute = ordre_achat.get("status") == "filled"
            if execute:
                if position.get("id_stop"):
                    client.annuler(position["id_stop"])
                    stop = _attendre_annulation(position["id_stop"], client, pause)
                    if stop.get("status") == "filled":
                        stop_execute = stop
                    elif stop.get("status") == "canceled":
                        stop_annule = True
                    else:
                        incertain = True
                        raise ErreurAlpaca(f"le stop n'est pas confirmé annulé (statut {stop.get('status')})")
            else:
                client.annuler(position["id_ordre"])
                parent_annule = True
            ordre = None
            if stop_execute is None and (execute or ticker in {pos["symbol"] for pos in client.positions()}):
                ordre = client.fermer_position(ticker)
        except ErreurAlpaca as erreur:
            if stop_annule:
                suite = (" ; son stop a été annulé puis reposé" if _reposer_stop(position, client, aujourd_hui)
                         else " ; son stop a été annulé et n'a pas pu être reposé (non reposé), la position n'est "
                              "plus protégée")
            elif incertain:
                suite = " ; rien n'a été vendu, à vérifier chez Alpaca"
            elif parent_annule:
                suite = (" ; l'achat a été annulé, mais les actions déjà achetées éventuelles n'ont plus de stop "
                         "(non reposé)")
            else:
                suite = ""
            messages += _suspendre(f"sortie de {ticker} impossible ({str(erreur)[:200]}){suite}", aujourd_hui)
            break
        etat["positions"].remove(position)
        prix_entree = _prix(ordre_achat.get("filled_avg_price")) if execute else None
        if stop_execute is not None:
            ligne = _sortie(position, aujourd_hui, "stop", prix_entree, _prix(stop_execute.get("filled_avg_price")))
            etat["historique"].append(ligne)
            messages.append(_annonce(ligne))
            continue
        ligne = _sortie(position, aujourd_hui, raison, prix_entree, None, id_vente=(ordre or {}).get("id"),
                        vente_confirmee=ordre is None)
        etat["historique"].append(ligne)
        messages.append(_annonce(ligne, "" if ordre else ", achat annulé avant exécution"))
    return etat, messages


def _reposer_stop(position, client, jour):
    """Une seule tentative de reposer le stop annulé, au même prix et sur la même quantité. Vrai si c'est fait.
    L'identifiant client porte la date du jour : un second reposage un autre jour ne le réutilise pas."""
    if not (position.get("stop") and position.get("quantite")):
        return False
    try:
        reponse = client.passer_stop(position["ticker"], position["quantite"], position["stop"],
                                     _id_client(position["source_id"], prefixe=f"copie-restop-{jour:%Y%m%d}-copie-"), VALIDITE)
    except ErreurAlpaca:
        return False
    position["id_stop"] = (reponse or {}).get("id") or position["id_stop"]
    return True


def _passer_oto(client, op, nombre, stop, id_client):
    """Passe l'ordre. Si Alpaca refuse l'identifiant client parce qu'il existe déjà (ordre passé lors d'un
    passage dont l'état n'a pas été enregistré), reprend l'ordre existant, à condition qu'il porte sur le même
    titre. Tout autre refus, ou un ordre introuvable ou d'un autre titre, relève l'ErreurAlpaca d'origine."""
    try:
        return client.passer_oto(op.ticker, nombre, stop, id_client, VALIDITE)
    except ErreurAlpaca as erreur:
        texte = str(erreur).lower()
        if "client_order_id" not in texte or "unique" not in texte:
            raise
        try:
            existant = client.ordre_par_id_client(id_client)
        except ErreurAlpaca:
            raise erreur from None
        if existant.get("symbol") != op.ticker:
            raise erreur from None
        return existant


def entrees(etat, achats, derniers_cours, client, p, aujourd_hui=None, messages=None):
    """Passe un ordre `oto` (achat au marché + stop à stop_pct sous le dernier cours) pour chaque achat copiable,
    dans l'ordre de priorité de la simulation. Renvoie (état, messages, achats seulement signalés avec leur
    motif). Un ErreurAlpaca sur un ordre suspend la copie : les achats restants sont signalés, pas passés."""
    jour = aujourd_hui or date.today()
    messages = [] if messages is None else messages
    signales = []
    deja = {pos["source_id"] for pos in etat["positions"] + etat["historique"]}
    nouveaux = [op for op in achats if op.sens == "achat" and op.source_id not in deja]
    if not nouveaux:
        return etat, messages, signales
    valeur = _valeur_compte(client)
    detenus = {pos["ticker"] for pos in etat["positions"]}
    sortis = {ligne["ticker"] for ligne in etat["historique"] if ligne.get("date_sortie") == jour.isoformat()}
    suspendue = False
    for op in trier_par_priorite(nouveaux, groupes(achats, p)):
        cours = derniers_cours.get(op.ticker)
        if suspendue:
            signales.append((op, "copie suspendue"))
        elif op.ticker in detenus:
            signales.append((op, "titre déjà détenu"))
        elif op.ticker in sortis:
            signales.append((op, "titre sorti aujourd'hui"))
        elif len(etat["positions"]) >= p["positions_max"]:
            signales.append((op, f"plafond de {p['positions_max']} positions atteint"))
        elif not isinstance(cours, (int, float)) or not math.isfinite(cours) or cours <= 0:
            signales.append((op, "pas de cours récent pour dimensionner l'ordre"))
        elif not (nombre := quantite(valeur, cours, p)):
            signales.append((op, f"une action coûte plus de {p['part_max_une_action_pct']} % du compte"))
        else:
            stop = cours * (1 - p["stop_pct"] / 100)
            try:
                ordre = _passer_oto(client, op, nombre, stop, _id_client(op.source_id))
            except ErreurAlpaca as erreur:
                suspendue = True
                signales.append((op, "ordre refusé par Alpaca"))
                messages += _suspendre(f"ordre d'achat de {op.ticker} refusé ({str(erreur)[:200]})", jour)
                continue
            jambe = _jambe_stop(ordre)
            etat["positions"].append({
                "ticker": op.ticker, "personne_id": op.personne_id, "source_id": op.source_id,
                "id_ordre": ordre.get("id"), "id_stop": jambe.get("id") if jambe else None,
                "date_entree": jour.isoformat(), "stop": stop, "quantite": nombre,
                "sortie_prevue": (pd.Timestamp(jour) + pd.DateOffset(months=p["duree_max_mois"])).date().isoformat(),
                "stop_recale": False})
            detenus.add(op.ticker)
            messages.append(achat_copie(op.ticker, nombre, cours, stop))
    return etat, messages, signales


def copier(achats, ventes, derniers_cours, client, p, aujourd_hui):
    """Passage du matin : rapprochement, sorties, puis entrées. Dès qu'une suspension existe, plus rien d'autre n'est
    fait. L'état est enregistré quoi qu'il arrive, ordres déjà passés compris. Une exception autre qu'`ErreurAlpaca`
    suspend la copie (le type d'erreur seul est noté), puis est relevée ; les deux portent les messages déjà produits
    dans leur attribut `messages`. Renvoie (messages, achats seulement
    signalés avec leur motif)."""
    etat = charger_etat()
    messages, signales = [], []
    try:
        etat, _ = rapprocher(etat, client, p, aujourd_hui, messages)
        if not suspendu():
            etat, _ = sorties(etat, ventes, aujourd_hui, client, messages=messages)
        if not suspendu():
            etat, _, signales = entrees(etat, achats, derniers_cours, client, p, aujourd_hui, messages)
    except ErreurAlpaca as erreur:
        erreur.messages = messages  # sorties et stops déjà traités : à annoncer quand même
        raise
    except Exception as erreur:
        _suspendre(f"erreur inattendue ({type(erreur).__name__}) pendant la copie", aujourd_hui)
        erreur.messages = messages
        raise
    finally:
        enregistrer_etat(etat)
    return messages, signales
