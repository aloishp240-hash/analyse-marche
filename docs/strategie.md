# Stratégie de l'agent court terme

Ce document décrit tout ce que fait l'agent, du repérage des titres à la vente. Chaque réglage chiffré indique son
nom dans `config.json` (section `court_terme`) : pour changer une règle, il suffit le plus souvent de changer ce
nombre.

Tout est **fictif** : aucun ordre n'est passé chez un courtier. L'agent tient un portefeuille simulé et t'envoie ses
décisions sur Telegram ; c'est toi qui choisis de les suivre ou non.

---

## 1. Vue d'ensemble

| Étape | Qui | Quand (heure de Paris) |
|---|---|---|
| Repérage des candidats et calcul des niveaux | le code (Python), sans Claude | après la clôture, vers 22h45 |
| Validation ou rejet de chaque candidat, revue des titres suivis | Claude (modèle Sonnet) | juste après, ~20 min max |
| Envoi des signaux et décisions sur Telegram | le code | à la fin du passage, une fois l'état enregistré |
| Suivi des ordres et positions (entrée, stop, objectif) | le code | toutes les 5 min pendant la séance, 15h30-22h |
| Bilan de la semaine | le code | le vendredi, vers 22h15 |

Une **séance** est une journée de bourse à New York : 9h30-16h là-bas, soit 15h30-22h à Paris. Les week-ends et les
jours fériés américains ne comptent pas.

Le tout tourne sur GitHub Actions toutes les 5 minutes, du lundi au vendredi.

---

## 2. Portefeuille

| Règle | Valeur | Réglage |
|---|---|---|
| Capital de départ | 3 000 € | `capital_initial` |
| Capital courant | 3 000 € + gains − pertes des positions fermées | — |
| Titres suivis en même temps (ordres + positions) | 5 au maximum | `positions_max` |
| Titres d'une même industrie | 2 au maximum | `par_industrie_max` |
| Montant total engagé | jamais plus que le capital courant | — |
| Risque total (pertes possibles de tous les ordres et positions, frais et marge de gap compris) | 4 % du capital au maximum | `risque_cumule_max_pct`, `marge_gap_atr` |

La **marge de gap** ajoute 0,5 ATR à la perte prévue de chaque titre : un titre peut ouvrir sous son stop après
une mauvaise nouvelle, et la vente se fait alors plus bas que prévu. Avec 1 % de risque par trade, le plafond de 4 %
laisse en pratique 3 titres suivis en même temps.

Les titres sont cotés en dollars ; le portefeuille est compté en euros au taux EUR/USD de la dernière clôture.

---

## 3. Univers

Les titres du **Nasdaq 100** (`tickers.txt`, rafraîchi automatiquement). QQQ, le fonds qui suit cet indice, sert de
référence.

---

## 4. Repérage des candidats (le code)

Chaque soir, le code passe en revue les 100 titres et garde ceux qui remplissent **toutes** les conditions d'une
configuration.

### Configuration A — repli dans une tendance haussière (active)

Idée : acheter un bon titre qui monte depuis des mois, au moment où il fait une petite pause.

| Condition | Valeur | Réglage |
|---|---|---|
| Tendance haussière | clôture > moyenne 50 jours > moyenne 200 jours | — |
| Plus fort que le marché | performance sur 3 mois supérieure à celle de QQQ | — |
| Pause, sans effondrement | RSI 14 jours entre 35 et 45 | `rsi_a_min`, `rsi_a_max` |
| Proche d'un support | clôture à moins de 1 ATR de la moyenne 20 ou 50 jours | `proximite_mm_atr` |

### Configuration B — cassure (codée, désactivée)

Idée : acheter un titre qui sort par le haut de sa fourchette récente avec du volume.
Activable avec `"B": true` dans `configurations`.

| Condition | Valeur | Réglage |
|---|---|---|
| Cassure | clôture au-dessus du plus haut des 20 séances précédentes | `cassure_seances` |
| Avec du volume | volume ≥ 1,5 × la moyenne des 20 séances | `volume_cassure` |
| Pas trop tendu | RSI < 75 | `rsi_b_max` |

### Exclusions

- résultats trimestriels publiés dans les **18 prochaines séances** (`seances_sans_resultats`) : 3 séances
  d'ordre plus 15 séances de position au maximum, pour qu'un trade ne traverse jamais une publication ;
- date des prochains résultats **inconnue** (`exclure_resultats_inconnus`) : une date introuvable ne prouve pas
  qu'il n'y a pas de publication pendant le trade ;
- données du titre **pas à jour** (dernière clôture qui n'est pas celle du jour) ;
- mouvement du jour supérieur à **2 ATR** (`variation_max_atr`) : trop violent pour être une simple pause ;
- titre déjà en ordre ou en position.

### Classement

Les candidats sont triés par **force relative** (performance sur 3 mois du titre moins celle de QQQ). Les
**5 premiers** passent à Claude (`candidats_max`).

> **ATR** (Average True Range) : l'amplitude moyenne d'une séance sur 14 jours, en dollars. C'est l'unité de mesure de
> tous les niveaux : un titre qui bouge beaucoup a un stop plus large qu'un titre calme.

---

## 5. Niveaux de chaque trade (le code)

Calculés à partir de la clôture du jour du signal. Exemple avec une clôture à 183,65 $ et un ATR de 5 $ :

| Niveau | Formule | Exemple | Réglage |
|---|---|---|---|
| Entrée (prix visé) | clôture − 0,25 ATR | 182,40 $ | `entree_atr` |
| Zone d'achat affichée | entrée ± 0,25 ATR | 181 – 184 $ | `zone_entree_atr` |
| Stop | entrée − 1,5 ATR | 174,90 $ | `stop_atr` |
| Objectif | entrée + 3 ATR | 197,40 $ | `objectif_atr` |

Le gain visé vaut donc **2 fois le risque** (3 ATR contre 1,5 ATR).

### Quantité

Le code prend le plus petit de ces deux nombres, arrondi au centième d'action inférieur (`decimales_quantite`) :

1. le nombre d'actions qui fait perdre **1 % du capital** (30 €) si le stop est touché (`risque_pct`), **frais
   compris** : 2 € de frais fixes (achat et vente) sont retirés des 30 €, et chaque action coûte l'écart jusqu'au
   stop plus 0,1 % de frais sur l'achat et sur la vente, plus le glissement de la vente ;
2. le nombre d'actions qui tient dans **40 % du capital** (1 200 €) (`plafond_position_pct`).

Dans l'exemple (en comptant 1 € = 1 $ pour simplifier) : (30 − 2) € ÷ 7,94 $ par action = 3,52 actions, contre
6,57 actions pour 1 200 €. Retenu : **3,52 actions** (environ 640 €), pour une perte d'environ 30 € si le stop est
touché.

Les frais pèsent lourd sur des positions aussi petites : dans le backtest 2023-2026, ils ont coûté en moyenne
**3,18 € par trade** (276,30 € pour 87 trades), soit environ 0,1 R quand le risque prévu est de 30 €. C'est pour cela
que la quantité les retire du risque, au lieu de les découvrir après coup.

---

## 6. Le rôle de Claude

Claude ne calcule et ne propose **aucun prix** : il décide seulement oui ou non, avec des outils en lecture seule
(actualités Yahoo et Google, fiche boursière, recherche web, engouement Reddit, discussions Reddit, paris Polymarket)
et une mémoire de notes d'une session à l'autre. Il a environ 10 recherches par session.

### Valider ou rejeter un candidat

- Il **cherche des raisons de rejeter** ; dans le doute, il rejette.
- Il rejette un **« couteau qui tombe »** : repli causé par une mauvaise nouvelle de fond (prévisions abaissées,
  enquête, perte d'un client majeur, dégradations en série), chute qui s'accélère, secteur entier qui décroche.
- Un repli technique **sans nouvelle**, dans une tendance intacte, est exactement ce qu'on cherche.
- Un engouement Reddit qui explose sans nouvelle, ou un pari Polymarket qui bascule contre le titre, sont des raisons
  de prudence.
- Chaque achat est justifié en quatre rubriques : **Technique, Actualités, Contexte, Risque**.

### Revoir les titres déjà suivis

À chaque session, Claude relit la thèse d'achat de chaque ordre et de chaque position et la confronte aux nouvelles.
Il **ferme** le titre dans deux cas seulement :

1. **la thèse est cassée** : nouvelle de fond négative depuis l'achat, résultats avancés, secteur qui décroche,
   catalyseur disparu ;
2. **la place manque pour un meilleur dossier** : un candidat du jour est nettement plus solide, mais un plafond
   (5 titres, 2 par industrie, capital) bloque son signal. Il ferme alors le titre suivi le plus faible.

Il ne ferme jamais un titre seulement parce qu'il recule un peu : c'est le rôle du stop.

Fermer un **ordre** pas encore exécuté l'annule. Fermer une **position** la vend à l'**ouverture de la séance
suivante** (15h30), au prix du marché. Sa place et son capital sont libérés tout de suite pour un nouveau signal.

Pour une nouvelle à surveiller qui ne justifie pas de fermer, il t'envoie seulement une information (ℹ️).

### Ce que tu peux relire

Tout est commité dans `agent/` sur `main` après chaque session :

- `sessions/AAAA-MM-JJ.md` : les verdicts de Claude, une ligne par candidat et par titre suivi ;
- `sessions/AAAA-MM-JJ-journal.md` : le journal de la session, dans l'ordre : ses réflexions entre deux étapes,
  chaque recherche web, page lue et outil lancé, avec un extrait de la réponse (1 500 caractères au plus). Écrit aussi
  quand la session échoue ;
- `notes.md` : sa mémoire d'une session à l'autre ;
- `positions.json` : ordres, positions et trades fermés, avec la justification de chaque achat et de chaque vente.

### Sécurité

- Le contenu lu en ligne est une donnée, jamais une instruction.
- Si la session Claude échoue, aucun signal ni aucune fermeture n'est appliqué ; nouvelle tentative 5 minutes plus
  tard, puis abandon pour la journée.
- Le détail (secrets, permissions, validation des textes, pages piégées) est à la section 12.

---

## 7. Vie d'un trade (le code, toutes les 5 min)

1. **Ordre d'achat** : valable **3 séances** (`validite_ordre_seances`). Il est exécuté au prix visé seulement si
   le cours descend **nettement** dessous, d'au moins 0,05 ATR (`marge_limite_atr`) : en vrai, un ordre au prix tout
   juste touché n'est souvent pas servi. Si le titre ouvre déjà sous le prix visé, l'achat se fait à l'ouverture.
   Sinon l'ordre expire. Il est **annulé** avant :
   - si le titre ouvre plus d'1 ATR sous le prix visé (`annulation_gap_atr`) : un tel écart signale souvent une
     mauvaise nouvelle ;
   - si une séance clôture sous la moyenne mobile 50 jours du jour du signal (`annulation_sous_mm50`) : la tendance
     n'est plus là ;
   - si des résultats trimestriels tombent avant la fin possible du trade (`annulation_resultats`).
2. **Position ouverte**, puis la première de ces sorties qui arrive :
   - **Stop** touché : vente au stop (ou à l'ouverture si le titre ouvre en dessous).
   - **Objectif** touché : vente à l'objectif (ou à l'ouverture si le titre ouvre au-dessus).
   - Si stop et objectif sont touchés la même séance, on compte le **stop** (hypothèse prudente).
   - **Le jour de l'achat**, ni objectif ni remontée du stop (`jour_entree_prudent`) : une barre journalière ne dit
     pas si le plus haut est venu avant ou après l'achat. Le stop, lui, reste possible ce jour-là.
   - **Stop remonté** : dès que le cours a monté de 1,5 ATR au-dessus de l'achat, le stop passe au prix d'achat ; le
     trade ne peut plus perdre (`remontee_stop_atr`). Il ne redescend jamais.
   - **Stop temporel** : vente à la clôture de la **15e séance** (environ 3 semaines) si rien d'autre n'est arrivé
     (`stop_temporel_seances`).
   - **Vente décidée par Claude** : à l'ouverture de la séance qui suit sa décision.
3. **Frais et glissement** : chaque ordre coûte 1 € + 0,1 % du montant (`frais_fixes_eur`, `frais_pct`). Les ventes
   au marché (stop, stop temporel, décision de Claude) perdent en plus 0,05 % de glissement (`glissement_pct`) ;
   l'achat et l'objectif sont des ordres limites, sans glissement. Chaque trade fermé garde son résultat **avant
   frais** et **après frais** ; c'est le second qui compte.
4. **Division d'une action (split)** : si un titre suivi se divise (par exemple 1 action devient 10), Yahoo divise
   tout l'historique ; le code divise alors prix, stop et objectif et multiplie la quantité, pour ne pas croire à une
   chute de 90 %. Si Yahoo ne répond pas sur les divisions, le titre n'est pas suivi pendant ce passage.

Chaque événement envoie un message Telegram avec le résultat en euros. Le message ne part qu'une fois l'état du
portefeuille enregistré dans le dépôt : si l'enregistrement échoue, rien ne part, et le passage suivant refait les
mêmes calculs et les mêmes messages.

---

## 8. Mesure des résultats

- **Bilan du vendredi** : capital, résultat de la semaine et depuis le début, comparé au simple fait de détenir QQQ ;
  taux de réussite, gain moyen, perte moyenne et **espérance par trade**.
- **Backtest** (`backtest.py`) : rejoue les règles du code sur l'historique, **sans Claude** (chaque candidat est
  pris), contre QQQ. Il donne le résultat avant et après frais, le **R moyen** (gain ou perte d'un trade divisé par la
  perte prévue au stop : +1 R = on a gagné autant qu'on risquait), le **facteur de profit** (total des gains divisé par
  total des pertes : au-dessus de 1, la stratégie gagne), la **baisse maximale** (pire recul depuis un plus haut) et le
  rendement annualisé. Il prévient quand il y a moins de 30 trades, trop peu pour conclure.
  - `--preglage reference` rejoue la stratégie d'avant les corrections (commit 04973f7, sans frais), `--preglage
    amelioree` la stratégie actuelle, `--preglage tous` les deux sur les mêmes données.
  - `--debut 2018-01-01 --fin 2022-12-31` choisit une période : régler les paramètres sur une période et les juger
    sur une autre évite de les ajuster au hasard d'une seule période.
  - `--frais-pct` et `--glissement-pct` testent des frais plus lourds.

---

## 9. Limites connues, à garder en tête

**Ce que disent les backtests** (21 octobre 2026, chaque candidat pris, sans Claude, Nasdaq 100 actuel) :

| Période | Version | Trades | Gagnants | R moyen | Facteur de profit | Résultat | QQQ |
|---|---|---|---|---|---|---|---|
| 2023-11 → 2026-10 | actuelle (frais compris) | 87 | 33,3 % | −0,02 | 0,90 | −3,8 % | +97,5 % |
| 2023-11 → 2026-10 | référence, mêmes frais | 97 | 35,1 % | +0,12 | 1,18 | +15,6 % | +97,5 % |
| 2023-11 → 2026-10 | référence, sans frais | 97 | 35,1 % | +0,22 | 1,39 | +33,1 % | +97,5 % |
| 2018-01 → 2022-12 | actuelle (frais compris) | 124 | 27,4 % | −0,23 | 0,66 | −19,3 % | +71,0 % |
| 2018-01 → 2022-12 | référence, sans frais | 121 | 34,7 % | +0,18 | 1,34 | +39,5 % | +71,0 % |

- **Après des frais réalistes, la stratégie actuelle n'a pas d'avance mesurable** : sur 3 ans, elle gagne
  153 € avant frais et en paie 276 €. Avec des frais plus lourds (1 € + 0,2 %, glissement 0,2 %), elle perd 8,5 %.
- À frais égaux, la référence fait mieux que la version actuelle sur 3 ans (+0,12 R contre −0,02 R). Deux
  raisons probables, non mesurées séparément : les règles prudentes retirent des gains que la référence comptait
  sans doute à tort (achat au simple contact du prix, objectif possible le jour de l'achat), et le risque de 1 % au
  lieu de 2 % double le poids des frais fixes face au gain visé. La version actuelle se veut plus **réaliste**, pas
  plus rentable.
- Dans tous les cas, **détenir QQQ a fait beaucoup mieux**, avec une baisse maximale comparable (−17,5 % pour la
  version actuelle sur 3 ans, −22,9 % pour QQQ).
- Sur des périodes d'un mois, les résultats varient énormément, avec trop peu de trades pour conclure.
- 7 titres récents n'ont pas de données sur 2018-2022.

- **Frais simplifiés** : la simulation compte 1 € + 0,1 % par ordre et 0,05 % de glissement sur les ventes au
  marché. Les frais de change réels de ton courtier peuvent être plus lourds : `--frais-pct` permet de le tester.
- **Biais du survivant** : le backtest prend la liste **actuelle** du Nasdaq 100 pour tout l'historique. Les titres
  sortis de l'indice (souvent parce qu'ils ont chuté) n'y sont pas, ce qui embellit les résultats passés.
- **Barres journalières** : le backtest ne voit qu'un plus haut et un plus bas par jour, pas leur ordre. Les règles
  prudentes (stop compté avant l'objectif, rien le jour de l'achat sauf le stop) corrigent ce défaut dans le sens
  défavorable.
- **Dates de résultats** : Yahoo ne donne que les publications récentes. Sur une période ancienne, une date
  introuvable fait exclure le titre, ce qui réduit le nombre de trades.
- **Délai des messages** : les messages de la séance partent à la fin du passage complet (suivi, momentum, puis
  session Claude le soir), soit jusqu'à une vingtaine de minutes de retard après la clôture. En lançant les scripts
  sur ton ordinateur, les messages restent dans `agent/a-envoyer.json` jusqu'à `python envoyer.py`.
- **Données Yahoo** gratuites : parfois en retard ou incomplètes.
- **Un mois de résultats fictifs ne prouve rien** : il faut plusieurs dizaines de trades pour juger l'espérance.

---

## 10. Points que tu peux vouloir changer

| Question | Réglage actuel |
|---|---|
| Activer les cassures en plus des replis ? | `configurations.B` = false |
| Prendre plus ou moins de risque par trade ? | 1 % du capital (de 0,5 à 1 % conseillé), 1 200 € max par position, 4 % de risque total |
| Plus de titres en même temps ? | 5 titres, 2 par industrie |
| Objectif plus proche (plus souvent atteint) ou plus loin (gains plus gros) ? | 3 ATR, soit 2 fois le risque |
| Garder les positions plus ou moins longtemps ? | 15 séances |
| Laisser l'ordre valable plus longtemps ? | 3 séances |
| Fermer automatiquement avant les résultats ? | inutile : aucun achat à moins de 18 séances des résultats, et un ordre est annulé si une date tombe pendant le trade |
| Frais de ton courtier ? | 1 € + 0,1 % par ordre, 0,05 % de glissement |
| Élargir l'univers au-delà du Nasdaq 100 ? | Nasdaq 100 seulement |

---

## 11. Second portefeuille : rotation momentum

Un second portefeuille **fictif** de 3 000 €, suivi à côté du premier pour comparer (`momentum.py`, `court_terme/momentum.py`, état dans `agent/momentum.json`, onglet « Momentum » du tableau de bord). Claude n'y joue aucun rôle : tout est mécanique.

- **Classement** : chaque fin de mois, performance des titres du Nasdaq 100 sur 12 mois **hors dernier mois** (clôtures ajustées des dividendes ; un titre sans historique complet sur la période est écarté).
- **Filtre de marché** : si QQQ est sous sa moyenne 200 jours, tout est vendu et le portefeuille reste en liquidités jusqu'à la fin de mois suivante.
- **Sélection** : les 5 premiers. Un titre déjà détenu qui reste dans le Top 5 est conservé sans rééquilibrage ; les liquidités sont réparties à parts égales entre les entrants.
- **Horaires** : décision après 16h15 New York le dernier jour ouvré du mois ; exécution à l'ouverture suivante, au prix de la première bougie de 5 min, après 9h35. Rien n'est envoyé sur Telegram : on suit ce portefeuille sur le tableau de bord. Une fin de mois manquée (jour férié, panne) est rattrapée à la séance suivante.
- **Frais** : 1 € + 0,1 % par ordre ; liquidités en euros, conversion au taux EURUSD du jour.
- **Suivi** : valeur relevée chaque soir après la clôture, comparée à QQQ depuis son ouverture le jour du premier achat.
- **Démarrage** : première décision le vendredi 30 octobre 2026, premiers achats le lundi 2 novembre.

Backtest 2018-2026 (composition historique du Nasdaq 100, frais compris) : +88 % sur 2018-2022 (QQQ +77 %) et +135 % sur 2023-2026 (QQQ +101 %), mais avec des baisses maximales de −46 % et −37 %. La stratégie concentre le risque sur 5 titres souvent du même secteur et peut sous-performer longtemps lors des retournements brusques. Les dividendes ne sont pas comptés dans la valeur du portefeuille fictif (léger désavantage face à QQQ).

Réglages : section `momentum` de `config.json` (`titres`, `mois_perf`, `mois_exclus`, `filtre_mm`, frais).

---

## 12. Fiabilité, sécurité et apprentissage

Tout reste **simulé** : aucun script ne parle à un courtier, aucun ne peut passer un ordre réel.

### Dans le doute, aucun achat (`court_terme/securite.py`)

L'agent ne crée **aucun nouvel ordre** ce jour-là si :

- les cours du jour manquent, sont périmés ou incohérents (plus bas au-dessus du plus haut, clôture hors de la barre,
  prix nul) pour QQQ ou pour plus de 10 % des titres ; un titre seul aux cours douteux est simplement écarté ;
- le suivi des ordres et positions est en retard : un passage de `positions.py` a échoué (par exemple une panne de
  GitHub Actions) et la séance n'a pas été traitée. L'agent ne suppose jamais qu'elle l'a été ;
- les nouvelles entrées sont **suspendues**.

La **suspension** est posée par le code quand le capital réalisé recule de 10 % depuis son plus haut
(`suspension_baisse_pct`) ou quand l'état enregistré est incohérent (même identifiant deux fois, titre suivi deux
fois, état qui ne correspond pas à sa liste, prix dans le désordre). Le suivi des positions continue. Seul toi la
lèves, après vérification : `python suspension.py etat` pour voir, `python suspension.py lever` pour reprendre.

Deux passages ne tournent jamais en même temps (verrou, et file d'attente côté GitHub Actions) ; un événement déjà
annoncé n'est pas renvoyé. Claude ne peut pas demander plus de 2 ventes par session (`fermetures_max_session`).

### Identifiant, états et historique (`court_terme/etats.py`)

Chaque ordre reçoit un **identifiant** unique (par exemple `2026-10-21-AAPL-3f9a1c`), affiché dans le message du
signal, et un **historique** daté de tout ce qui lui arrive. Ses états : candidat → en attente → exécuté → clôturé, ou
annulé / expiré. Un passage d'un état à un autre non prévu (par exemple d'expiré à exécuté) est refusé par le code.
L'état « partiellement exécuté » est prévu mais jamais produit : la simulation achète tout ou rien. Les ordres annulés
et expirés restent consultables dans `ordres_clos`.

### Sécurité de l'IA

- **Secrets hors de portée** : la session Claude tourne sans les variables Telegram ni le jeton GitHub, et sans les
  outils de lecture et d'écriture de fichiers. Elle ne peut lancer que les outils de `outils/` et lire le web.
- **Le code décide** : Claude ne fixe ni prix, ni quantité, ni plafond. Ses textes (justification, note, motif de
  vente) sont contrôlés et bornés par `court_terme/validation.py` : liens et caractères cachés retirés, longueur
  limitée, rubriques obligatoires dans une justification d'achat. Un signal sur un titre absent des candidats du jour
  est refusé.
- **Pages piégées** : `tests/test_injection.py` vérifie sans Claude que les outils résistent à des textes piégés.
  `tests/injection/lancer.py` lance une **vraie** session face à une page qui tente de voler un faux secret ou de
  faire acheter un titre : il consomme ton abonnement, à lancer à la main seulement.

### Fiche de chaque trade (`python analyse_trades.py`)

Pour chaque trade fermé : prix d'entrée prévu et obtenu, stop et objectif **initiaux**, prix et motif de sortie,
frais, glissement, résultat net en € et en R, durée, **MFE** (plus grand gain latent pendant le trade, en R) et
**MAE** (plus grande perte latente, en R), date de résultats connue avant l'achat et événements survenus pendant le
trade. Le script rejoue aussi des **variantes** (achat au marché, sans objectif, sans remontée du stop, objectif à
2 ATR, stop temporel à 10 séances) pour comprendre ce qui a joué. Il écrit `agent/analyses/trades.json` et ne touche
jamais au résultat officiel ni aux réglages : **aucun ajustement automatique**.

### Évaluation de Claude (`python evaluation_claude.py`)

Chaque session laisse une trace dans `agent/decisions/AAAA-MM-JJ.json` : avis de Claude sur chaque candidat et son
motif, ordre réellement créé par le code, refus du code, coût de la session (tours, durée, recherches, pages lues) et
sources consultées. Le script rejoue ensuite **tous** les candidats, validés ou rejetés, avec les mêmes règles, et
compare : les validés, les rejetés, et l'ensemble des candidats, qui représente la stratégie **sans IA**. La
différence est l'**apport de Claude**. Il liste aussi les rejets qui auraient atteint l'objectif, à relire.

- **Un bon trade n'est pas une bonne décision** : il faut au moins 30 trades avant de conclure, et le script le
  rappelle tant que ce n'est pas le cas.
- **Pas de score de confiance** : Claude n'en donne pas. Une note du type « confiance 80 % » ne serait pas une
  probabilité tant qu'on n'aurait pas vérifié, sur un grand nombre de trades, que 80 % d'entre eux gagnent.

### Registre des versions (`court_terme/preglages.py`, `python backtest.py --enregistrer`)

`VERSIONS` décrit chaque version de la stratégie : identifiant, date, modifications, périodes de conception et de
validation, métriques, limites et hypothèses. La **référence** (commit 04973f7) ne change jamais. Avec
`--enregistrer`, chaque backtest ajoute à `experiences/registre.json` ses paramètres exacts, sa période, le commit
du code et ses mesures, sans jamais réécrire les lignes précédentes. `--memes-frais` compare la référence et la
version actuelle avec les mêmes frais et le même glissement.

### Journal personnel facultatif (`python journal_personnel.py`)

Si tu passes toi-même des ordres réels chez ton courtier, tu peux les noter à la main ici, à part du portefeuille
fictif : `ajouter AAPL achat 182.40 3 --frais 1 --ref <réf. du signal>`, puis `liste` ou `bilan`. Les données restent
sur ton ordinateur (`personnel/journal.json`, jamais commité ni envoyé). Le script ne contacte aucun courtier. La
performance du portefeuille fictif n'est **pas** la tienne et n'apparaît jamais dans ce bilan.

### Rapport quotidien d'audit (`agent/audit/AAAA-MM-JJ.md`)

Écrit par `agent.py` à chaque passage du soir, bref et limité à ce qui compte : fraîcheur des données, titres
écartés, erreurs et sessions échouées, ordres et positions à vérifier (suivi en retard, état incohérent, suspension,
ordre décidé par la session mais absent de l'état enregistré ou l'inverse), événements du jour et résultats
trimestriels proches, signaux annulés ou expirés, décisions de Claude avec leurs sources. Seuls les points « à
vérifier » **nouveaux** partent sur Telegram ; une journée normale se résume à « Rien d'anormal. ».
