"""Achats et ventes d'actions des dirigeants et administrateurs (Form 4 de la SEC).

Deux sources donnent les mêmes opérations : les jeux trimestriels Form 345 (TSV) et le XML de chaque dépôt, retrouvé
par l'index quotidien d'EDGAR. Le XML et les TSV sont des données : on n'en extrait que des champs précis, et un
dépôt ou une ligne mal formée est ignoré sans arrêter le reste.
"""
import csv
import io
import math
import re
import zipfile
from collections import defaultdict
from datetime import date, timedelta

import pandas
from lxml import etree

from inities.modele import Operation

URL_JEU = "https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/{annee}q{trimestre}_form345.zip"
# Depuis le deuxième trimestre 2026, la SEC publie les jeux dans un autre dossier.
URL_JEU_RECENT = ("https://www.sec.gov/files/datastandardsinnovation/data/insider-transactions-data-sets/"
                  "{annee}q{trimestre}_form345.zip")
URL_INDEX_JOUR = "https://www.sec.gov/Archives/edgar/daily-index/{annee}/QTR{trimestre}/form.{jour:%Y%m%d}.idx"

_SENS = {"P": "achat", "S": "vente"}
_MOIS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}
_DATE_TSV = re.compile(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})")
_BLOC_XML = re.compile(r"<ownershipDocument\b.*?</ownershipDocument>", re.DOTALL)
_CHEMIN = re.compile(r"edgar/data/\d+/([\d-]+)\.txt")
_COLONNES = {
    "SUBMISSION": ["ACCESSION_NUMBER", "FILING_DATE", "DOCUMENT_TYPE", "ISSUERNAME", "ISSUERTRADINGSYMBOL"],
    "REPORTINGOWNER": ["ACCESSION_NUMBER", "RPTOWNERCIK", "RPTOWNERNAME", "RPTOWNER_RELATIONSHIP", "RPTOWNER_TITLE"],
    "NONDERIV_TRANS": ["ACCESSION_NUMBER", "TRANS_DATE", "TRANS_CODE", "TRANS_SHARES", "TRANS_PRICEPERSHARE"],
}


class ErreurDirigeants(Exception):
    pass


def url_jeu(annee, trimestre):
    modele = URL_JEU_RECENT if (annee, trimestre) >= (2026, 2) else URL_JEU
    return modele.format(annee=annee, trimestre=trimestre)


def _chaine(valeur):
    return valeur.strip() if isinstance(valeur, str) else ""


def _cik(valeur):
    chiffres = _chaine(valeur)
    return str(int(chiffres)) if chiffres.isdigit() else None


def _symbole(valeur):
    symbole = _chaine(valeur).upper()
    return None if symbole in ("", "NONE") else symbole


def _nombre(valeur):
    try:
        nombre = float(_chaine(valeur))
    except ValueError:
        return None
    return nombre if math.isfinite(nombre) and nombre > 0 else None


def _poste(titre, officier, administrateur, dix_pour_cent):
    if titre:
        return titre
    if officier:
        return "Officer"
    if administrateur:
        return "Director"
    return "10% Owner" if dix_pour_cent else ""


def _operation(accession, rang, publication, jour, code, declarant, emetteur, actions, prix):
    """Opération d'un déclarant, ou None s'il manque le déclarant, le symbole, la date, les actions ou le prix."""
    actions, prix = _nombre(actions), _nombre(prix)
    cik, symbole = declarant["cik"], _symbole(emetteur["symbole"])
    if None in (jour, actions, prix, cik, symbole):
        return None
    montant = round(actions * prix, 2)
    return Operation(
        famille="dirigeant", personne_id=f"dirigeant:{cik}", personne=declarant["nom"], ticker=symbole,
        sens=_SENS[code], date_operation=jour, date_publication=publication, montant_min=montant,
        montant_max=montant, source_id=f"form4:{accession}:{rang}",
        details={"poste": declarant["poste"], "code": code, "dirigeant": declarant["dirigeant"],
                 "societe": emetteur["societe"]})


def _premier_dirigeant(declarants):
    return next((d for d in declarants if d["dirigeant"]), declarants[0] if declarants else None)


# --- XML d'un dépôt ---

def _texte(noeud, chemin):
    return (noeud.findtext(chemin) or "").strip()


def _vrai(valeur):
    return valeur.strip().lower() in ("1", "true")


def _jour_iso(valeur):
    try:
        return date.fromisoformat(valeur[:10])
    except ValueError:
        return None


def lire_form4(xml, accession, date_publication):
    """Achats (P) et ventes (S) non dérivés d'un Form 4, sans les transactions dont le prix manque.

    Quand plusieurs personnes déposent ensemble, c'est le premier dirigeant ou administrateur qui est retenu.
    """
    parseur = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, encoding="utf-8")
    try:
        racine = etree.fromstring(xml.encode("utf-8"), parseur)
    except (etree.XMLSyntaxError, ValueError):
        return []
    if racine.tag != "ownershipDocument":
        return []
    declarants = []
    for proprietaire in racine.findall("reportingOwner"):
        relation = proprietaire.find("reportingOwnerRelationship")
        if relation is None:
            relation = etree.Element("reportingOwnerRelationship")
        officier, administrateur = _vrai(_texte(relation, "isOfficer")), _vrai(_texte(relation, "isDirector"))
        declarants.append({
            "cik": _cik(_texte(proprietaire, "reportingOwnerId/rptOwnerCik")),
            "nom": _texte(proprietaire, "reportingOwnerId/rptOwnerName"),
            "dirigeant": officier or administrateur,
            "poste": _poste(_texte(relation, "officerTitle"), officier, administrateur,
                            _vrai(_texte(relation, "isTenPercentOwner"))),
        })
    declarant = _premier_dirigeant(declarants)
    if declarant is None:
        return []
    emetteur = {"symbole": _texte(racine, "issuer/issuerTradingSymbol"), "societe": _texte(racine, "issuer/issuerName")}
    operations = []
    for transaction in racine.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code = _texte(transaction, "transactionCoding/transactionCode")
        if code not in _SENS:
            continue
        operation = _operation(
            accession, len(operations) + 1, date_publication, _jour_iso(_texte(transaction, "transactionDate/value")),
            code, declarant, emetteur, _texte(transaction, "transactionAmounts/transactionShares/value"),
            _texte(transaction, "transactionAmounts/transactionPricePerShare/value"))
        if operation is not None:
            operations.append(operation)
    return operations


def extraire_xml(txt_complet):
    """Bloc <ownershipDocument> d'un dépôt complet (.txt d'EDGAR), ou None s'il n'y en a pas."""
    trouve = _BLOC_XML.search(txt_complet)
    return trouve.group(0) if trouve else None


# --- jeu trimestriel ---

def _jour_tsv(valeur):
    trouve = _DATE_TSV.fullmatch(_chaine(valeur))
    if trouve is None or trouve[2].upper() not in _MOIS:
        return None
    try:
        return date(int(trouve[3]), _MOIS[trouve[2].upper()], int(trouve[1]))
    except ValueError:
        return None


def _table(archive, nom):
    fichier = f"{nom}.tsv"
    if fichier not in archive.namelist():
        raise ErreurDirigeants(f"jeu trimestriel : {nom} absent")
    colonnes = _COLONNES[nom]
    try:
        with archive.open(fichier) as flux:
            table = pandas.read_csv(flux, sep="\t", dtype=str, usecols=lambda c: c in colonnes, quoting=csv.QUOTE_NONE,
                                    on_bad_lines="skip", encoding="utf-8", encoding_errors="replace")
    except (pandas.errors.ParserError, pandas.errors.EmptyDataError, ValueError) as erreur:
        raise ErreurDirigeants(f"jeu trimestriel : {nom} illisible ({erreur})") from erreur
    manquantes = [c for c in colonnes if c not in table.columns]
    if manquantes:
        raise ErreurDirigeants(f"jeu trimestriel : {nom} sans la colonne {manquantes[0]}")
    return table


def lire_jeu(zip_bytes):
    """Mêmes opérations que lire_form4, à partir des TSV SUBMISSION, REPORTINGOWNER et NONDERIV_TRANS d'un jeu."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as erreur:
        raise ErreurDirigeants("jeu trimestriel : zip invalide") from erreur
    depots = _table(archive, "SUBMISSION")
    depots = depots[depots.DOCUMENT_TYPE.str.strip() == "4"]
    emetteurs = {ligne.ACCESSION_NUMBER: {"publication": _jour_tsv(ligne.FILING_DATE),
                                          "symbole": ligne.ISSUERTRADINGSYMBOL, "societe": _chaine(ligne.ISSUERNAME)}
                 for ligne in depots.itertuples()}
    declarants = defaultdict(list)
    for ligne in _table(archive, "REPORTINGOWNER").itertuples():
        relations = {r.strip() for r in _chaine(ligne.RPTOWNER_RELATIONSHIP).split(",")}
        officier, administrateur = "Officer" in relations, "Director" in relations
        declarants[ligne.ACCESSION_NUMBER].append({
            "cik": _cik(ligne.RPTOWNERCIK), "nom": _chaine(ligne.RPTOWNERNAME),
            "dirigeant": officier or administrateur,
            "poste": _poste(_chaine(ligne.RPTOWNER_TITLE), officier, administrateur, "TenPercentOwner" in relations)})
    transactions = _table(archive, "NONDERIV_TRANS")
    transactions = transactions[transactions.TRANS_CODE.isin(list(_SENS))]
    rangs = defaultdict(int)
    operations = []
    for ligne in transactions.itertuples():
        emetteur = emetteurs.get(ligne.ACCESSION_NUMBER)
        declarant = _premier_dirigeant(declarants.get(ligne.ACCESSION_NUMBER, []))
        if emetteur is None or declarant is None or emetteur["publication"] is None:
            continue
        operation = _operation(
            ligne.ACCESSION_NUMBER, rangs[ligne.ACCESSION_NUMBER] + 1, emetteur["publication"],
            _jour_tsv(ligne.TRANS_DATE), ligne.TRANS_CODE, declarant, emetteur, ligne.TRANS_SHARES,
            ligne.TRANS_PRICEPERSHARE)
        if operation is not None:
            rangs[ligne.ACCESSION_NUMBER] += 1
            operations.append(operation)
    return operations


# --- index quotidien ---

def lire_index_jour(texte):
    """Chemins edgar/data/…txt des formulaires 4 (ni 4/A ni autre type), un seul par dépôt."""
    chemins, vus = [], set()
    for ligne in texte.splitlines():
        if not re.match(r"4\s", ligne):
            continue
        trouve = _CHEMIN.search(ligne)
        if trouve is not None and trouve[1] not in vus:
            vus.add(trouve[1])
            chemins.append(trouve[0])
    return chemins


# --- filtre et groupes ---

def achat_retenu(op, p):
    return (op.sens == "achat" and op.details.get("code") == "P" and bool(op.details.get("dirigeant"))
            and op.montant_min >= p["dirigeant_montant_min"])


def groupes(ops, p):
    """source_id des achats retenus quand au moins deux dirigeants différents achètent le même titre à
    groupe_dirigeants_jours jours d'écart au plus."""
    ecart = timedelta(days=p["groupe_dirigeants_jours"])
    par_ticker = defaultdict(list)
    for op in ops:
        if achat_retenu(op, p):
            par_ticker[op.ticker].append(op)
    groupes_trouves = set()
    for achats in par_ticker.values():
        achats.sort(key=lambda op: op.date_operation)
        for i, premier in enumerate(achats):
            for suivant in achats[i + 1:]:
                if suivant.date_operation - premier.date_operation > ecart:
                    break
                if suivant.personne_id != premier.personne_id:
                    groupes_trouves.update((premier.source_id, suivant.source_id))
    return groupes_trouves
