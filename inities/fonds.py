"""Nouvelles positions et ventes des grands investisseurs, d'après leurs déclarations 13F-HR trimestrielles (SEC).

Les 13F sont lus fonds par fonds sur EDGAR. Le XML est une donnée : on n'en extrait que des champs précis, et un
dépôt ou une table illisible pour un fonds est ignoré et signalé sans arrêter les autres fonds. Une table vide ou
illisible n'est jamais comparée : elle ferait passer toutes les positions pour des achats ou des ventes.
"""
import re
from collections import defaultdict
from datetime import date
from pathlib import Path

from lxml import etree

from commun import charger_config
from inities.modele import Operation
from inities.sec import ErreurSec

URL_SOUMISSIONS = "https://data.sec.gov/submissions/CIK{cik:0>10}.json"
URL_DEPOT = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/"
URL_TICKERS = "https://www.sec.gov/files/company_tickers.json"

# CIK (sans zéros de tête) -> (nom affiché, mot attendu dans le nom EDGAR)
FONDS = {
    "1067983": ("Berkshire Hathaway", "BERKSHIRE"),
    "1336528": ("Pershing Square", "PERSHING"),
    "1649339": ("Scion", "SCION"),
    "1656456": ("Appaloosa", "APPALOOSA"),
    "1536411": ("Duquesne Family Office", "DUQUESNE"),
    "1061768": ("Baupost", "BAUPOST"),
    "1079114": ("Greenlight", "GREENLIGHT"),
    "1040273": ("Third Point", "THIRD POINT"),
    "1061165": ("Lone Pine", "LONE PINE"),
    "1135730": ("Coatue", "COATUE"),
    "1103804": ("Viking", "VIKING"),
    "1167483": ("Tiger Global", "TIGER GLOBAL"),
    "1112520": ("Akre", "AKRE"),
}

_SUFFIXES = [["INC"], ["CORP"], ["CO"], ["LTD"], ["PLC"], ["HLDGS"], ["HOLDINGS"], ["CL", "A"], ["COM"], ["NEW"]]
_CUSIP = re.compile(r"[0-9A-Z]{9}")
_TICKER = re.compile(r"[A-Z0-9.\-]{1,10}")
_ACCESSION = re.compile(r"\d{10}-\d{2}-\d{6}")
_FICHIER_XML = re.compile(r"[A-Za-z0-9_.\-]+\.xml", re.IGNORECASE)  # EDGAR écrit parfois « .XML »
_LONGUEUR_NOM = 80


class ErreurFonds(Exception):
    pass


def _nombre(valeur):
    try:
        nombre = float((valeur or "").strip())
    except ValueError:
        return None
    return nombre if nombre == nombre and abs(nombre) != float("inf") else None


def _local(noeud):
    return etree.QName(noeud).localname if isinstance(noeud.tag, str) else ""


def lire_table(xml):
    """Table d'information d'un 13F : CUSIP -> nom, valeur et actions, les lignes d'un même CUSIP additionnées.

    Les lignes putCall (options) et les lignes dont le CUSIP ou la valeur est inutilisable sont écartées.
    Lève ErreurFonds si le XML est illisible ou si rien d'utilisable n'en sort.
    """
    parseur = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, encoding="utf-8")
    try:
        racine = etree.fromstring(xml.lstrip("﻿").encode("utf-8"), parseur)
    except (etree.XMLSyntaxError, ValueError) as erreur:
        raise ErreurFonds(f"XML illisible ({type(erreur).__name__})") from None
    if _local(racine) != "informationTable":
        raise ErreurFonds("ce n'est pas une table d'information 13F")
    table = {}
    for ligne in racine:
        if _local(ligne) != "infoTable":
            continue
        champs = {}
        for noeud in ligne.iter():
            champs.setdefault(_local(noeud), noeud)
        if "putCall" in champs:
            continue
        cusip = ((champs["cusip"].text or "") if "cusip" in champs else "").strip().upper()
        valeur = _nombre(champs["value"].text) if "value" in champs else None
        if not _CUSIP.fullmatch(cusip) or valeur is None or valeur <= 0:
            continue
        actions = _nombre(champs["sshPrnamt"].text) if "sshPrnamt" in champs else None
        actions = actions if actions is not None and actions >= 0 else 0.0
        nom = ((champs["nameOfIssuer"].text or "") if "nameOfIssuer" in champs else "").strip()[:_LONGUEUR_NOM]
        if cusip in table:
            table[cusip]["valeur"] += valeur
            table[cusip]["actions"] += actions
        else:
            table[cusip] = {"nom": nom, "valeur": valeur, "actions": actions}
    if not table:
        raise ErreurFonds("table d'information vide")
    return table


def normaliser(nom):
    """Nom en majuscules, sans ponctuation ni suffixe de société (INC, CORP, CL A…)."""
    mots = re.sub(r"[\W_]+", " ", nom.upper()).split()
    retire = True
    while retire and mots:
        retire = False
        for suffixe in _SUFFIXES:
            if mots[-len(suffixe):] == suffixe:
                del mots[-len(suffixe):]
                retire = True
                break
    return " ".join(mots)


def charger_correspondance(donnees):
    """Nom normalisé -> ticker d'après company_tickers.json ; un nom qui désigne plusieurs tickers est retiré."""
    if not isinstance(donnees, dict):
        return {}
    tickers = defaultdict(set)
    for entree in donnees.values():
        if not isinstance(entree, dict):
            continue
        ticker, titre = entree.get("ticker"), entree.get("title")
        if not isinstance(ticker, str) or not isinstance(titre, str):
            continue
        ticker, nom = ticker.strip().upper(), normaliser(titre)
        if nom and _TICKER.fullmatch(ticker):
            tickers[nom].add(ticker)
    return {nom: next(iter(ensemble)) for nom, ensemble in tickers.items() if len(ensemble) == 1}


def _cik(cik):
    cik = str(cik).strip()
    return str(int(cik)) if cik.isdigit() else cik


def nouvelles_positions(avant, apres, cik, accession, date_publication, date_trimestre, correspondance, p):
    """(opérations, noms sans ticker sûr) en comparant deux tables consécutives d'un même fonds.

    Achat : CUSIP absent d'avant et pesant au moins fonds_poids_min_pct de la valeur totale d'après.
    Vente : CUSIP présent dans avant et absent d'après.
    """
    cik = _cik(cik)
    fonds = FONDS.get(cik, (f"CIK {cik}",))[0]
    total = sum(ligne["valeur"] for ligne in apres.values())
    operations, sans_ticker = [], 0

    def operation(cusip, ligne, sens, **details):
        nonlocal sans_ticker
        ticker = correspondance.get(normaliser(ligne["nom"]))
        if ticker is None:
            sans_ticker += 1
            return
        operations.append(Operation(
            famille="fonds", personne_id=f"fonds:{cik}", personne=fonds, ticker=ticker, sens=sens,
            date_operation=date_trimestre, date_publication=date_publication, montant_min=ligne["valeur"],
            montant_max=ligne["valeur"], source_id=f"13f:{accession}:{cusip}",
            details={"cusip": cusip, "nom": ligne["nom"], "actions": ligne["actions"],
                     "trimestre": date_trimestre.isoformat(), **details}))

    for cusip, ligne in apres.items():
        poids = ligne["valeur"] / total * 100
        if cusip not in avant and poids >= p["fonds_poids_min_pct"]:
            operation(cusip, ligne, "achat", poids_pct=round(poids, 2))
    for cusip, ligne in avant.items():
        if cusip not in apres:
            operation(cusip, ligne, "vente")
    return operations, sans_ticker


# --- téléchargement ---

def _jour(valeur):
    try:
        return date.fromisoformat(valeur)
    except (TypeError, ValueError):
        return None


def _depots_13f(soumissions):
    """13F-HR (ni amendements ni autres formulaires) triés par trimestre, un seul par trimestre."""
    recents = ((soumissions.get("filings") or {}).get("recent")) or {}
    par_trimestre = {}
    for formulaire, publication, trimestre, accession in zip(
            recents.get("form", []), recents.get("filingDate", []), recents.get("reportDate", []),
            recents.get("accessionNumber", [])):
        publication, trimestre = _jour(publication), _jour(trimestre)
        if (formulaire != "13F-HR" or publication is None or trimestre is None
                or not isinstance(accession, str) or not _ACCESSION.fullmatch(accession)):
            continue
        if trimestre not in par_trimestre or publication > par_trimestre[trimestre]["publication"]:
            par_trimestre[trimestre] = {"accession": accession, "publication": publication, "trimestre": trimestre}
    return [par_trimestre[t] for t in sorted(par_trimestre)]


def _table_depot(client, cik, accession, dossier):
    """Table d'un dépôt : lue dans le cache du dossier, sinon téléchargée puis mise en cache une fois lisible."""
    fichier = dossier / f"{cik}-{accession}.xml"
    if fichier.exists():
        try:
            return lire_table(fichier.read_text(encoding="utf-8"))
        except (ErreurFonds, UnicodeDecodeError):
            fichier.unlink(missing_ok=True)
    base = URL_DEPOT.format(cik=cik, accession=accession.replace("-", ""))
    contenu = client.json(base + "index.json")
    elements = ((contenu or {}).get("directory") or {}).get("item") or []
    noms = [e.get("name") for e in elements if isinstance(e, dict) and isinstance(e.get("name"), str)]
    for nom in noms:
        if not _FICHIER_XML.fullmatch(nom) or nom.lower() == "primary_doc.xml":
            continue
        texte = client.texte(base + nom)
        if texte is None:
            continue
        try:
            table = lire_table(texte)
        except ErreurFonds:
            continue
        dossier.mkdir(parents=True, exist_ok=True)
        fichier.write_text(texte, encoding="utf-8")
        return table
    raise ErreurFonds(f"table illisible ou absente ({accession})")


def _signaler(fonds, raison):
    print(f"13F ignoré : {fonds} ({raison})")


def _raison(erreur):
    return str(erreur) if isinstance(erreur, (ErreurFonds, ErreurSec)) else type(erreur).__name__


def _lire_fonds(client, cik, nom, mot, debut, dossier, correspondance, p):
    soumissions = client.json(URL_SOUMISSIONS.format(cik=cik))
    if not isinstance(soumissions, dict):
        _signaler(nom, "dépôts introuvables")
        return [], 0
    if mot not in str(soumissions.get("name", "")).upper():
        _signaler(nom, f"nom EDGAR inattendu, « {mot} » absent")
        return [], 0
    depots = _depots_13f(soumissions)
    tables = {}

    def table(depot):
        accession = depot["accession"]
        if accession not in tables:
            try:
                tables[accession] = _table_depot(client, cik, accession, dossier)
            except Exception as erreur:  # un dépôt illisible ne doit pas arrêter le fonds suivant
                tables[accession] = erreur
        if isinstance(tables[accession], Exception):
            raise tables[accession]
        return tables[accession]

    operations, sans_ticker = [], 0
    for precedent, depot in zip(depots, depots[1:]):
        if depot["publication"] < debut:
            continue
        try:
            ops, manquants = nouvelles_positions(
                table(precedent), table(depot), cik, depot["accession"], depot["publication"], depot["trimestre"],
                correspondance, p)
        except Exception as erreur:
            _signaler(nom, f"{depot['accession']} : {_raison(erreur)}")
            continue
        operations.extend(ops)
        sans_ticker += manquants
    return operations, sans_ticker


def telecharger(client, debut, dossier, p=None):
    """(opérations, noms sans ticker sûr) des 13F-HR publiés depuis debut, pour chaque fonds de FONDS.

    Le trimestre précédent sert de point de comparaison même s'il a été publié avant debut. Un fonds dont le nom
    EDGAR ne contient pas le mot attendu, ou dont un dépôt est illisible, est ignoré et signalé.
    """
    p = p or charger_config()["copie"]
    dossier = Path(dossier)
    donnees = client.json(URL_TICKERS)
    correspondance = charger_correspondance(donnees)
    operations, sans_ticker = [], 0
    for cik, (nom, mot) in FONDS.items():
        try:
            ops, manquants = _lire_fonds(client, cik, nom, mot, debut, dossier, correspondance, p)
        except Exception as erreur:  # un fonds injoignable ne doit pas arrêter les autres
            _signaler(nom, _raison(erreur))
            continue
        operations.extend(ops)
        sans_ticker += manquants
    return operations, sans_ticker
