"""Nouveautés du matin : nouvelles déclarations de la Chambre, du Sénat, des Form 4 et des 13F.

Elles donnent les achats à signaler et les ventes des personnes copiées (celles qui ont une position).
`copie/vus.json` garde ce qui a déjà été traité, pour ne jamais renvoyer deux fois la même chose :

    {"dernier_passage": "AAAA-MM-JJ", "vus": {"chambre:<DocID>": "AAAA-MM-JJ", "senat:<uuid>": ..., "form4:<accession>": ...,
     "13f:<accession>": ...}, "achats_dirigeants": [{"ticker", "date", "personne_id"}, ...]}

La date d'un identifiant vu est le jour de l'index pour un Form 4 et le jour du passage pour le reste. Elle sert à purger :
un Form 4 dont le jour est au plus `dernier_passage` n'est plus jamais relu, les autres identifiants s'oublient après
45 jours (la Chambre et le Sénat sont relus sur 14 jours). L'ancien format (liste d'identifiants) est encore lu.
`dernier_passage` est le dernier jour dont l'index Form 4 a été lu : il n'avance que si aucune source n'a échoué. Si le budget
de temps des Form 4 est dépassé, il s'arrête au dernier jour lu en entier : les dépôts déjà lus du jour interrompu sont gardés
(et marqués vus), les autres jours sont relus au passage suivant.
Une source en erreur produit une note et ne marque rien comme vu : le passage suivant recommence. Le texte lu en ligne est
une donnée : on n'en extrait que des champs précis, jamais d'instruction.
"""
import json
import os
import time
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests
from pandas.tseries.holiday import USFederalHolidayCalendar

from commun import DONNEES
from inities import chambre, cours, dirigeants, fonds, senat
from inities.chambre import ErreurChambre
from inities.copie_alpaca import DOSSIER, charger_etat
from inities.dirigeants import ErreurDirigeants, achat_retenu, groupes
from inities.fonds import ErreurFonds
from inities.modele import Operation, nettoyer_ticker
from inities.sec import ErreurSec
from inities.senat import AccordNonAccepte, ErreurSenat

VUS = DOSSIER / "vus.json"
DOSSIER_CACHE = DONNEES / "cache_copie"
URL_ARCHIVES = "https://www.sec.gov/Archives/"
VUS_JOURS = 45  # un identifiant autre qu'un Form 4 s'oublie après ce nombre de jours
RECUL_ELUS_JOURS = 14  # la Chambre et le Sénat sont relus sur ce nombre de jours avant le dernier passage
JOURS_MAX = 7
FORM4_DUREE_MAX_S = 900  # budget de temps pour télécharger les dépôts Form 4 (le job GitHub est limité à 40 minutes)
ECHECS_DE_SUITE_MAX = 5  # la SEC ne répond plus : on arrête de lire les dépôts au lieu d'attendre chaque délai
JOURS_COURS = 10  # recul pour trouver le dernier cours
_ERREURS_CONNUES = (ErreurSec, ErreurChambre, ErreurSenat, ErreurFonds, ErreurDirigeants, AccordNonAccepte)


def _raison(erreur):
    return str(erreur) if isinstance(erreur, _ERREURS_CONNUES) else type(erreur).__name__


class ErreurVus(Exception):
    """vus.json existe mais n'est pas lisible : on ne repart pas de zéro en silence (tout serait renvoyé)."""


def _vus_vide():
    return {"dernier_passage": None, "vus": {}, "achats_dirigeants": []}


def charger_vus():
    try:
        texte = VUS.read_text()
    except FileNotFoundError:
        return _vus_vide()
    try:
        vus = json.loads(texte)
    except ValueError as erreur:
        raise ErreurVus(f"{VUS.name} illisible (JSON invalide) : à corriger ou à supprimer à la main") from erreur
    if not isinstance(vus, dict):
        raise ErreurVus(f"{VUS.name} illisible (objet JSON attendu) : à corriger ou à supprimer à la main")
    return _copie_vus(vus)


def enregistrer_vus(vus):
    """Écriture atomique : fichier temporaire puis os.replace, jamais un vus.json à moitié écrit."""
    VUS.parent.mkdir(parents=True, exist_ok=True)
    temporaire = VUS.with_name(VUS.name + ".tmp")
    try:
        temporaire.write_text(json.dumps(vus, indent=1, ensure_ascii=False) + "\n")
        os.replace(temporaire, VUS)
    finally:
        temporaire.unlink(missing_ok=True)


def _copie_vus(vus, aujourd_hui=None):
    """Copie au format dict ; l'ancien format liste est daté d'aujourd'hui."""
    brut = vus.get("vus", {})
    if isinstance(brut, dict):
        jetons = {str(jeton): str(jour) for jeton, jour in brut.items()}
    else:
        jour = (aujourd_hui or date.today()).isoformat()
        jetons = {str(jeton): jour for jeton in brut}
    return {"dernier_passage": vus.get("dernier_passage"), "vus": jetons,
            "achats_dirigeants": [dict(a) for a in vus.get("achats_dirigeants", [])]}


def _purger(jetons, dernier_passage, aujourd_hui):
    """Retire les Form 4 dont le jour est au plus `dernier_passage` (ils ne seront plus relus) et les autres
    identifiants vus il y a plus de VUS_JOURS jours."""
    limite = (aujourd_hui - timedelta(days=VUS_JOURS)).isoformat()

    def a_garder(jeton, jour):
        if jeton.startswith("form4:"):
            return dernier_passage is None or jour > dernier_passage
        return jour >= limite

    return {jeton: jour for jeton, jour in jetons.items() if a_garder(jeton, jour)}


def _jeton(source_id):
    """Dépôt d'où vient une opération : « chambre:<DocID> », « senat:<uuid> », « form4:<accession> » ou
    « 13f:<accession> » (source_id sans le rang ni le CUSIP)."""
    return ":".join(source_id.split(":")[:2])


def est_doublon(op, vus):
    return _jeton(op.source_id) in vus.get("vus", ())


def _veille_ouvree(jour):
    jour -= timedelta(days=1)
    while jour.weekday() >= 5:
        jour -= timedelta(days=1)
    return jour


def _jours_ouvres(dernier_passage, aujourd_hui):
    jours, jour = [], dernier_passage + timedelta(days=1)
    while jour < aujourd_hui:
        if jour.weekday() < 5:
            jours.append(jour)
        jour += timedelta(days=1)
    return jours


def jours_a_lire(dernier_passage, aujourd_hui):
    """Jours ouvrés de ]dernier_passage, aujourd'hui[, les JOURS_MAX plus récents ; sans dernier_passage, la veille
    ouvrée seulement."""
    if dernier_passage is None:
        return [_veille_ouvree(aujourd_hui)]
    return _jours_ouvres(dernier_passage, aujourd_hui)[-JOURS_MAX:]


def _ferie_americain(jour):
    """Jour férié fédéral américain : EDGAR est fermé, il n'y a pas d'index ce jour-là."""
    return len(USFederalHolidayCalendar().holidays(start=pd.Timestamp(jour), end=pd.Timestamp(jour))) > 0


class _Lecture:
    """Ce que les sources ont donné : opérations par dépôt (jeton), notes et indicateur d'échec."""

    def __init__(self):
        self.lots, self.notes, self.echec = {}, [], False
        self.jours = {}  # jour de l'index de chaque dépôt Form 4 (date de son identifiant dans vus)
        self.jours_lus = None  # budget Form 4 dépassé : nombre de jours (depuis le plus ancien) lus en entier

    def echoue(self, source, message):
        self.notes.append(f"{source} : {message}")
        self.echec = True

    def erreur(self, source, erreur):
        self.echoue(source, _raison(erreur))


# --- sources ---

def _lire_chambre(lecture, personnes, deja, depuis, aujourd_hui, session, dossier):
    """`depuis` : premier jour de publication relu (le vus empêche les doublons)."""
    session = session or requests.Session()
    entrees = []
    for annee in range(depuis.year, aujourd_hui.year + 1):
        entrees.extend(chambre._index_annuel(session, annee))
    for entree in entrees:
        personne_id = f"politicien:chambre:{entree['nom']}|{entree['prenom']}|{entree['etat']}"
        jeton = f"chambre:{entree['doc_id']}"
        if personne_id not in personnes or entree["date_publication"] < depuis or jeton in deja:
            continue
        try:
            pdf = chambre._pdf(session, entree, dossier)
            operations = chambre.lire_declaration(pdf, entree) if pdf is not None else None
        except Exception as erreur:  # une mauvaise déclaration ne doit pas arrêter les autres
            lecture.erreur(f"Chambre, déclaration {entree['doc_id']}", erreur)
            continue
        if pdf is None:
            lecture.notes.append(f"Chambre : PDF de la déclaration {entree['doc_id']} introuvable (404), sera relu")
        elif operations is None:
            lecture.notes.append(f"Chambre : déclaration {entree['doc_id']} illisible (pas de texte)")
            lecture.lots[jeton] = []
        else:
            lecture.lots[jeton] = operations


def _lire_senat(lecture, personnes, deja, depuis, aujourd_hui, config, session, dossier):
    session = senat.ouvrir_session(config, session)
    for entree in senat.rechercher(session, depuis, aujourd_hui):
        personne_id = f"politicien:senat:{entree['nom']}|{entree['prenom']}"
        jeton = f"senat:{entree['uuid']}"
        if personne_id not in personnes or entree["date_publication"] < depuis or jeton in deja:
            continue
        if entree["papier"]:
            lecture.notes.append(f"Sénat : déclaration {entree['uuid']} sur papier, non lue")
            lecture.lots[jeton] = []
            continue
        try:
            page, _ = senat._page(session, entree, dossier)
            operations = senat.lire_declaration(page, entree) if page is not None else None
        except Exception as erreur:
            lecture.erreur(f"Sénat, déclaration {entree['uuid']}", erreur)
            continue
        if page is None:
            lecture.notes.append(f"Sénat : page de la déclaration {entree['uuid']} introuvable (404), sera relue")
        elif operations is None:
            lecture.notes.append(f"Sénat : déclaration {entree['uuid']} illisible (tableau absent)")
            lecture.lots[jeton] = []
        else:
            lecture.lots[jeton] = operations


def _lire_form4(lecture, jours, deja, client, horloge=time.monotonic):
    echecs_de_suite = 0
    debut = horloge()

    def budget_depasse(lus):
        if horloge() - debut < FORM4_DUREE_MAX_S:
            return False
        lecture.jours_lus = lus
        lecture.notes.append(f"Form 4 : budget de temps de {FORM4_DUREE_MAX_S} s dépassé, lecture arrêtée ; "
                             f"{len(jours) - lus} jour(s) restent à lire au prochain passage")
        return True

    for numero, jour in enumerate(jours):
        if budget_depasse(numero):
            return
        trimestre = (jour.month - 1) // 3 + 1
        url = dirigeants.URL_INDEX_JOUR.format(annee=jour.year, trimestre=trimestre, jour=jour)
        try:
            index = client.texte(url, absent_si_403=True)
        except ErreurSec as erreur:
            lecture.erreur(f"Form 4, index du {jour.isoformat()}", erreur)
            continue
        if index is None:
            # EDGAR n'est fermé que les jours fériés fédéraux : ailleurs, une 404 (ou 403, la SEC répond 403 pour un fichier d'Archives absent) est une panne ou un index pas encore publié
            if not _ferie_americain(jour):
                lecture.echoue(f"Form 4, index du {jour.isoformat()}", "absent (404 ou 403) un jour ouvré, sera relu")
            continue
        introuvables = 0
        for chemin in dirigeants.lire_index_jour(index):
            accession = chemin.rsplit("/", 1)[-1].removesuffix(".txt")
            jeton = f"form4:{accession}"
            if jeton in deja or jeton in lecture.lots:
                continue
            if budget_depasse(numero):
                if introuvables:
                    lecture.notes.append(f"Form 4 : {introuvables} dépôt(s) du {jour.isoformat()} introuvable(s)")
                return
            try:
                depot = client.texte(URL_ARCHIVES + chemin, absent_si_403=True)
            except ErreurSec as erreur:
                lecture.erreur(f"Form 4, dépôt {accession}", erreur)
                depot, echec = None, True
            else:
                echec = depot is None and not _ferie_americain(jour)
                if echec:
                    lecture.echoue(f"Form 4, dépôt {accession}", "introuvable (404 ou 403) un jour ouvré, sera relu")
            if echec:
                echecs_de_suite += 1
                if echecs_de_suite >= ECHECS_DE_SUITE_MAX:
                    lecture.notes.append("Form 4 : lecture arrêtée après plusieurs échecs de suite")
                    return
                continue
            echecs_de_suite = 0
            if depot is None:  # jour férié : rien à lire
                introuvables += 1
                continue
            xml = dirigeants.extraire_xml(depot)
            lecture.lots[jeton] = dirigeants.lire_form4(xml, accession, jour) if xml else []
            lecture.jours[jeton] = jour.isoformat()
        if introuvables:
            lecture.notes.append(f"Form 4 : {introuvables} dépôt(s) du {jour.isoformat()} introuvable(s)")


def _lire_fonds(lecture, personnes, deja, depuis, client, p, dossier):
    en_echec = {}

    def signaler(cik, nom, raison):
        if f"fonds:{cik}" in personnes:  # un fonds non suivi et sans position ne bloque rien
            en_echec.setdefault(f"fonds:{cik}", []).append(f"13F, {nom} : {raison}")

    operations, sans_ticker = fonds.telecharger(client, depuis, dossier, p, signaler=signaler)
    for notes in en_echec.values():
        lecture.notes.extend(notes)
        lecture.echec = True
    for op in operations:
        jeton = _jeton(op.source_id)
        if op.personne_id in personnes and op.personne_id not in en_echec and jeton not in deja:
            lecture.lots.setdefault(jeton, []).append(op)
    if sans_ticker:
        lecture.notes.append(f"13F : {sans_ticker} nom(s) de société sans ticker sûr, ignoré(s)")


# --- univers ---

def _capitalisation(nombre, df):
    """Nombre d'actions × dernier cours, ou None si l'une des deux données manque ou est inutilisable."""
    if nombre is None or df is None or df.empty:
        return None
    fermetures = df["Close"].dropna()
    if fermetures.empty:
        return None
    valeur = float(nombre) * float(fermetures.iloc[-1])
    return valeur if valeur == valeur and valeur > 0 else None


def _univers(tickers, aujourd_hui, dossier, p):
    """(tickers dans l'univers, tickers que Yahoo limite ou ne sert plus, tickers sans cours ou sans nombre d'actions,
    tickers hors univers). Univers simplifié : capitalisation (nombre d'actions actuel × dernier cours) d'au moins
    capitalisation_min.

    Seul un titre dont la capitalisation a été calculée et reste sous le seuil est « hors univers » (marqué vu). Un titre
    sans cours ou sans nombre d'actions est à relire au passage suivant, comme tout le lot si l'appel lève ou ne donne
    rien du tout : c'est une panne de Yahoo."""
    tickers = sorted(tickers)
    if not tickers:
        return set(), set(), set(), set()
    limites = set()
    try:
        actions = cours.actions_en_circulation(tickers, dossier, limites=limites)
        if not actions:
            return set(), set(tickers), set(), set()
        prix = cours.charger(tickers, aujourd_hui - timedelta(days=JOURS_COURS), aujourd_hui, dossier, limites=limites)
    except Exception:  # panne ou limite de Yahoo : on ne conclut rien sur ces titres
        return set(), set(tickers), set(), set()
    if not prix:
        return set(), limites & set(tickers), set(tickers) - limites, set()
    retenus, indisponibles, hors = set(), set(), set()
    for ticker in tickers:
        if ticker in limites:
            continue
        valeur = _capitalisation(actions.get(ticker), prix.get(ticker))
        if valeur is None:
            indisponibles.add(ticker)
        elif valeur >= p["capitalisation_min"]:
            retenus.add(ticker)
        else:
            hors.add(ticker)
    return retenus, limites & set(tickers), indisponibles, hors


# --- collecte ---

def _nettoyer(lots):
    """Opérations avec un ticker lisible ; (lots nettoyés, nombre d'opérations ignorées)."""
    ignorees, propres = 0, {}
    for jeton, operations in lots.items():
        propres[jeton] = []
        for op in operations:
            ticker = nettoyer_ticker(op.ticker)
            if ticker is None:
                ignorees += 1
            else:
                propres[jeton].append(replace(op, ticker=ticker))
    return propres, ignorees


def _a_signaler(op, suivis_ids, p):
    """Achat d'une personne suivie (élu : d'au moins politicien_montant_min) ou achat de dirigeant retenu."""
    if op.sens != "achat":
        return False
    if op.famille == "dirigeant":
        return achat_retenu(op, p)
    if op.personne_id not in suivis_ids:
        return False
    return op.famille != "politicien" or op.montant_min >= p["politicien_montant_min"]


def _ordre(op):
    return op.date_publication, op.source_id


def _historique(achats_dirigeants, p):
    """Achats de dirigeants déjà signalés, remis en Operation pour réutiliser `groupes`."""
    return [Operation("dirigeant", a["personne_id"], a["personne_id"], a["ticker"], "achat",
                      date.fromisoformat(a["date"]), date.fromisoformat(a["date"]), p["dirigeant_montant_min"],
                      p["dirigeant_montant_min"], f"histo:{i}", {"code": "P", "dirigeant": True})
            for i, a in enumerate(achats_dirigeants)]


def collecter(suivis, vus, aujourd_hui, config, client_sec, *, etat=None, session_chambre=None, session_senat=None,
              dossier_cache=None, dossier_cours=None, horloge=None):
    """(achats à signaler, ventes des personnes copiées, notes du journal, nouveaux vus).

    `vus` n'est pas modifié. Les ventes sont celles des personnes qui ont une position, sur leurs titres. Un achat
    hors de l'univers, ou dont le ticker est illisible, est écarté et compté dans les notes. `horloge` (secondes) sert au
    budget de temps des Form 4 ; time.monotonic par défaut."""
    p = config["copie"]
    vus = _copie_vus(vus, aujourd_hui)
    etat = charger_etat() if etat is None else etat
    dossier_cache = Path(dossier_cache) if dossier_cache else DOSSIER_CACHE
    dossier_cours = Path(dossier_cours) if dossier_cours else dossier_cache / "cours"
    deja = set(vus["vus"])
    dernier = date.fromisoformat(vus["dernier_passage"]) if vus["dernier_passage"] else None
    jours = jours_a_lire(dernier, aujourd_hui)
    depuis = dernier or jours[0]
    depuis_elus = depuis - timedelta(days=RECUL_ELUS_JOURS)  # une déclaration peut apparaître plusieurs jours après

    suivis_ids = {x["personne_id"] for x in suivis.get("personnes", [])}
    positions = {(pos["personne_id"], pos["ticker"]) for pos in etat["positions"]}
    personnes = suivis_ids | {personne_id for personne_id, _ in positions}

    lecture = _Lecture()
    if dernier is not None and (sautes := len(_jours_ouvres(dernier, aujourd_hui)) - len(jours)):
        lecture.notes.append(f"Form 4 : {sautes} jour(s) ouvré(s) avant le {jours[0].isoformat()} ne seront pas lus "
                             f"(plafond de {JOURS_MAX} jours)")
    sources = []
    if any(i.startswith("politicien:chambre:") for i in personnes):
        sources.append(("Chambre", lambda: _lire_chambre(lecture, personnes, deja, depuis_elus, aujourd_hui,
                                                          session_chambre, dossier_cache / "chambre")))
    suivis_senat = sum(i.startswith("politicien:senat:") for i in personnes)
    if suivis_senat and config["copie"].get("senat_accepte") is True:
        sources.append(("Sénat", lambda: _lire_senat(lecture, personnes, deja, depuis_elus, aujourd_hui, config,
                                                      session_senat, dossier_cache / "senat")))
    elif suivis_senat:
        lecture.notes.append(f"Sénat : {suivis_senat} personne(s) suivie(s) mais accord du site non accepté "
                             "(senat_accepte), déclarations non lues")
    sources.append(("Form 4", lambda: _lire_form4(lecture, jours, deja, client_sec, horloge or time.monotonic)))
    if any(i.startswith("fonds:") for i in personnes):
        sources.append(("13F", lambda: _lire_fonds(lecture, personnes, deja, depuis, client_sec, p,
                                                    dossier_cache / "13f")))
    for nom, lire in sources:
        try:
            lire()
        except Exception as erreur:  # une source en erreur n'arrête pas les autres
            lecture.erreur(nom, erreur)

    lots, ignorees = _nettoyer(lecture.lots)
    notes = list(lecture.notes)
    if ignorees:
        notes.append(f"{ignorees} opération(s) ignorée(s) : ticker illisible")

    candidats = {op.ticker for ops in lots.values() for op in ops if _a_signaler(op, suivis_ids, p)}
    dans_univers, limites, indisponibles, hors = _univers(candidats, aujourd_hui, dossier_cours, p)
    a_relire = limites | indisponibles
    bloques = {jeton for jeton, ops in lots.items()
               if any(op.ticker in a_relire and _a_signaler(op, suivis_ids, p) for op in ops)}
    if limites:
        notes.append(f"Univers : Yahoo limite le débit pour {', '.join(sorted(limites))} ; dépôts relus au passage suivant")
    if indisponibles:
        notes.append(f"Univers : cours indisponibles ou nombre d'actions inconnu pour {', '.join(sorted(indisponibles))}, "
                     "sera relu au passage suivant")
    lecture.echec = lecture.echec or bool(a_relire)

    achats, ventes, hors_univers = [], [], 0
    for jeton, ops in lots.items():
        if jeton in bloques:
            continue
        for op in ops:
            if _a_signaler(op, suivis_ids, p):
                if op.ticker in dans_univers:
                    achats.append(op)
                else:
                    hors_univers += 1
            elif op.sens == "vente" and (op.personne_id, op.ticker) in positions:
                ventes.append(op)
    if hors_univers:
        notes.append(f"{hors_univers} achat(s) hors univers (capitalisation sous le seuil : "
                     f"{', '.join(sorted(hors))})")

    limite_groupe = aujourd_hui - timedelta(days=p["groupe_dirigeants_jours"])
    historique = [a for a in vus["achats_dirigeants"] if date.fromisoformat(a["date"]) >= limite_groupe]
    en_groupe = groupes(_historique(historique, p) + [op for op in achats if op.famille == "dirigeant"], p)
    achats = [replace(op, details={**op.details, "groupe": op.source_id in en_groupe}) if op.famille == "dirigeant"
              else op for op in achats]
    for op in achats:
        if op.famille == "dirigeant":
            ligne = {"ticker": op.ticker, "date": op.date_operation.isoformat(), "personne_id": op.personne_id}
            if ligne not in historique:
                historique.append(ligne)

    if lecture.echec or not jours:
        dernier_passage = vus["dernier_passage"]
    elif lecture.jours_lus is not None:  # budget dépassé : jusqu'au dernier jour lu en entier
        dernier_passage = jours[lecture.jours_lus - 1].isoformat() if lecture.jours_lus else vus["dernier_passage"]
    else:
        dernier_passage = jours[-1].isoformat()
    marques = {**{jeton: lecture.jours.get(jeton, aujourd_hui.isoformat()) for jeton in lots if jeton not in bloques},
               **vus["vus"]}  # un identifiant déjà vu garde sa date
    nouveaux = {
        "dernier_passage": dernier_passage,
        "vus": _purger(marques, dernier_passage, aujourd_hui),
        "achats_dirigeants": historique,
    }
    return sorted(achats, key=_ordre), sorted(ventes, key=_ordre), notes, nouveaux
