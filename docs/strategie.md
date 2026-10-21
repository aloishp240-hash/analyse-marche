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
| Envoi des signaux et décisions sur Telegram | le code | à la fin de la session Claude |
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

Avec des positions de 40 % au plus, le capital bloque en pratique avant le nombre de titres : 2 à 3 positions
pleines à la fois.

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

- résultats trimestriels publiés dans les **10 prochaines séances** (`seances_sans_resultats`) ;
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

1. le nombre d'actions qui fait perdre **2 % du capital** (60 €) si le stop est touché (`risque_pct`) ;
2. le nombre d'actions qui tient dans **40 % du capital** (1 200 €) (`plafond_position_pct`).

Dans l'exemple : 8 actions pour 60 € de risque, mais 6,57 actions pour 1 200 €. Retenu : **6,57 actions**, soit un
risque réel d'environ 49 €. Pour la plupart des titres, c'est le plafond de 1 200 € qui décide.

Ces deux réglages valaient 1 % et 20 % au départ. Le backtest sur 3 ans avec frais (1 € + 0,1 % par ordre) les a
fait doubler : avec 600 € par trade, les frais mangeaient tout l'avantage (−6,5 % sur 3 ans) ; avec 1 200 €, ils
pèsent moitié moins (+19 %, positif chaque année, mais baisse maximale de −23,5 %).

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

---

## 7. Vie d'un trade (le code, toutes les 5 min)

1. **Ordre d'achat** : valable **3 séances** (`validite_ordre_seances`). Il est exécuté dès que le cours descend au
   prix visé, à ce prix (ou à l'ouverture si le titre ouvre déjà en dessous). Sinon il expire.
2. **Position ouverte**, puis la première de ces sorties qui arrive :
   - **Stop** touché : vente au stop (ou à l'ouverture si le titre ouvre en dessous).
   - **Objectif** touché : vente à l'objectif (ou à l'ouverture si le titre ouvre au-dessus).
   - Si stop et objectif sont touchés la même séance, on compte le **stop** (hypothèse prudente).
   - **Stop remonté** : dès que le cours a monté de 1,5 ATR au-dessus de l'achat, le stop passe au prix d'achat ; le
     trade ne peut plus perdre (`remontee_stop_atr`). Il ne redescend jamais.
   - **Stop temporel** : vente à la clôture de la **15e séance** (environ 3 semaines) si rien d'autre n'est arrivé
     (`stop_temporel_seances`).
   - **Vente décidée par Claude** : à l'ouverture de la séance qui suit sa décision.

Chaque événement envoie un message Telegram avec le résultat en euros.

---

## 8. Mesure des résultats

- **Bilan du vendredi** : capital, résultat de la semaine et depuis le début, comparé au simple fait de détenir QQQ ;
  taux de réussite, gain moyen, perte moyenne et **espérance par trade**.
- **Backtest** (`backtest.py`) : rejoue les règles du code sur l'historique, **sans Claude** (chaque candidat est
  pris), mois par mois, contre QQQ. Le rapport est `rapport_backtest.html`.

---

## 9. Limites connues, à garder en tête

- **Pas de frais** : la simulation ignore les frais de courtage et de change. Sur des ordres de 1 200 €,
  ils coûtent environ 4,4 € par aller-retour (1 € + 0,1 % par ordre).
- **Exécution idéale** : la simulation achète exactement au prix visé et vend exactement au stop. En vrai, tu peux
  acheter plus haut dans la zone, et un stop peut être exécuté un peu plus bas (glissement).
- **Résultats pendant le trade** : un titre n'est pas acheté à moins de 10 séances de ses résultats, mais une position
  peut durer 15 séances. Elle peut donc traverser une publication ; c'est à Claude de décider s'il ferme avant.
- **Données Yahoo** gratuites : parfois en retard ou incomplètes.
- **Un mois de résultats fictifs ne prouve rien** : il faut plusieurs dizaines de trades pour juger l'espérance.

---

## 10. Points que tu peux vouloir changer

| Question | Réglage actuel |
|---|---|
| Activer les cassures en plus des replis ? | `configurations.B` = false |
| Prendre plus ou moins de risque par trade ? | 2 % du capital, 1 200 € max par position |
| Plus de titres en même temps ? | 5 titres, 2 par industrie |
| Objectif plus proche (plus souvent atteint) ou plus loin (gains plus gros) ? | 3 ATR, soit 2 fois le risque |
| Garder les positions plus ou moins longtemps ? | 15 séances |
| Laisser l'ordre valable plus longtemps ? | 3 séances |
| Fermer automatiquement avant les résultats ? | non, c'est Claude qui juge |
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
