"""Déclarations d'achats et de ventes des sénateurs (efdsearch.senate.gov), lues seulement après l'accord de l'utilisateur.

Accepter l'accord du site est une décision de l'utilisateur : `copie.senat_accepte` vaut `true` seulement s'il l'a mis.
Le HTML lu est une donnée : on n'en extrait que des champs précis, jamais le commentaire libre.
"""
import re
from datetime import date, datetime
from pathlib import Path

import requests
from lxml import html as lxml_html
from lxml.etree import LxmlError

from inities.modele import Operation

URL_ACCUEIL = "https://efdsearch.senate.gov/search/home/"
URL_RECHERCHE = "https://efdsearch.senate.gov/search/report/data/"
URL_DECLARATION = "https://efdsearch.senate.gov/search/view/ptr/{uuid}/"
ENTETES = {"User-Agent": "analyse-marche"}
DELAI = 30  # secondes
LONGUEUR_PAGE = 100
PAGES_MAX = 200  # garde-fou contre une pagination sans fin

COLONNES = ("Transaction Date", "Owner", "Ticker", "Asset Type", "Type", "Amount")
_LIEN = re.compile(r"/search/view/(?P<genre>ptr|paper)/(?P<uuid>[0-9a-fA-F-]{8,40})/")
_TICKER = re.compile(r"[A-Z][A-Z0-9.\-]{0,9}")
_MONTANT = re.compile(r"\$\s*(?P<min>[\d,]+)\s*-\s*\$\s*(?P<max>[\d,]+)")


class AccordNonAccepte(Exception):
    """L'utilisateur n'a pas accepté l'accord du site du Sénat (`copie.senat_accepte`)."""


class ErreurSenat(Exception):
    pass


def _date(texte):
    return datetime.strptime(texte.strip(), "%m/%d/%Y").date()


def _entier(texte):
    return int(texte.replace(",", ""))


def ouvrir_session(config, session=None):
    """Session ouverte sur le site du Sénat, après l'accord de l'utilisateur ; aucune requête sans cet accord."""
    if (config.get("copie") or {}).get("senat_accepte") is not True:
        raise AccordNonAccepte("accord du site du Sénat non accepté : copie.senat_accepte doit valoir true")
    session = session or requests.Session()
    accueil = session.request("GET", URL_ACCUEIL, headers=ENTETES, timeout=DELAI)
    if accueil.status_code != 200:
        raise ErreurSenat(f"page d'accueil du Sénat indisponible (code {accueil.status_code})")
    jetons = lxml_html.fromstring(accueil.text or "<html/>").xpath('//input[@name="csrfmiddlewaretoken"]/@value')
    if not jetons:
        raise ErreurSenat("page d'accueil du Sénat : jeton csrfmiddlewaretoken introuvable")
    reponse = session.request("POST", URL_ACCUEIL, headers={**ENTETES, "Referer": URL_ACCUEIL}, timeout=DELAI,
                              data={"prohibition_agreement": "1", "csrfmiddlewaretoken": jetons[0]})
    if reponse.status_code != 200:
        raise ErreurSenat(f"accord du Sénat refusé par le site (code {reponse.status_code})")
    return session


def lire_recherche(donnees):
    """Déclarations (clés uuid, nom, prenom, date_publication, papier) d'une page de résultats ; ligne incomplète ignorée."""
    lignes = []
    for ligne in donnees.get("data") or []:
        try:
            prenom, nom, _, lien, publication = ligne[:5]
            trouve = _LIEN.search(lien)
            if trouve is None:
                continue
            lignes.append({"uuid": trouve["uuid"], "nom": nom.strip(), "prenom": prenom.strip(),
                           "date_publication": _date(publication), "papier": trouve["genre"] == "paper"})
        except (ValueError, TypeError, AttributeError):
            continue
    return lignes


def rechercher(session, debut: date, fin: date):
    """Déclarations de transactions (type 11) publiées entre `debut` et `fin` inclus, toutes pages."""
    cookies = getattr(session, "cookies", None)
    jeton = cookies.get("csrftoken") if cookies is not None else None
    entetes = {**ENTETES, "Referer": URL_ACCUEIL}
    if jeton:
        entetes["X-CSRFToken"] = jeton
    lignes = []
    for page in range(PAGES_MAX):
        formulaire = {
            "start": page * LONGUEUR_PAGE, "length": LONGUEUR_PAGE, "report_types": "[11]", "filer_types": "[]",
            "submitted_start_date": debut.strftime("%m/%d/%Y 00:00:00"),
            "submitted_end_date": fin.strftime("%m/%d/%Y 23:59:59"),
            "candidate_state": "", "senator_state": "", "office_id": "", "first_name": "", "last_name": "",
            "csrfmiddlewaretoken": jeton or ""}
        reponse = session.request("POST", URL_RECHERCHE, headers=entetes, timeout=DELAI, data=formulaire)
        if reponse.status_code != 200:
            raise ErreurSenat(f"recherche du Sénat indisponible (code {reponse.status_code})")
        try:
            donnees = reponse.json()
        except ValueError as erreur:
            raise ErreurSenat("recherche du Sénat : réponse JSON illisible") from erreur
        brutes = donnees.get("data") or []
        lignes.extend(lire_recherche(donnees))
        if len(brutes) < LONGUEUR_PAGE:
            break
    return lignes


def _tableau(racine):
    """(lignes, position des colonnes) du tableau des transactions, ou None s'il manque une colonne."""
    for tableau in racine.xpath("//table"):
        entetes = [" ".join(th.text_content().split()) for th in tableau.xpath(".//thead//th")]
        if all(c in entetes for c in COLONNES):
            return tableau.xpath(".//tbody/tr"), {c: entetes.index(c) for c in COLONNES}
    return None


def lire_declaration(page, entree):
    """Opérations (achats et ventes d'actions) d'une déclaration, ou None si la page n'a pas le tableau attendu."""
    try:
        racine = lxml_html.fromstring(page)
    except (LxmlError, ValueError):
        return None
    tableau = _tableau(racine)
    if tableau is None:
        return None
    lignes, colonne = tableau
    personne_id = f"politicien:senat:{entree['nom']}|{entree['prenom']}"
    operations = []
    for ligne in lignes:
        cellules = [" ".join(td.text_content().split()) for td in ligne.xpath("./td")]
        if len(cellules) <= max(colonne.values()):
            continue
        champ = {nom: cellules[i] for nom, i in colonne.items()}
        genre = champ["Type"]
        if champ["Asset Type"] != "Stock" or not (genre == "Purchase" or genre.startswith("Sale")):
            continue
        montant = _MONTANT.fullmatch(champ["Amount"])
        if montant is None or not _TICKER.fullmatch(champ["Ticker"]):
            continue
        try:
            date_operation = _date(champ["Transaction Date"])
        except ValueError:
            continue
        proprietaire = champ["Owner"]
        operations.append(Operation(
            famille="politicien", personne_id=personne_id, personne=f"{entree['prenom']} {entree['nom']}",
            ticker=champ["Ticker"], sens="achat" if genre == "Purchase" else "vente",
            date_operation=date_operation, date_publication=entree["date_publication"],
            montant_min=_entier(montant["min"]), montant_max=_entier(montant["max"]),
            source_id=f"senat:{entree['uuid']}:{len(operations) + 1}",
            details={"proprietaire": proprietaire} if proprietaire and proprietaire != "--" else {}))
    return operations


def _page(session, entree, dossier):
    """(HTML, vient du cache) de la déclaration, ou (None, False) si elle est introuvable (404). Toute autre réponse
    que 200 (5xx, 429...) lève ErreurSenat : le site est en panne, la page n'est pas « introuvable »."""
    fichier = dossier / f"{entree['uuid']}.html"
    if fichier.exists():
        return fichier.read_text(encoding="utf-8"), True
    reponse = session.request("GET", URL_DECLARATION.format(uuid=entree["uuid"]), headers=ENTETES, timeout=DELAI)
    if reponse.status_code == 404:
        return None, False
    if reponse.status_code != 200:
        raise ErreurSenat(f"page {entree['uuid']} du Sénat indisponible (code {reponse.status_code})")
    return reponse.text, False


def telecharger(config, debut, fin, dossier, session=None):
    """(opérations, déclarations lues, déclarations illisibles) publiées entre `debut` et `fin`.

    Lève AccordNonAccepte, avant toute requête, si l'utilisateur n'a pas accepté l'accord du site.
    """
    session = ouvrir_session(config, session)
    dossier = Path(dossier)
    operations, lues, illisibles = [], 0, 0
    for entree in rechercher(session, debut, fin):
        if entree["papier"]:
            illisibles += 1
            continue
        try:
            page, du_cache = _page(session, entree, dossier)
            lues_ici = lire_declaration(page, entree) if page is not None else None
            if lues_ici is not None and not du_cache:  # une page de connexion ou d'erreur n'est jamais gardée
                dossier.mkdir(parents=True, exist_ok=True)
                (dossier / f"{entree['uuid']}.html").write_text(page, encoding="utf-8")
        except Exception:  # une mauvaise déclaration ne doit jamais arrêter le lot quotidien
            lues_ici = None
        if lues_ici is None:
            illisibles += 1
            continue
        lues += 1
        operations.extend(lues_ici)
    return operations, lues, illisibles
