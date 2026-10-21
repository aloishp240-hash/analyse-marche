"""Test sur le passé de la copie d'initiés : télécharge les déclarations, mesure chaque personne contre QQQ, simule le
portefeuille et propose la liste des personnes à copier.

    python backtest_copie.py                        télécharge (long), écrit rapport_copie.md et copie/*.json
    python backtest_copie.py --sans-telechargement  refait le calcul 100 % hors ligne (déclarations, cours et actions gardés)
    python backtest_copie.py --valider              enregistre la proposition dans copie/suivis.json (sur demande)

Les déclarations déjà lues sont gardées dans donnees/copie/ : un deuxième lancement ne redemande que le reste.
"""
import argparse
import json
import sys
from dataclasses import replace
from datetime import date

import pandas as pd

from commun import RACINE, charger_config
from inities import chambre, cours, dirigeants, fonds, mesure, rapport, senat, simulation
from inities.dirigeants import achat_retenu
from inities.modele import depuis_dict, nettoyer_ticker, vers_dict
from inities.sec import ClientSec

DONNEES = RACINE / "donnees" / "copie"
COPIE = RACINE / "copie"
RAPPORT = RACINE / "rapport_copie.md"
DATE_CHOIX = date(2026, 10, 21)
DEBUT_SIMULATION = date(2025, 10, 1)
DEBUT_DECLARATIONS = date(2024, 1, 1)
DEBUT_COURS = date(2023, 11, 15)  # de quoi calculer l'univers (20 séances) des premières publications de 2024
PREMIER_TRIMESTRE = (2024, 1)
SOURCE_LIMITE = "cours Yahoo (limite de débit)"
EXEMPLES = 5  # titres cités comme exemples quand des cours ou des nombres d'actions manquent


def _message(erreur):
    return f"{type(erreur).__name__}: {erreur}".replace("\n", " ")[:200]


def _trimestres(premier, dernier):
    annee, trimestre = premier
    while (annee, trimestre) <= dernier:
        yield annee, trimestre
        annee, trimestre = (annee + 1, 1) if trimestre == 4 else (annee, trimestre + 1)


def _nom_trimestre(annee, trimestre):
    return f"{annee}T{trimestre}"


# --- lecture des sources ---

class _Sec:
    """Client SEC créé au premier usage : sans contact, seules les sources SEC échouent."""

    def __init__(self, dossier):
        self._dossier, self._client = dossier, None

    def __call__(self):
        if self._client is None:
            self._client = ClientSec(dossier_cache=self._dossier / "sec")
        return self._client


def _lire_chambre(dossier, date_choix):
    ops, lues, illisibles = chambre.telecharger(range(2024, date_choix.year + 1), dossier / "chambre")
    return ops, {"lues": lues, "illisibles": illisibles}


def _lire_senat(config, dossier, date_choix):
    ops, lues, illisibles = senat.telecharger(config, DEBUT_DECLARATIONS, date_choix, dossier / "senat")
    return ops, {"lues": lues, "illisibles": illisibles}


def _jeu_trimestriel(client, annee, trimestre, dossier):
    """Opérations du jeu Form 345 du trimestre, ou None s'il n'est pas (encore) publié. Le zip n'est gardé sur disque
    qu'une fois lisible."""
    fichier = dossier / "form345" / f"{annee}q{trimestre}.zip"
    contenu = fichier.read_bytes() if fichier.exists() else client.octets(dirigeants.url_jeu(annee, trimestre))
    if contenu is None:
        return None
    ops = dirigeants.lire_jeu(contenu)
    if not fichier.exists():
        fichier.parent.mkdir(parents=True, exist_ok=True)
        fichier.write_bytes(contenu)
    return ops


def _achat_utile(op, p):
    return op.sens == "achat" and (op.famille != "dirigeant" or achat_retenu(op, p))


def _garder_utiles(ops, p, suivis):
    """Opérations utiles de ops ; `suivis` (personne, titre) retient les achats utiles déjà vus, pour garder leurs ventes
    même dans un lot suivant."""
    suivis.update((op.personne_id, op.ticker) for op in ops if _achat_utile(op, p))
    return [op for op in ops
            if _achat_utile(op, p) or (op.sens == "vente" and (op.personne_id, op.ticker) in suivis)]


def filtrer(ops, p):
    """Opérations utiles : tous les achats d'élus et de fonds, les achats de dirigeants qui passent le filtre, et les
    ventes d'une personne sur un titre qu'elle a acheté. Le reste ne peut influencer ni la mesure ni la simulation."""
    return _garder_utiles(ops, p, set())


def _lire_dirigeants(sec, dossier, date_choix, p):
    """Les jeux trimestriels sont filtrés un par un : on ne garde pas les millions d'opérations brutes en mémoire."""
    client = sec()
    ops, lues, illisibles, absents, details, dernier = [], 0, 0, [], [], None
    brutes, suivis = 0, set()
    for annee, trimestre in _trimestres(PREMIER_TRIMESTRE, (date_choix.year, (date_choix.month - 1) // 3 + 1)):
        try:
            ops_trimestre = _jeu_trimestriel(client, annee, trimestre, dossier)
        except Exception as erreur:
            illisibles += 1
            details.append(f"{_nom_trimestre(annee, trimestre)} : {_message(erreur)}")
            continue
        if ops_trimestre is None:  # un 404 sur le trimestre en cours, pas encore publié, n'est pas une erreur
            absents.append((annee, trimestre))
            continue
        lues += 1
        brutes += len(ops_trimestre)
        ops.extend(_garder_utiles(ops_trimestre, p, suivis))
        dernier = (annee, trimestre)
    trous = [q for q in absents if dernier is not None and q < dernier]
    illisibles += len(trous)
    details += [f"{_nom_trimestre(*q)} : jeu absent" for q in trous]
    if dernier is not None:
        details.insert(0, f"dernier trimestre publié : {_nom_trimestre(*dernier)}")
    details.insert(0, f"avant filtre : {brutes} opérations")
    return ops, {"lues": lues, "illisibles": illisibles, "libelle": ["jeu trimestriel lu", "jeux trimestriels lus"],
                 "a_part": True,  # des jeux et non des PDF : hors du total des déclarations lues et illisibles
                 "detail": " ; ".join(details) or None,
                 "dernier_trimestre": _nom_trimestre(*dernier) if dernier else None}


def _fonds_sans_13f(client, date_choix, p):
    """Noms des fonds qui n'ont publié aucun 13F-HR dans les recent_mois mois avant date_choix."""
    limite = (pd.Timestamp(date_choix) - pd.DateOffset(months=p["recent_mois"])).date()
    sans = []
    for cik, (nom, _) in fonds.FONDS.items():
        try:
            depots = fonds._depots_13f(client.json(fonds.URL_SOUMISSIONS.format(cik=cik)) or {})
        except Exception:
            continue  # fonds.telecharger a déjà signalé ce fonds s'il est injoignable
        if not any(limite <= depot["publication"] <= date_choix for depot in depots):
            sans.append(nom)
    return sans


def _lire_fonds(sec, dossier, date_choix, p):
    client = sec()
    ops, sans_ticker = fonds.telecharger(client, DEBUT_DECLARATIONS, dossier / "13f", p)
    return ops, {"sans_ticker": sans_ticker, "fonds_sans_13f": _fonds_sans_13f(client, date_choix, p)}


def _lire_source(nom, lecture):
    """(opérations, fiche de la source) ; un échec marque la source et laisse les autres continuer."""
    try:
        ops, fiche = lecture()
        fiche = {"nom": nom, "etat": "lue", "operations": len(ops), **fiche}
    except Exception as erreur:
        ops, fiche = [], {"nom": nom, "etat": "echec", "detail": _message(erreur)}
    print(rapport.ligne_source(fiche), flush=True)
    return ops, fiche


def telecharger_tout(config, dossier, date_choix):
    """(opérations utiles, lecture) de toutes les sources ; une source en échec n'arrête pas les autres."""
    p = config["copie"]
    sec = _Sec(dossier)
    senat_lu = p.get("senat_accepte") is True
    sources = [("Chambre", lambda: _lire_chambre(dossier, date_choix)),
               ("Sénat", (lambda: _lire_senat(config, dossier, date_choix)) if senat_lu else None),
               ("Form 4 (jeux trimestriels de la SEC)", lambda: _lire_dirigeants(sec, dossier, date_choix, p)),
               ("13F (fonds)", lambda: _lire_fonds(sec, dossier, date_choix, p))]
    ops, fiches = [], []
    for nom, lecture in sources:
        if lecture is None:
            fiche = {"nom": nom, "etat": "non lue", "detail": "accord non donné"}
            print(rapport.ligne_source(fiche), flush=True)
        else:
            ops_source, fiche = _lire_source(nom, lecture)
            ops.extend(ops_source)
        fiches.append(fiche)
    lecture = {"fenetre_mois": p["fenetre_mois"]}
    for fiche in fiches:
        for cle in ("sans_ticker", "fonds_sans_13f"):
            if cle in fiche:
                lecture[cle] = fiche.pop(cle)
        if fiche.get("dernier_trimestre"):
            lecture["dernier_trimestre_dirigeants"] = fiche["dernier_trimestre"]
        fiche.pop("dernier_trimestre", None)
    lecture["sources"] = fiches
    return filtrer(ops, p), lecture


# --- disque ---

def _ecrire_json(fichier, contenu):
    fichier.parent.mkdir(parents=True, exist_ok=True)
    fichier.write_text(json.dumps(contenu, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def _sauver_lecture(dossier, ops, lecture):
    _ecrire_json(dossier / "operations.json", [vers_dict(op) for op in ops])
    _ecrire_json(dossier / "lecture.json", lecture)


def _relire_lecture(dossier):
    """(opérations, lecture) gardées par un lancement précédent, ou None."""
    try:
        ops = [depuis_dict(d) for d in json.loads((dossier / "operations.json").read_text(encoding="utf-8"))]
        lecture = json.loads((dossier / "lecture.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, KeyError):
        return None
    return ops, lecture


# --- calcul ---

def _cours_du_cache(tickers, debut, fin, dossier):
    """Cours déjà gardés sur disque par cours.charger, sans rien demander à Yahoo ; un titre absent du cache (ou dont
    le cache ne couvre pas la période) est simplement absent du résultat."""
    resultat = {}
    for ticker in tickers:
        try:
            entree = pd.read_pickle(cours._fichier(dossier, ticker))
            if entree["debut"] <= debut and entree["fin"] >= fin:
                df = entree["cours"].loc[pd.Timestamp(debut):pd.Timestamp(fin)]
                if isinstance(df, pd.DataFrame) and not df.empty:
                    resultat[ticker] = df
        except Exception:
            continue
    return resultat


def _actions_du_cache(tickers, dossier):
    """Nombres d'actions déjà gardés dans actions.json, sans rien demander à Yahoo."""
    try:
        connues = json.loads((dossier / "actions.json").read_text())
    except (OSError, ValueError):
        return {}
    return {t: connues[t] for t in tickers if t in connues} if isinstance(connues, dict) else {}


def _charger_cours(tickers, debut, fin, dossier, hors_ligne=False, limites=None):
    """Cours des tickers ; `limites` reçoit ceux que Yahoo limite encore après les reprises (jamais hors ligne)."""
    if hors_ligne:
        return _cours_du_cache(tickers, debut, fin, dossier)
    return cours.charger(tickers, debut, fin, dossier, limites=limites,
                         progres=lambda faits, total: print(f"  cours : lot {faits}/{total}", flush=True))


def _manquants(tickers):
    """Nombre de titres et quelques exemples, pour le rapport."""
    tickers = sorted(tickers)
    return {"nombre": len(tickers), "exemples": tickers[:EXEMPLES]}


def _phrase_manquants(nom, manquants):
    exemples = f" (par exemple {', '.join(manquants['exemples'])})" if manquants["exemples"] else ""
    return f"{manquants['nombre']} {nom}{exemples}"


def _ligne_titres(lecture):
    morceaux = [_phrase_manquants("sans cours", lecture["sans_cours"]),
                _phrase_manquants("sans nombre d'actions", lecture["sans_actions"])]
    if "ticker_illisible" in lecture:
        morceaux.append(_phrase_manquants("ticker illisible", lecture["ticker_illisible"]))
    if lecture.get("limite_yahoo", {}).get("nombre"):
        morceaux.append(_phrase_manquants("limite Yahoo", lecture["limite_yahoo"]))
    return "Titres : " + " ; ".join(morceaux)


def _a_mesurer(op, debut_simulation, date_choix, p):
    """L'achat peut-il être mesuré ou copié ? Mêmes critères que mesure.selectionner (élus et fonds, sur la fenêtre la
    plus large utilisée par la simulation), mesure.bilan_dirigeants et simulation.simuler (dirigeants retenus). Les
    ventes ne demandent jamais de cours : elles ne servent qu'à fixer une date de sortie."""
    if op.famille == "dirigeant":
        return achat_retenu(op, p) and op.date_publication < date_choix
    debut = (pd.Timestamp(min(debut_simulation, date_choix)) - pd.DateOffset(months=p["fenetre_mois"])).date()
    return mesure.achat_a_mesurer(op, debut, date_choix, p)


def _nettoyer_tickers(ops, debut_simulation, date_choix, p):
    """(opérations au ticker nettoyé, tickers à demander, tickers illisibles). Seul endroit où un ticker est nettoyé :
    une opération au ticker illisible est écartée et son ticker brut compté (s'il aurait servi) ; seuls les titres
    d'achats mesurables sont demandés à Yahoo."""
    nettes, utiles, illisibles = [], set(), set()
    for op in ops:
        ticker = nettoyer_ticker(op.ticker)
        a_mesurer = _a_mesurer(op, debut_simulation, date_choix, p)
        if ticker is None:
            if a_mesurer:
                illisibles.add(op.ticker)
            continue
        nettes.append(op if ticker == op.ticker else replace(op, ticker=ticker))
        if a_mesurer:
            utiles.add(ticker)
    return nettes, sorted(utiles), illisibles


def _noter_limite_yahoo(lecture, limites):
    """Les titres que Yahoo limite encore après les reprises ne sont pas des titres « sans données » : on les compte à
    part et on ajoute une source en échec, ce qui rend la proposition incomplète (--valider la refuse)."""
    lecture["limite_yahoo"] = _manquants(limites)
    sources = [s for s in lecture.get("sources", []) if s.get("nom") != SOURCE_LIMITE]
    if limites:
        n = len(limites)
        sources.append({"nom": SOURCE_LIMITE, "etat": "echec",
                        "detail": f"{n} titre{'s' if n > 1 else ''} sans réponse malgré les reprises"})
    lecture["sources"] = sources


def analyser(ops, lecture, p, date_choix, debut_simulation, dossier, hors_ligne=False):
    """(classement, bilan des dirigeants, résultats des deux simulations) ; complète `lecture`. Hors ligne, rien n'est
    demandé à Yahoo : les titres absents du cache sont comptés sans cours."""
    dossier_cours = dossier / "cours"
    brutes = ops
    ops, tickers, illisibles = _nettoyer_tickers(brutes, debut_simulation, date_choix, p)
    lecture["ticker_illisible"] = _manquants(illisibles)
    print(f"Cours : {len(tickers)} titres, QQQ et EURUSD", flush=True)
    limites_cours, limites_actions = set(), set()
    reference = _charger_cours(["QQQ", "EURUSD=X"], DEBUT_COURS, date_choix, dossier_cours, hors_ligne, limites_cours)
    qqq = reference.get("QQQ")
    if qqq is None:
        raise RuntimeError("cours de QQQ introuvables"
                           + (" dans le cache" if hors_ligne else " (limite de débit de Yahoo)"
                              if "QQQ" in limites_cours else "") + " : impossible de comparer")
    eurusd = reference.get("EURUSD=X")
    lecture["eurusd_absent"] = eurusd is None
    historique = _charger_cours(tickers, DEBUT_COURS, date_choix, dossier_cours, hors_ligne, limites_cours)
    avec_cours = [t for t in tickers if t in historique]
    actions = (_actions_du_cache(avec_cours, dossier_cours) if hors_ligne
               else cours.actions_en_circulation(avec_cours, dossier_cours, limites=limites_actions))
    lecture["sans_cours"] = _manquants(set(tickers) - set(avec_cours) - limites_cours)
    lecture["sans_actions"] = _manquants({t for t in avec_cours if t not in actions} - limites_actions)
    _noter_limite_yahoo(lecture, limites_cours | limites_actions)
    print(_ligne_titres(lecture), flush=True)
    classement = mesure.selectionner(ops, historique, qqq, actions, date_choix, p)
    bilan_dirigeants = mesure.bilan_dirigeants(ops, historique, qqq, actions, date_choix, p)
    en_ligne = {ligne["personne_id"] for ligne in classement}
    lecture["personnes_sans_ligne"] = len({op.personne_id for op in brutes if op.famille in ("politicien", "fonds")
                                           and op.sens == "achat"} - en_ligne)
    taux = eurusd["Close"] if eurusd is not None else pd.Series(dtype=float)
    resultats = {}
    for cle, frais in (("sans_frais", simulation.frais_nuls), ("avec_frais", simulation.frais_courtier(taux))):
        print(f"Simulation {cle.replace('_', ' ')} : {debut_simulation} → {date_choix}", flush=True)
        resultats[cle] = simulation.simuler(ops, historique, qqq, actions, debut_simulation, date_choix, p, frais)
    return classement, bilan_dirigeants, resultats


def _bilan_json(b):
    return {cle: (valeur.isoformat() if isinstance(valeur, date) else [list(m) for m in valeur] if cle == "mesures"
                  else valeur) for cle, valeur in b.items()}


def _sources_en_echec(lecture):
    return [s["nom"] for s in lecture.get("sources", []) if s.get("etat") == "echec"]


def _ecrire_resultats(classement, bilan_dirigeants, resultats, lecture, date_choix, dossier_copie, fichier_rapport):
    fichier_rapport.write_text(rapport.ecrire(classement, bilan_dirigeants, resultats, lecture, date_choix),
                               encoding="utf-8")
    retenus = [{"personne_id": ligne["personne_id"], "personne": ligne["personne"], "famille": ligne["famille"]}
               for ligne in classement if ligne["retenu"]]
    echecs = _sources_en_echec(lecture)
    _ecrire_json(dossier_copie / "proposition.json",
                 {"date_choix": date_choix.isoformat(), "complet": not echecs, "sources_en_echec": echecs,
                  "personnes": retenus})
    bilans = {ligne["personne_id"]: _bilan_json(ligne["bilan"]) for ligne in classement}
    bilans["dirigeants"] = _bilan_json(bilan_dirigeants)
    _ecrire_json(dossier_copie / "bilans.json", bilans)
    return retenus


def lancer(sans_telechargement=False, config=None, dossier_donnees=DONNEES, dossier_copie=COPIE,
           fichier_rapport=RAPPORT, date_choix=DATE_CHOIX, debut_simulation=DEBUT_SIMULATION):
    config = config or charger_config()
    p = config["copie"]
    if sans_telechargement:
        relu = _relire_lecture(dossier_donnees)
        if relu is None:
            print("Aucune déclaration gardée : lance d'abord backtest_copie.py sans --sans-telechargement.")
            return 1
        ops, lecture = relu
        print(f"Déclarations relues depuis le disque : {len(ops)} opérations", flush=True)
    else:
        ops, lecture = telecharger_tout(config, dossier_donnees, date_choix)
        _sauver_lecture(dossier_donnees, ops, lecture)  # avant les cours : un incident plus loin ne perd rien
    try:
        classement, bilan_dirigeants, resultats = analyser(ops, lecture, p, date_choix, debut_simulation,
                                                           dossier_donnees, hors_ligne=sans_telechargement)
    except Exception as erreur:
        # une ancienne proposition ne doit pas rester validable alors que cette analyse n'a pas abouti
        (dossier_copie / "proposition.json").unlink(missing_ok=True)
        if not isinstance(erreur, RuntimeError):
            raise
        print(f"Arrêt : {erreur}")
        return 1
    retenus = _ecrire_resultats(classement, bilan_dirigeants, resultats, lecture, date_choix, dossier_copie,
                                fichier_rapport)
    lues, illisibles = rapport.totaux(lecture)
    part = f"{illisibles / (lues + illisibles) * 100:.1f}".replace(".", ",") if lues + illisibles else "0,0"
    nombre = len(retenus)
    print(f"Résumé : {lues} lues, {illisibles} illisibles ({part} %), {nombre} "
          f"{'personne retenue' if nombre <= 1 else 'personnes retenues'}.")
    print(_ligne_titres(lecture) + ".")
    for personne in retenus:
        print(f"  - {personne['personne']} ({personne['famille']})")
    print(f"Rapport : {fichier_rapport}")
    echecs = _sources_en_echec(lecture)
    if echecs:
        print(f"ATTENTION : source(s) en échec : {', '.join(echecs)}. La proposition est incomplète et --valider la "
              "refusera.")
        return 1
    return 0


def valider(dossier_copie=COPIE, aujourd_hui=None):
    """Copie copie/proposition.json dans copie/suivis.json, avec la date de validation."""
    try:
        proposition = json.loads((dossier_copie / "proposition.json").read_text(encoding="utf-8"))
        personnes = proposition["personnes"]
        if not isinstance(personnes, list):
            raise ValueError
    except (OSError, ValueError, TypeError, KeyError):
        print("Aucune proposition à valider : lance d'abord backtest_copie.py.")
        return 1
    if proposition.get("complet") is not True:
        echecs = proposition.get("sources_en_echec")
        if echecs:
            print(f"Validation refusée : la proposition est incomplète, source(s) en échec : {', '.join(echecs)}. "
                  "Relance backtest_copie.py quand ces sources répondent.")
        else:
            print("Validation refusée : la proposition ne dit pas si toutes les sources ont été lues. Relance "
                  "backtest_copie.py (ou backtest_copie.py --sans-telechargement) pour la refaire.")
        return 1
    _ecrire_json(dossier_copie / "suivis.json",
                 {"valide_le": (aujourd_hui or date.today()).isoformat(), "personnes": personnes})
    print(f"{len(personnes)} personne(s) validée(s) dans {dossier_copie / 'suivis.json'}")
    return 0


def main(argv=None):
    parseur = argparse.ArgumentParser(description="Test sur le passé de la copie d'initiés.")
    parseur.add_argument("--valider", action="store_true",
                         help="enregistre copie/proposition.json dans copie/suivis.json (sur demande de l'utilisateur)")
    parseur.add_argument("--sans-telechargement", action="store_true",
                         help="entièrement hors ligne : réutilise les déclarations, les cours et les nombres d'actions gardés sur "
                              "disque (un titre absent du cache est compté sans cours)")
    options = parseur.parse_args(argv)
    if options.valider:
        return valider()
    return lancer(sans_telechargement=options.sans_telechargement)


if __name__ == "__main__":
    sys.exit(main())
