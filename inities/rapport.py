"""Rapport Markdown du test sur le passé : liste proposée, classement, dirigeants, portefeuille et limites des données."""
from collections import Counter

import pandas as pd

from inities.mesure import bat_qqq

AVERTISSEMENT = "Résultats passés, pas une garantie ; pas un conseil financier."
_FAMILLES = {"politicien": "élu", "fonds": "fonds", "dirigeant": "dirigeant"}
_SCENARIOS = (("sans_frais", "Sans frais"), ("avec_frais", "Avec frais (1 € + 0,1 %)"))


def _nombre(valeur, decimales=1, signe=False):
    if valeur is None:
        return "—"
    texte = f"{valeur:{'+' if signe else ''},.{decimales}f}".replace(",", " ").replace(".", ",")
    return texte


def _pct(valeur):
    return "—" if valeur is None else f"{_nombre(valeur, 1, signe=True)} %"


def _paire(copie, qqq):
    return f"{_pct(copie)} / {_pct(qqq)}" if copie is not None else "—"


def _part(valeur):
    return "—" if valeur is None else f"{valeur * 100:.0f} %"


def _jours(valeur):
    return "—" if valeur is None else f"{valeur:.0f} j"


def _dollars(valeur):
    return "—" if valeur is None else f"{valeur:,.0f} $".replace(",", " ")


def _cellule(texte):
    return str(texte).replace("|", "/").replace("\n", " ")


def _pluriel(nombre, singulier, pluriel):
    return f"{nombre} {singulier if nombre <= 1 else pluriel}"


def _liste_proposee(classement):
    retenus = [ligne for ligne in classement if ligne["retenu"]]
    if not retenus:
        return ["Aucune personne ne passe les critères. Rien à copier pour l'instant."]
    lignes = []
    for ligne in retenus:
        b = ligne["bilan"]
        lignes.append(
            f"- **{ligne['personne']}** ({_FAMILLES.get(ligne['famille'], ligne['famille'])}) : "
            f"{_pluriel(b['achats'], 'achat', 'achats')}, copie {_pct(b['r'])} contre QQQ "
            f"{_pct(b['q'])} (6 mois, sinon 3 mois), QQQ battu dans {_part(b['part_battu'])} des cas.")
    return lignes


def _classement(classement):
    if not classement:
        return ["Aucune personne n'a d'achat retenu sur la fenêtre."]
    ordre = sorted(classement, key=lambda ligne: (
        not ligne["retenu"], -(ligne["bilan"]["part_battu"] if ligne["bilan"]["part_battu"] is not None else -1),
        ligne["personne"]))
    lignes = ["| Personne | Type | Achats | Délai moyen | 3 mois copie / QQQ | 6 mois copie / QQQ | Part battue "
              "| Retenu / motif |",
              "|---|---|---:|---:|---:|---:|---:|---|"]
    for ligne in ordre:
        b = ligne["bilan"]
        lignes.append(
            f"| {_cellule(ligne['personne'])} | {_FAMILLES.get(ligne['famille'], ligne['famille'])} | {b['achats']} "
            f"| {_jours(b['delai_moyen'])} | {_paire(b['r3'], b['q3'])} | {_paire(b['r6'], b['q6'])} "
            f"| {_part(b['part_battu'])} | {'retenu' if ligne['retenu'] else _cellule(ligne['motif'])} |")
    return lignes


def _dirigeants(b):
    if not b or not b.get("achats"):
        return ["Aucun achat de dirigeant ne passe le filtre (code P, au moins 100 000 $, dirigeant ou administrateur, "
                "titre dans l'univers)."]
    verdict = "bat QQQ" if bat_qqq(b) else "ne bat pas QQQ"
    return [
        f"- {_pluriel(b['achats'], 'achat', 'achats')} retenus, délai moyen {_jours(b['delai_moyen'])}.",
        f"- 3 mois : copie {_pct(b['r3'])} / QQQ {_pct(b['q3'])}.",
        f"- 6 mois : copie {_pct(b['r6'])} / QQQ {_pct(b['q6'])}.",
        f"- Part des copies qui battent QQQ : {_part(b['part_battu'])}. Au total, la copie {verdict}.",
        "- Ces achats sont copiés un par un, sans suivre la personne ; les achats « groupés » (au moins deux "
        "dirigeants de la même société en 30 jours) passent en premier quand les places manquent."]


def _portefeuille(resultats):
    if not any(resultats.get(cle) for cle, _ in _SCENARIOS):
        return ["Simulation non disponible."]
    lignes = ["| Scénario | Valeur finale | Rendement | QQQ | Trades | Raisons de sortie |",
              "|---|---:|---:|---:|---:|---|"]
    for cle, titre in _SCENARIOS:
        r = resultats.get(cle)
        if not r:
            continue
        raisons = Counter(t["raison"] for t in r.get("trades", []))
        detail = ", ".join(f"{raison} {n}" for raison, n in raisons.most_common()) or "—"
        lignes.append(f"| {titre} | {_dollars(r.get('final'))} | {_pct(r.get('rendement_pct'))} "
                      f"| {_pct(r.get('qqq_pct'))} | {len(r.get('trades', []))} | {detail} |")
    ignores = Counter(i["motif"] for i in next((resultats[c].get("ignores", []) for c, _ in _SCENARIOS
                                                if resultats.get(c)), []))
    if ignores:
        lignes += ["", "Achats publiés mais non copiés (sans frais) : "
                   + ", ".join(f"{motif} : {n}" for motif, n in ignores.most_common()) + "."]
    lignes += ["", "« ouverte » : position encore détenue au dernier jour, comptée au dernier cours, sans frais de "
               "sortie. QQQ est mesuré de l'ouverture du premier jour à la clôture du dernier."]
    return lignes


def ligne_source(s):
    """Une ligne sur une source de déclarations : lues, illisibles avec leur part en %, ou échec."""
    nom = s["nom"]
    if s.get("etat") == "non lue":
        return f"{nom} non lu ({s.get('detail', 'raison inconnue')})"
    if s.get("etat") == "echec":
        return f"{nom} : échec : {s.get('detail', 'raison inconnue')}"
    texte = f"{nom} : "
    if "lues" in s:
        lues, illisibles = s["lues"], s.get("illisibles", 0)
        singulier, pluriel = s.get("libelle", ["déclaration lue", "déclarations lues"])
        total = lues + illisibles
        part = f" ({_nombre(illisibles / total * 100)} %)" if total else ""
        texte += f"{_pluriel(lues, singulier, pluriel)}, {_pluriel(illisibles, 'illisible', 'illisibles')}{part}"
        if s.get("operations") is not None:
            texte += f", {s['operations']} opérations"
    elif s.get("operations") is not None:
        texte += f"{s['operations']} opérations"
    if s.get("detail"):
        texte += f" ({s['detail']})"
    return texte


def totaux(lecture):
    """(déclarations lues, illisibles) de toutes les sources lues."""
    sources = [s for s in lecture.get("sources", []) if s.get("etat") == "lue"]
    return sum(s.get("lues", 0) for s in sources), sum(s.get("illisibles", 0) for s in sources)


def _donnees(lecture):
    sources = lecture.get("sources", [])
    lignes = [f"- {ligne_source(s)}" for s in sources]
    lues, illisibles = totaux(lecture)
    if lues + illisibles:
        lignes.append(f"- Au total : {lues} lues, {illisibles} illisibles "
                      f"({_nombre(illisibles / (lues + illisibles) * 100)} %).")
    if "personnes_sans_ligne" in lecture:
        lignes.append(f"- {_pluriel(lecture['personnes_sans_ligne'], 'personne', 'personnes')} sans aucun achat retenu "
                      "(fenêtre, univers ou montant) : absentes du classement.")
    if "sans_ticker" in lecture:
        lignes.append(f"- {_pluriel(lecture['sans_ticker'], 'nom de fonds', 'noms de fonds')} sans ticker sûr : "
                      "ignorés.")
    sans_13f = lecture.get("fonds_sans_13f")
    if sans_13f is not None:
        noms = f" ({', '.join(sans_13f)})" if sans_13f else ""
        lignes.append(f"- {_pluriel(len(sans_13f), 'fonds', 'fonds')} sans 13F récent{noms} : le fonds reste dans la "
                      "liste mais ne produit rien.")
    lignes += ["", "**Limites, en mots simples**", "",
               "- La taille des sociétés est calculée avec le nombre d'actions d'aujourd'hui, pas celui de la date de "
               "publication : une société qui a beaucoup émis ou racheté d'actions peut être mal classée.",
               "- Les cours sont ajustés des dividendes et des divisions d'actions : les rendements les incluent, pour "
               "les titres comme pour QQQ.",
               "- Pour les fonds, une nouvelle position est un titre absent du 13F précédent. Un trimestre manquant ou "
               "un changement de CUSIP (le code du titre) peut créer de fausses nouvelles positions. Le 13F paraît "
               "jusqu'à 45 jours après la fin du trimestre : la copie est toujours en retard.",
               "- Un achat de fonds lié à un administrateur est compté comme un achat de dirigeant.",
               "- Un nom de fonds sans ticker sûr (nom absent ou ambigu dans la liste de la SEC) est ignoré et compté "
               "ci-dessus."]
    lignes.append("- Une déclaration illisible (PDF scanné, page absente ou mal formée) est comptée mais pas lue : ses "
                  "opérations manquent.")
    dernier = lecture.get("dernier_trimestre_dirigeants")
    if dernier:
        lignes.append(f"- Les achats de dirigeants publiés après le trimestre {dernier} ne sont pas encore dans les "
                      "jeux de la SEC.")
    if lecture.get("eurusd_absent"):
        lignes.append("- Le cours EURUSD manquait : l'euro est compté pour 1 $ dans les frais.")
    if any(s.get("etat") == "non lue" and s["nom"] == "Sénat" for s in sources):
        lignes.append("- Les élus du Sénat ne sont pas lus : seule la Chambre des représentants compte.")
    return lignes


def ecrire(classement, bilan_dirig, resultats, lecture, date_choix):
    """Texte Markdown du rapport."""
    resultats, lecture = resultats or {}, lecture or {}
    fenetre = lecture.get("fenetre_mois", 18)
    debut = (pd.Timestamp(date_choix) - pd.DateOffset(months=fenetre)).date()
    sections = [
        ("Liste proposée", _liste_proposee(classement)),
        ("Classement complet", _classement(classement)),
        ("Dirigeants (filtre par achat)", _dirigeants(bilan_dirig)),
        ("Portefeuille au fil de l'eau (oct. 2025 → oct. 2026)", _portefeuille(resultats)),
        ("Données", _donnees(lecture)),
    ]
    lignes = [
        "# Copie d'initiés : test sur le passé", "",
        f"Liste choisie au {date_choix.strftime('%d/%m/%Y')}, sur la fenêtre du {debut.strftime('%d/%m/%Y')} au "
        f"{date_choix.strftime('%d/%m/%Y')} ({fenetre} mois). Chaque copie est mesurée en achetant à l'ouverture de la "
        "séance qui suit la publication de la déclaration, et comparée à QQQ aux mêmes dates.", ""]
    for titre, contenu in sections:
        lignes += [f"## {titre}", "", *contenu, ""]
    lignes.append(AVERTISSEMENT)
    return "\n".join(lignes) + "\n"
