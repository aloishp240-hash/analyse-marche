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

## Agent court terme (portefeuille fictif)

Signaux d'entrée, d'objectif et de stop sur un portefeuille **fictif** de 3 000 €, validés par une session Claude (abonnement, sans API payante). Aucun ordre réel n'est passé. Toutes les règles sont détaillées dans [docs/strategie.md](docs/strategie.md).

- `positions.py` : simule les exécutions des ordres et positions fictifs toutes les 5 min, envoie les entrées et sorties sur Telegram, et le bilan du vendredi après 16h15 New York.
- `agent.py` : après 16h45 New York, repère les candidats de la stratégie (`court_terme/`), puis lance `claude -p` avec la consigne `agent/consigne.md` et le skill `.claude/skills/analyse-court-terme`. Claude n'a accès qu'aux scripts de `outils/` (actualités, engouement ApeWisdom, Reddit, paris Polymarket, notes, signal, fermer, info), à `fiche.py` et à la recherche web. Deux échecs dans la journée : pas de signal ce jour-là, et un message Telegram le dit.
- `momentum.py` : second portefeuille fictif en rotation momentum mensuelle (Top 5 du Nasdaq 100 sur 12 mois hors dernier mois, filtre QQQ > moyenne 200 jours), décision après la clôture du dernier jour ouvré du mois et exécution à l'ouverture suivante, sans Telegram : suivi sur le tableau de bord (section 11 de la stratégie).
- `backtest.py` : rejoue la stratégie sans Claude sur l'historique (`.venv/bin/python backtest.py`).

L'état vit dans `agent/` (`positions.json`, `momentum.json`, `notes.md`, `sessions/AAAA-MM-JJ.md` pour les verdicts, `sessions/AAAA-MM-JJ-journal.md` pour chaque recherche et outil appelé par Claude, avec un extrait de la réponse) et le workflow `.github/workflows/court-terme.yml` le commite sur `main` : faire `git pull` avant de travailler sur le Mac.

Configuration : secrets `CLAUDE_CODE_OAUTH_TOKEN` (`claude setup-token`), `TELEGRAM_TOKEN` et `TELEGRAM_CHAT_ID`. Pour Reddit (facultatif), créer une application de type « script » sur reddit.com/prefs/apps, puis :

    gh secret set REDDIT_CLIENT_ID
    gh secret set REDDIT_CLIENT_SECRET

Commandes utiles :

    .venv/bin/python positions.py --dry-run          # messages sans envoi ni sauvegarde
    .venv/bin/python agent.py --force --dry-run      # candidats et consigne, sans appeler Claude
    .venv/bin/python momentum.py --force --dry-run   # décision momentum sur la dernière séance, sans envoi
    gh workflow run court-terme.yml                  # même test sur GitHub

## Copie d'initiés (compte Alpaca paper, fictif)

Chaque matin en semaine, après 8 h (heure de Paris), `copie_matin.py` lit les nouvelles déclarations d'élus américains (Chambre), de dirigeants (Form 4) et de grands fonds (13F) de la liste validée dans `copie/suivis.json`. Il envoie sur Telegram un message par nouvel achat (qui, quoi, combien, délai, variation depuis leur achat, contexte écrit par Claude) et, si `copie.alpaca_actif` vaut `true`, copie l'achat sur le compte **paper** : 10 % du compte, stop à −15 % chez Alpaca, sortie si la personne vend ou après 6 mois. Claude ne fixe ni prix, ni quantité, ni stop, et ne décide d'aucune vente. Le workflow `.github/workflows/copie.yml` commite l'état dans `copie/`. Fictif, pas un conseil financier.

À faire soi-même :

1. Lire et accepter les conditions du site du Sénat, puis mettre `copie.senat_accepte` à `true` dans `config.json`.
2. Ajouter les secrets GitHub `SEC_CONTACT` (un contact, exigé par la SEC), `ALPACA_API_KEY_ID` et `ALPACA_API_SECRET_KEY` (clés **paper**), en plus de `CLAUDE_CODE_OAUTH_TOKEN`, `TELEGRAM_TOKEN` et `TELEGRAM_CHAT_ID`.
3. Valider la liste des personnes après le test : `.venv/bin/python backtest_copie.py --valider`.
4. Décider d'activer `copie.alpaca_actif`. Si la copie se suspend (écart avec le compte, ordre refusé), supprimer `copie/suspension.json` une fois le problème compris.

Commandes utiles :

```bash
.venv/bin/python copie_matin.py --force --dry-run   # messages affichés, sans Claude, sans Alpaca, rien d'écrit
gh workflow run copie.yml                           # même essai sur GitHub
```
