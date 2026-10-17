# Analyse marché — Nasdaq 100

- `surveillance.py` : scan toutes les 5 min pendant la séance US (lancé par launchd), alertes Telegram (dont mouvements groupés par industrie), puis bilan de clôture entre 16h00 et 16h30 New York. Ne consomme aucun token Claude.
- `fiche.py TICKER ...` : fiche compacte (cours, force relative, MM, RSI, MACD, ATR, Bollinger, pivots, volume, valorisation, dette, croissance, analystes, résultats, actualités).
- `.claude/agents/analyste-marche.md` : agent Claude (Haiku) qui explique les alertes, à la demande.
- `config.json` : seuils des alertes (variations exprimées en multiples de l'ATR, avec un minimum en %). `tickers.txt` : composition, rafraîchie tous les 30 jours.
- `alertes/AAAA-MM-JJ.jsonl` : journal des alertes envoyées. `logs/surveillance.log` : erreurs.

Commandes utiles :

    .venv/bin/python surveillance.py --force --dry-run   # tester sans envoyer
    .venv/bin/python surveillance.py --bilan --dry-run   # voir le bilan de clôture
    .venv/bin/python telegram_config.py                  # configurer et tester Telegram
    ./installer.sh                                       # activer le scan automatique
    ./installer.sh retirer                               # le désactiver

Le Mac doit être allumé et éveillé entre 15h30 et 22h30 (heure de Paris) pour recevoir les alertes.

## Relais GitHub (Mac éteint)

Le workflow `.github/workflows/surveillance.yml` tourne toutes les 5 min en semaine sur GitHub. Il envoie le calendrier des résultats et le bilan de clôture, et prend le relais des alertes de séance si le Mac ne s'est pas manifesté depuis 15 min. Le Mac signale sa présence via `gh variable set MAC_VU`.
Configuration : `GITHUB_DEPOT=compte/depot` dans `.env`, secrets `TELEGRAM_TOKEN` et `TELEGRAM_CHAT_ID` dans le dépôt. Sans `GITHUB_DEPOT`, le Mac fait tout seul. GitHub suspend les tâches planifiées après 60 jours sans commit.
