"""Opération déclarée par une personne copiée (élu, dirigeant ou fonds), commune aux trois sources."""
import re
from dataclasses import asdict, dataclass, field
from datetime import date


@dataclass(frozen=True)
class Operation:
    famille: str  # "politicien", "dirigeant" ou "fonds"
    personne_id: str
    personne: str
    ticker: str
    sens: str  # "achat" ou "vente"
    date_operation: date
    date_publication: date
    montant_min: float
    montant_max: float
    source_id: str
    details: dict = field(default_factory=dict)


_PREFIXE_BOURSE = re.compile(r"(?:NYSE|NASDAQ)\s*:\s*")
_TICKER_VALIDE = re.compile(r"[A-Z]{1,5}(?:[.-][A-Z]{1,2})?")


def nettoyer_ticker(texte):
    """Ticker Yahoo plausible (majuscules, sans espaces autour ni préfixe NYSE:/NASDAQ:), ou None s'il est illisible :
    crochets, plusieurs titres, espaces au milieu, « N.A. »... On ne devine jamais parmi plusieurs candidats."""
    if not isinstance(texte, str):
        return None
    texte = _PREFIXE_BOURSE.sub("", texte.strip().upper(), count=1)
    return texte if _TICKER_VALIDE.fullmatch(texte) else None


def delai(op):
    """Jours calendaires entre l'opération et sa publication."""
    return (op.date_publication - op.date_operation).days


def vers_dict(op):
    d = asdict(op)
    d["date_operation"] = op.date_operation.isoformat()
    d["date_publication"] = op.date_publication.isoformat()
    return d


def depuis_dict(d):
    d = dict(d)
    d["date_operation"] = date.fromisoformat(d["date_operation"])
    d["date_publication"] = date.fromisoformat(d["date_publication"])
    return Operation(**d)
