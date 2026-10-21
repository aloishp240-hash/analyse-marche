"""Préréglages nommés de la stratégie court terme.

- amelioree : la section court_terme de config.json (réglages actifs, avec frais et règles prudentes) ;
- reference : la stratégie telle qu'au commit 04973f7, figée ici pour toujours pouvoir la comparer
  (sans frais, exécution au simple contact, pas de règle d'annulation, 2 % de risque).
"""
REFERENCE = {
    "capital_initial": 3000,
    "risque_pct": 2,
    "plafond_position_pct": 40,
    "decimales_quantite": 2,
    "positions_max": 5,
    "par_industrie_max": 2,
    "candidats_max": 5,
    "configurations": {"A": True, "B": False},
    "rsi_a_min": 35,
    "rsi_a_max": 45,
    "proximite_mm_atr": 1,
    "cassure_seances": 20,
    "volume_cassure": 1.5,
    "rsi_b_max": 75,
    "seances_sans_resultats": 10,
    "variation_max_atr": 2,
    "entree_atr": 0.25,
    "zone_entree_atr": 0.25,
    "stop_atr": 1.5,
    "objectif_atr": 3,
    "remontee_stop_atr": 1.5,
    "validite_ordre_seances": 3,
    "stop_temporel_seances": 15,
    # Règles ajoutées après le commit 04973f7, neutralisées
    "frais_fixes_eur": 0,
    "frais_pct": 0,
    "glissement_pct": 0,
    "marge_limite_atr": None,
    "jour_entree_prudent": False,
    "annulation_gap_atr": None,
    "annulation_sous_mm50": False,
    "annulation_resultats": False,
    "exclure_resultats_inconnus": False,
    "jours_feries": False,
    "risque_cumule_max_pct": None,
    "marge_gap_atr": 0,
    "suspension_baisse_pct": None,
    "fermetures_max_session": None,
}

NOMS = ("amelioree", "reference")


def parametres(config, nom="amelioree"):
    """Paramètres du préréglage `nom` ; `config` est le contenu de config.json."""
    if nom == "reference":
        return {**REFERENCE, "configurations": dict(REFERENCE["configurations"])}
    if nom == "amelioree":
        return {**REFERENCE, **config["court_terme"]}
    raise ValueError(f"préréglage inconnu : {nom}")

# Registre des versions : ce qui a changé, sur quelles périodes la version a été conçue puis contrôlée, ce qu'elle a
# donné et ses limites. Les paramètres exacts se lisent avec parametres(config, preglage) et sont recopiés dans
# experiences/registre.json à chaque backtest enregistré (backtest.py --enregistrer). La référence ne change jamais.
VERSIONS = [
    {
        "id": "reference-04973f7", "date": "2026-10-20", "preglage": "reference",
        "modifications": "stratégie d'origine (repli en tendance, configuration A) telle qu'au commit 04973f7",
        "conception": "règles écrites à la main, sans réglage sur données",
        "validation": "aucune avant mise en service ; backtests du 2026-10-21 sur 2023-11 → 2026-10 et 2018 → 2022",
        "metriques": "3 ans : 97 trades, 35,1 % gagnants, +0,22 R par trade, +33,1 % (QQQ +97,5 %) ; "
                     "2018-2022 : 121 trades, +0,18 R, +39,5 % (QQQ +71,0 %) ; sans aucun frais. Avec les frais de "
                     "v2 (--memes-frais), 3 ans : 97 trades, +0,12 R, +15,6 %, facteur de profit 1,18",
        "limites": "sans frais ni glissement ; achat au simple contact du prix limite ; objectif possible le jour "
                   "de l'achat ; titres actuels du Nasdaq 100 appliqués au passé (biais du survivant)",
    },
    {
        "id": "v2-2026-10-21", "date": "2026-10-21", "preglage": "amelioree",
        "modifications": "frais (1 € + 0,1 %) et glissement (0,05 %) ; risque 1 % par trade ; exécution prudente "
                         "(marge sous la limite, pas d'objectif le jour de l'achat) ; annulations (gap, MM50, "
                         "résultats) ; plafond de risque cumulé 4 % ; jours fériés ; suspension après -10 %",
        "conception": "règles de prudence choisies a priori, sans optimisation ; contrôle sur 2023-11 → 2026-10",
        "validation": "2018-01 → 2022-12, période non utilisée pour la conception",
        "metriques": "3 ans : 87 trades, 33,3 % gagnants, -0,02 R par trade, -3,8 % (QQQ +97,5 %), frais 276 € ; "
                     "2018-2022 : 124 trades, -0,23 R, -19,3 % (QQQ +71,0 %)",
        "limites": "après frais réalistes, aucune avance mesurable sur ces deux périodes ; dates de résultats Yahoo "
                   "approximatives ; biais du survivant ; Claude non compris dans le backtest",
    },
]
