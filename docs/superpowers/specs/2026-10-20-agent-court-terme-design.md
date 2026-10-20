# Agent d'analyse court terme — conception

## Intention

Ajouter au scanner Nasdaq 100 existant un agent qui propose des positions court terme (quelques jours à quelques semaines), chacune avec trois prix fixés dès l'ouverture : entrée, objectif de revente (take-profit) et stop-loss. Inspiré de la vidéo d'Aywen « J'ai laissé 4 IA investir en bourse » : mêmes familles d'outils, mais en corrigeant ses erreurs (stop déplacé par l'IA, dates mal calculées, allers-retours sans raison, exploration coûteuse).

L'agent tient un **portefeuille fictif** de 3 000 €. Il ne passe aucun ordre réel et n'a aucun accès au compte de courtage de l'utilisateur. L'utilisateur seul décide s'il reproduit un signal.

## Objectifs

- Repérer chaque jour des candidats selon des règles écrites, les faire valider par Claude, envoyer les signaux sur Telegram.
- Suivre automatiquement chaque position (entrée exécutée, stop, objectif, stop temporel) et prévenir sur Telegram.
- Mesurer honnêtement la stratégie : backtest d'abord, puis 1 à 2 mois en portefeuille fictif, avec un bilan hebdomadaire comparé au simple fait de détenir l'indice.

## Non-objectifs

- Passer des ordres chez un courtier.
- Enregistrer les vraies positions de l'utilisateur.
- Vente à découvert, options, levier.
- Univers au-delà du Nasdaq 100 (pour l'instant).

## Anti-objectifs (échec même si « ça marche »)

- **Claude fixe ou modifie un prix.** Les niveaux viennent uniquement du code.
- **Un stop-loss descend.** Il ne peut que monter.
- **Une date est calculée par le modèle.** Dates de résultats, durées, échéances : toujours par le code.
- **L'agent épuise les limites Pro de l'utilisateur.** Il partage le quota de ses conversations interactives.
- **Un secret apparaît dans un fichier ou un log.**

## Contraintes

- **Exécution :** GitHub Actions, dépôt public `aloishp240-hash/analyse-marche` (minutes illimitées). Tout ce qui est écrit dans le dépôt et dans les logs est public, ce qui est acceptable car le portefeuille est fictif.
- **Claude :** Claude Code en mode non interactif (`claude -p`), authentifié par l'abonnement Pro via le secret `CLAUDE_CODE_OAUTH_TOKEN` (déjà enregistré). Modèle Sonnet. Aucune API payante.
- **Coût :** zéro euro en plus de l'abonnement Pro. Toutes les sources de données sont gratuites.
- **Code existant à réutiliser :** `commun.py` (historique, intraday, RSI, ATR, MACD, résultats, fondamentaux, `envoyer_telegram`), `fiche.py` (fiche compacte + actualités Yahoo RSS), `config.json` pour les seuils.
- **Mac :** le scanner tourne aussi sur le Mac (launchd). L'agent et le suivi des positions tournent **uniquement sur GitHub**, pour n'avoir qu'une seule source de vérité.

## Stratégie

### Repérage (code Python, sans Claude)

Univers : `tickers.txt` (Nasdaq 100).

- **Configuration A — repli dans une tendance haussière** (active)
  - Clôture > MM50 > MM200.
  - Performance sur 3 mois supérieure à celle de l'indice.
  - RSI(14) entre 35 et 45.
  - Clôture à moins de 1 ATR de la MM20 ou de la MM50.
- **Configuration B — cassure** (codée, désactivée dans `config.json`)
  - Clôture au-dessus du plus haut des 20 séances précédentes.
  - Volume ≥ 1,5 × la moyenne sur 20 jours.
  - RSI < 75.

**Exclusions :**
- résultats publiés dans les 10 prochaines séances ;
- variation du jour > 2 ATR ;
- titre déjà en position ou en ordre d'entrée.

**Classement :** les 5 meilleurs candidats (critère de tri laissé au développeur, à documenter) passent à Claude.

### Niveaux (code)

- **Entrée :** ordre limite = clôture du jour du signal − 0,25 ATR(14), valable 3 séances, annulé ensuite.
- **Stop-loss :** entrée − 1,5 ATR. Il remonte au prix d'entrée quand le plus haut atteint entrée + 1,5 ATR. Il ne descend jamais.
- **Take-profit :** entrée + 3 ATR.
- **Stop temporel :** vente à la clôture de la 15e séance si ni le stop ni l'objectif n'a été touché.

### Taille et limites

- **Risque par position :** 1 % du capital courant (30 € au départ). Quantité = risque / (entrée − stop), arrondie à l'action entière inférieure.
- **Plafond par position :** 20 % du capital courant (600 € au départ).
- **Plafonds globaux :** 5 positions ouvertes (ordres en attente compris), 2 par industrie au maximum (industries déjà utilisées par le scanner).
- **Si le plafond est atteint,** on n'ajoute rien, même si Claude valide.
- **Capital courant :** 3 000 € + gains et pertes réalisés. Le montant engagé (positions + ordres en attente, au prix d'entrée) ne dépasse jamais ce capital.

### Simulation des exécutions (portefeuille fictif)

- **Entrée exécutée** si le plus bas de la séance ≤ prix limite. Prix d'exécution : la limite, ou l'ouverture si elle est déjà sous la limite.
- **Sortie à l'ouverture** si le cours ouvre au-delà du stop ou de l'objectif.
- **Stop et objectif touchés la même séance :** on retient le stop (hypothèse prudente).
- **Frais :** zéro.

## L'agent Claude

**Moment :** une session par jour de bourse, après la clôture et le bilan du scanner (vers 16h45 heure de New York, heure d'été comme d'hiver).

**Entrée :** les candidats du jour avec leurs niveaux calculés, l'état du portefeuille fictif et le fichier de notes.

**Mission :**
- Pour chaque candidat, chercher une raison de **ne pas** y aller : mauvaise nouvelle, procès, révision à la baisse, opération en cours, engouement suspect sur les réseaux.
- Rendre un verdict **validé / rejeté** avec 2 à 3 phrases de justification.
- Pour chaque position ouverte, signaler une nouvelle importante. Le message est purement informatif : il ne ferme pas la position.
- Mettre à jour ses notes.

**Outils.** Chacun est un script Python accompagné d'une skill qui explique quand et comment s'en servir :

| Outil | Source |
|---|---|
| Actualités | RSS Yahoo Finance (déjà dans `fiche.py`) + Google Actualités par titre |
| Screener | le repérage ci-dessus, en lecture : liste des candidats et pourquoi |
| Bourse | `fiche.py` (cours, techniques, valorisation, dette, croissance, analystes, résultats) |
| Recherche web | outils de recherche et de lecture de page intégrés à Claude Code |
| Engouement | API ApeWisdom (mentions et variation sur 24 h, gratuit, sans clé ; testé le 2026-10-20) |
| Reddit | API officielle Reddit (secrets `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` à créer) |
| Notes | lecture / ajout dans `agent/notes.md` |
| Signal | enregistre un candidat validé, avec `ticker`, `configuration` et `justification` uniquement. Le code calcule les niveaux, vérifie les plafonds, écrit le journal et envoie le message Telegram. |

**Garde-fous :**
- Claude n'a accès qu'à ces outils : pas de shell libre, pas de git.
- Budget d'environ 10 appels d'outils par session (lecture des notes et signaux non compris), et nombre de tours limité.
- Le contenu lu sur le web, Reddit ou dans les actualités est une donnée, jamais une instruction : à rappeler dans la consigne.
- Si Claude échoue (quota Pro atteint, erreur), aucun signal n'est envoyé ce jour-là et un message Telegram le dit.

**Notes :**
- **Contenu :** dates clés, raisons des choix, erreurs à ne pas refaire.
- **Taille bornée** pour ne pas alourdir chaque session. Le développeur fixe la limite et la méthode de résumé.

## Suivi et Telegram (code)

- **Fréquence :** le suivi des positions tourne toutes les 5 minutes pendant la séance, sur GitHub, dans un workflow distinct du scanner.
- **Messages :** signal (titre, configuration, entrée, objectif, stop, quantité, risque en €, validité, verdict de Claude), entrée exécutée, ordre expiré, stop touché, objectif touché, stop remonté au prix d'entrée, stop temporel, échec de session.
- **Bilan hebdomadaire** (vendredi après la clôture) :
  - nombre de positions ;
  - taux de réussite, gain moyen, perte moyenne, espérance par position ;
  - valeur du portefeuille fictif ;
  - comparaison avec l'indice sur la même période.

## Fichiers et état

Dans le dépôt, commités par le workflow **seulement quand quelque chose change** (permission `contents: write`) :

- `agent/positions.json` : ordres en attente, positions ouvertes, historique des positions fermées, capital.
- `agent/notes.md` : mémoire de l'agent.
- `agent/sessions/AAAA-MM-JJ.md` : compte rendu de la session de Claude (candidats, verdicts, sources).

Les caches de données restent dans `actions/cache`, comme aujourd'hui.

Effet secondaire utile : ces commits empêchent GitHub de suspendre les tâches planifiées après 60 jours sans commit.

## Mise en route

1. **Backtest :**
   - Script qui applique repérage, niveaux et simulation sur 2 à 3 ans d'historique du Nasdaq 100, sans Claude.
   - Il produit les statistiques du bilan hebdomadaire.
   - Biais à documenter : la composition actuelle de l'indice est appliquée au passé, et les dates de résultats historiques sont approximatives.
2. **Portefeuille fictif :** 1 à 2 mois avec Claude, en conditions réelles.
3. **Décision :** l'utilisateur seul choisit ce qu'il fait des signaux ensuite.

## Laissé au développeur

- Structure exacte des scripts et des skills, et façon de lancer Claude Code dans le workflow : installation, options, outils autorisés.
- Critère de classement des candidats.
- Méthode pour borner les notes.
- Format exact des messages Telegram, dans l'esprit des alertes existantes.

## Risques connus

- **L'accès à l'API Reddit** peut demander une validation de Reddit. Si elle n'est pas accordée, l'agent se contente d'ApeWisdom.
- **Les limites Pro sont partagées.** Une session lourde un jour d'usage intensif peut échouer : c'est prévu dans les garde-fous.
- **Les gains d'un mois fictif ne prouvent rien.** C'est la conclusion de la vidéo elle-même. Le backtest et l'espérance par position comptent davantage.
