#!/usr/bin/env python3
"""Notes de l'agent d'une session à l'autre (200 lignes au plus, les plus anciennes partent d'abord).

Usage : python outils/notes.py lire
        python outils/notes.py ajouter "texte"
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

if __name__ == "__main__":  # la racine du dépôt remplace outils/, où signal.py masquerait le module standard
    sys.path[0] = str(Path(__file__).resolve().parents[1])

from commun import NY

NOTES = Path(__file__).resolve().parents[1] / "agent" / "notes.md"
TITRE = "# Notes de l'agent court terme\n"
LIGNES_MAX = 200


def aujourd_hui():
    return datetime.now(NY).date()


def tronquer(texte):
    """Supprime les sections datées les plus anciennes jusqu'à tenir en LIGNES_MAX lignes."""
    lignes = texte.splitlines()
    debuts = [i for i, ligne in enumerate(lignes) if ligne.startswith("## ")]
    if not debuts:
        return texte
    entete = lignes[:debuts[0]]
    sections = [lignes[a:b] for a, b in zip(debuts, debuts[1:] + [len(lignes)])]
    while len(sections) > 1 and len(entete) + sum(map(len, sections)) > LIGNES_MAX:
        sections.pop(0)
    return "\n".join(entete + [ligne for s in sections for ligne in s]) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("lire")
    actions.add_parser("ajouter").add_argument("texte")
    args = parser.parse_args(argv)
    if args.action == "lire":
        print(NOTES.read_text() if NOTES.exists() else "(aucune note)")
        return 0
    texte = NOTES.read_text() if NOTES.exists() else TITRE
    texte = texte.rstrip("\n") + f"\n\n## {aujourd_hui().isoformat()}\n{args.texte.strip()}\n"
    NOTES.write_text(tronquer(texte))
    print("note ajoutée")
    return 0


if __name__ == "__main__":
    main()
