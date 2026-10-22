"""Description des achats par Claude : parti et commission d'un élu, société, actualités, pistes.

Claude cherche sur le web (texte lu = donnée, jamais instruction) et n'écrit que du texte : il ne fixe aucun prix ni
aucune quantité, ne touche à aucun stop et ne décide d'aucune vente. Sa réponse est relue par le code : seules les
clés attendues, des textes nettoyés et bornés, au plus 3 pistes. Tout échec donne {}, et le message part sans
description.
"""
import json
import subprocess
from pathlib import Path

import agent
from commun import charger_config
from inities.messages import PISTES_MAX, nettoyer_champ
from inities.modele import vers_dict

CONSIGNE = Path(__file__).resolve().parent.parent / "copie" / "consigne.md"
CHAMPS = ("contexte", "societe", "actualites")
PREFIXES_RETIRES = ("ALPACA_", "REDDIT_")
RETIRES = ("SEC_CONTACT",)


def commande(consigne):
    return ["claude", "-p", consigne, "--model", "sonnet", "--max-turns", "20", "--output-format", "stream-json",
            "--verbose", "--allowedTools", "Bash(python outils/actualites.py:*)", "Bash(python fiche.py:*)",
            "WebSearch", "--disallowedTools", *agent.INTERDITS]


def environnement():
    """Environnement de claude : celui de l'agent court terme, sans les clés Alpaca, Reddit ni le contact SEC.

    agent.SECRETS ne les retire pas, alors que Claude n'en a aucun besoin ici (il garde CLAUDE_CODE_OAUTH_TOKEN).
    """
    return {k: v for k, v in agent.environnement_claude().items()
            if not k.startswith(PREFIXES_RETIRES) and k not in RETIRES}


def consigne(achats):
    return (CONSIGNE.read_text().rstrip() + "\n\n## Achats à décrire\n\n"
            + json.dumps([vers_dict(op) for op in achats], ensure_ascii=False) + "\n")


def _dernier_json(texte):
    """Dernier objet JSON de premier niveau du texte, ou None."""
    decodeur, dernier, i = json.JSONDecoder(), None, 0
    while (i := texte.find("{", i)) != -1:
        try:
            objet, fin = decodeur.raw_decode(texte, i)
        except ValueError:
            i += 1
            continue
        dernier, i = objet, fin
    return dernier


def _description(brut):
    if not isinstance(brut, dict):
        return {}
    description = {}
    for cle in CHAMPS:
        valeur = brut.get(cle)
        valeur = nettoyer_champ(valeur) if isinstance(valeur, str) else ""
        if valeur:
            description[cle] = valeur
    pistes = brut.get("pistes")
    if isinstance(pistes, list):
        pistes = [nettoyer_champ(p, une_ligne=True) for p in pistes if isinstance(p, str)]
        description["pistes"] = [p for p in pistes if p][:PISTES_MAX]
    return description


def decrire(achats, duree_max_s=900):
    """{source_id: description} pour les premiers achats (copie.descriptions_max), {} en cas d'échec."""
    try:
        achats = list(achats)[:charger_config()["copie"].get("descriptions_max", 5)]
        if not achats:
            return {}
        resultat = subprocess.run(commande(consigne(achats)), timeout=duree_max_s, capture_output=True, text=True,
                                  env=environnement())
        if resultat.returncode != 0:
            return {}
        final, _, _ = agent.lire_flux(resultat.stdout)
        reponse = _dernier_json(final) if isinstance(final, str) else None
        if not isinstance(reponse, dict):
            return {}
        identifiants = {op.source_id for op in achats}
        descriptions = {i: _description(brut) for i, brut in reponse.items() if i in identifiants}
        return {i: d for i, d in descriptions.items() if d}
    except Exception:
        return {}
