"""Déclarations d'achats et de ventes des élus de la Chambre : index annuel XML et PDF de chaque déclaration.

Le texte des PDF est une donnée : on n'en extrait que des champs précis par expression régulière.
"""
import io
import re
import zipfile
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree

import requests
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from inities.modele import Operation

URL_INDEX = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{annee}FD.zip"
URL_PDF = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{annee}/{doc_id}.pdf"
ENTETES = {"User-Agent": "analyse-marche"}
DELAI = 30  # secondes

# Le texte de pypdf coupe les lignes à l'intérieur d'une transaction : on travaille sur des blocs recollés.
_TRANSACTION = re.compile(
    r"\((?P<ticker>[A-Z][A-Z0-9.\-]{0,9})\)\s*\[ST\]\s*(?P<type>P|S(?: \(partial\))?)\s+(?P<date>\d\d/\d\d/\d{4})"
    r"\s+\d\d/\d\d/\d{4}\s+\$(?P<min>[\d,]+)\s*-\s*\$(?P<max>[\d,]+)")
_SEPARATEUR = re.compile(r"^F\s+S\s*:", re.MULTILINE)  # « Filing Status : », en petites capitales
_PROPRIETAIRE = re.compile(r"(?:^|\n)(SP|DC|JT)\s+\S")


class ErreurChambre(Exception):
    pass


def _entier(texte):
    return int(texte.replace(",", ""))


def lire_index(xml):
    """Déclarations de transactions (FilingType P) de l'index annuel ; un membre sans date ou sans numéro est ignoré."""
    try:
        racine = ElementTree.fromstring(xml)
    except ElementTree.ParseError as erreur:
        raise ErreurChambre(f"index de la Chambre : XML illisible ({erreur})") from erreur
    entrees = []
    for membre in racine.iter("Member"):
        if (membre.findtext("FilingType") or "").strip() != "P":
            continue
        try:
            publication = datetime.strptime((membre.findtext("FilingDate") or "").strip(), "%m/%d/%Y").date()
            annee = int(membre.findtext("Year"))
        except (ValueError, TypeError):
            continue
        doc_id = (membre.findtext("DocID") or "").strip()
        if not doc_id:
            continue
        entrees.append({
            "doc_id": doc_id,
            "nom": (membre.findtext("Last") or "").strip(),
            "prenom": (membre.findtext("First") or "").strip(),
            "etat": (membre.findtext("StateDst") or "").strip(),
            "annee": annee,
            "date_publication": publication,
        })
    return entrees


def _texte_pdf(pdf):
    pages = (page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf)).pages)
    return "\n".join(pages).replace("\x00", "")


def lire_declaration(pdf, entree):
    """Opérations (achats et ventes d'actions) d'une déclaration, ou None si le PDF n'a pas de texte lisible."""
    try:
        texte = _texte_pdf(pdf)
    except PyPdfError:
        return None
    if not texte.strip():
        return None
    personne_id = f"politicien:chambre:{entree['nom']}|{entree['prenom']}|{entree['etat']}"
    operations = []
    for bloc in _SEPARATEUR.split(texte):
        for m in _TRANSACTION.finditer(bloc):
            proprietaires = _PROPRIETAIRE.findall(bloc[:m.start()])
            operations.append(Operation(
                famille="politicien", personne_id=personne_id, personne=f"{entree['prenom']} {entree['nom']}",
                ticker=m["ticker"], sens="achat" if m["type"] == "P" else "vente",
                date_operation=datetime.strptime(m["date"], "%m/%d/%Y").date(),
                date_publication=entree["date_publication"],
                montant_min=_entier(m["min"]), montant_max=_entier(m["max"]),
                source_id=f"chambre:{entree['doc_id']}:{len(operations) + 1}",
                details={"proprietaire": proprietaires[-1]} if proprietaires else {}))
    return operations


def _lire(session, url):
    return session.request("GET", url, headers=ENTETES, timeout=DELAI)


def _index_annuel(session, annee):
    reponse = _lire(session, URL_INDEX.format(annee=annee))
    if reponse.status_code != 200:
        raise ErreurChambre(f"index {annee} de la Chambre indisponible (code {reponse.status_code})")
    try:
        with zipfile.ZipFile(io.BytesIO(reponse.content)) as archive:
            nom = next((n for n in archive.namelist() if n.lower().endswith(".xml")), None)
            if nom is None:
                raise ErreurChambre(f"index {annee} de la Chambre : aucun fichier XML dans le zip")
            return lire_index(archive.read(nom))
    except zipfile.BadZipFile as erreur:
        raise ErreurChambre(f"index {annee} de la Chambre : zip invalide") from erreur


def _pdf(session, entree, dossier):
    """Octets du PDF (cache disque), ou None s'il est introuvable (404). Toute autre réponse que 200 (5xx, 429...) lève
    ErreurChambre : le site est en panne, le PDF n'est pas « introuvable »."""
    fichier = dossier / f"{entree['doc_id']}.pdf"
    if fichier.exists():
        return fichier.read_bytes()
    reponse = _lire(session, URL_PDF.format(annee=entree["annee"], doc_id=entree["doc_id"]))
    if reponse.status_code == 404:
        return None
    if reponse.status_code != 200:
        raise ErreurChambre(f"PDF {entree['doc_id']} de la Chambre indisponible (code {reponse.status_code})")
    dossier.mkdir(parents=True, exist_ok=True)
    fichier.write_bytes(reponse.content)
    return reponse.content


def telecharger(annees, dossier, session=None):
    """(opérations, déclarations lues, déclarations illisibles) pour les années demandées."""
    session = session or requests.Session()
    dossier = Path(dossier)
    operations, lues, illisibles = [], 0, 0
    for annee in annees:
        for entree in _index_annuel(session, annee):
            try:
                pdf = _pdf(session, entree, dossier)
                lues_ici = lire_declaration(pdf, entree) if pdf is not None else None
            except Exception:  # une mauvaise déclaration ne doit jamais arrêter le lot quotidien
                lues_ici = None
            if lues_ici is None:
                illisibles += 1
                continue
            lues += 1
            operations.extend(lues_ici)
    return operations, lues, illisibles
