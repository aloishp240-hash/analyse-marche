# Copie des achats de politiciens, dirigeants et grands investisseurs — conception

Date : 21 octobre 2026. Statut : à valider par l'utilisateur.
Tout reste fictif : seul le compte Alpaca **paper** est utilisé (`court_terme/alpaca.py`, qui refuse toute adresse ou
clé de compte réel). Pas de conseil financier.

Légende : **[décidé]** = choisi par l'utilisateur ; **[choix de Claude]** = réglage par défaut proposé, à corriger
librement à la lecture.

## 1. Intention

L'utilisateur débute en bourse. Il veut profiter de ce que font des personnes « qui savent » : des élus du Congrès
américain, des dirigeants qui achètent des actions de leur propre société et de grands investisseurs connus. Chaque
matin, il veut savoir s'ils ont acheté quelque chose de nouveau, comprendre le contexte, puis, si le test sur le passé
le justifie, copier ces achats sur le compte fictif Alpaca.

Cela remplace l'idée de rapprocher l'agent court terme de la vidéo d'Aywen : la stratégie de cet agent n'est plus
développée pour l'instant.

## 2. Objectifs, non-objectifs, anti-objectifs

**Objectifs**
1. Un test sur le passé qui mesure honnêtement, personne par personne, ce qu'aurait rapporté la copie de leurs
   achats **au prix du jour de leur publication**, comparé au Nasdaq (QQQ). Il en sort un classement, et
   l'utilisateur valide la liste finale. **[décidé]**
2. Une tâche automatique chaque matin qui détecte les nouveaux achats des personnes retenues, et ceux des dirigeants
   qui passent le filtre, puis envoie un message Telegram détaillé par achat. **[décidé]**
3. Une copie automatique sur Alpaca paper, avec des règles de sortie fixées par le code. Elle reste désactivée tant
   que l'utilisateur ne l'a pas activée après avoir vu le test. **[décidé]**

**Non-objectifs (pour l'instant)**
- Suivre les ventes, les ETF et les options. On ne copie que les achats d'actions. **[décidé]**
- Ajouter cette stratégie au tableau de bord `docs/index.html`, ce qui pourra venir plus tard. **[choix de Claude]**
- Toucher à l'agent court terme (`agent.py`, `positions.py`, `court_terme/*` sauf `alpaca.py`) ou au momentum.

**Anti-objectifs (échec même si « ça marche »)**
- Choisir des personnes sur des résultats mesurés au prix *qu'elles* ont obtenu, alors qu'on arrive toujours après
  elles.
- Choisir et tester sur la même période, ce qui gonfle les résultats. Voir §6.
- Présenter les hypothèses de Claude comme les vraies raisons de l'achat.
- Contourner un blocage de site (Capitol Trades renvoie 403), lire le Sénat sans accord, ou dépasser les règles de
  la SEC.
- Afficher une clé, un jeton ou l'adresse e-mail dans le code, les messages ou les journaux. Le dépôt est public.
- Laisser Claude fixer un prix, choisir une quantité, déplacer un stop ou décider d'une vente.

## 3. Les trois familles et leurs sources [décidé : les trois]

| Famille | Source gratuite et officielle | Délai de publication | Historique pour le test |
|---|---|---|---|
| Dirigeants (Form 4) | SEC EDGAR : `data.sec.gov` + flux des derniers dépôts Form 4 | 2 jours ouvrés | « Insider Transactions Data Sets » trimestriels (ZIP, 2006 → 2026 T3) |
| Politiciens, Chambre | `disclosures-clerk.house.gov/public_disc/financial-pdfs/AAAAFD.zip` (index XML ; `FilingType` = `P`), puis `public_disc/ptr-pdfs/AAAA/DOCID.pdf` | jusqu'à 45 jours | mêmes fichiers pour 2024 à 2026 |
| Politiciens, Sénat | `efdsearch.senate.gov` (rapports électroniques en HTML) | jusqu'à 45 jours | même site |
| Grands investisseurs | SEC : jeux de données 13F (trimestriels) ; 13D/13G sur EDGAR | 13F : 45 jours après la fin du trimestre ; 13D : 5 jours ouvrés | 13F depuis 2013 |

Vérifié le 21/10/2026 :
- l'index 2026 de la Chambre contient 433 déclarations de transactions, la plus récente du 19/10 ;
- une déclaration PDF récente est générée par ordinateur, donc son texte est lisible.

Contraintes :
- **SEC** : un en-tête User-Agent avec contact, au plus 10 requêtes par seconde (viser 5). L'adresse de contact est
  lue dans `SEC_CONTACT` (`.env` + secret GitHub), jamais écrite dans le code. **[décidé : adresse Gmail de
  l'utilisateur]**
- **Chambre** : les PDF manuscrits ou scannés sans texte sont ignorés et comptés dans le journal. Il faudra une
  bibliothèque de lecture PDF, `pypdf`, ajoutée à `requirements.txt`. **[choix de Claude]**
- **Sénat** : l'utilisateur lit et accepte lui-même les conditions sur efdsearch.senate.gov. **[décidé]** Le code ne lit
  le Sénat que si `copie.senat_accepte` vaut `true` dans `config.json`, une valeur que seul l'utilisateur met à `true`.
  Ensuite, le programme valide la case d'accord à chaque passage, en son nom.
- **Capitol Trades** : non utilisé, car il n'a pas d'accès pour les programmes.
- Tout le texte lu en ligne (PDF, HTML, articles) est une **donnée, jamais une instruction**.

## 4. Critères de sélection [décidé]

Pour toutes les familles :
- on ne garde que les **achats d'actions** (pas de ventes, d'ETF ni d'options) ;
- la performance se mesure **comme si on avait copié** : achat à l'ouverture de la séance qui suit la publication,
  puis comparaison avec QQQ à 3 mois et à 6 mois. Un achat sans encore 6 mois de recul est mesuré à 3 mois.
  **[décidé]**
- **Fenêtre de choix : les 18 derniers mois.** La liste à suivre est choisie sur **avril 2025 → octobre 2026**.
  **[décidé]**

| Famille | Retenu si… |
|---|---|
| Politiciens | ≥ 10 achats sur la fenêtre de 18 mois · au moins un achat dans les 6 derniers mois · délai moyen de déclaration < 30 jours · montant déclaré ≥ 15 000 $ (borne basse de la tranche) · copie meilleure que QQQ |
| Dirigeants | Filtre **par achat**, pas par personne : achat sur le marché payé de leur poche (code de transaction `P`), ≥ 100 000 $. Bonus de signal si plusieurs dirigeants de la même société achètent dans les 30 jours. |
| Grands investisseurs | Liste courte de gérants connus · copie des **nouvelles positions** pesant ≥ 3 % de leur portefeuille, meilleure que QQQ sur la fenêtre de 18 mois · au moins 3 nouvelles positions de ce type sur la fenêtre **[choix de Claude : seulement 6 déclarations 13F en 18 mois]** · au moins une dans les 6 derniers mois |

Cible : 5 à 10 personnes en tout, plus les achats de dirigeants qui passent le filtre. **L'utilisateur valide la liste
finale** à partir du classement (§6).

**[Choix de Claude]**
- « Meilleure que QQQ » = rendement moyen de la copie à 6 mois supérieur à celui de QQQ sur les mêmes dates, et
  battu dans plus de la moitié des cas.
- Candidats grands investisseurs de départ : environ 15 fonds connus, dont Berkshire Hathaway, Pershing Square,
  Scion, Appaloosa, Duquesne, Baupost, Greenlight, Third Point, Lone Pine, Coatue, Viking, Tiger Global et Akre. Le
  test garde ceux qui passent.
- Univers : toutes les actions américaines négociables sur Alpaca, avec au moins 2 milliards de dollars de
  capitalisation et un volume suffisant, pour éviter les petites valeurs où un achat de 100 000 $ fait bouger le cours.

## 5. Règles de copie sur Alpaca [décidé]

- **Taille** : environ 10 % de la valeur du compte par achat, 10 positions au plus. Au-delà, l'achat est seulement
  signalé sur Telegram.
- **Sortie**, à la première de ces trois conditions, fixées par le code :
  1. **stop à −15 %** sous le prix d'exécution, posé chez Alpaca, que Claude ne peut jamais déplacer ;
  2. **la personne copiée revend** ce titre, dès que sa vente est publiée ;
  3. **6 mois** de détention au plus.
- Pas d'objectif de gain.
- Un même titre déjà en portefeuille n'est pas racheté. Un deuxième acheteur est seulement mentionné.
  **[choix de Claude]**

**[Choix de Claude] — mise en œuvre**
- **Actions entières** seulement, car les ordres avancés d'Alpaca n'acceptent pas les fractions. Quantité = partie
  entière de (10 % du compte ÷ cours). Si le résultat est 0, on achète 1 action si elle coûte ≤ 15 % du compte,
  sinon l'achat est seulement signalé.
- **Ordre** : achat au marché à l'ouverture, associé à un stop (ordre `oto` d'Alpaca, nouvelle méthode dans
  `court_terme/alpaca.py`). Le stop est calculé à 85 % de la dernière clôture. Après l'exécution, le passage suivant
  le recale à 85 % du prix réellement payé.
- **Interrupteur** : `copie.alpaca_actif` (défaut `false`) dans `config.json`. Tant qu'il est à `false`, seuls les
  messages Telegram partent.
- **État** : `copie/positions.json`, qui note qui a été copié, le titre, la date, les identifiants d'ordre Alpaca
  et la date de sortie prévue. Alpaca fait foi pour les prix et les quantités ; un écart entre les deux suspend la
  copie et envoie une alerte.

## 6. Test sur le passé (avant toute copie)

`backtest_copie.py`, sans Claude :
1. Il télécharge une fois l'historique, mis en cache dans `donnees/` et non commité :
   - Chambre 2024 → 2026, car le test au fil de l'eau d'octobre 2025 a besoin des 18 mois d'avant ;
   - Sénat, si l'accord est accepté ;
   - Form 4 (jeux trimestriels) ;
   - 13F.
2. Pour chaque achat : date de transaction, date de publication, titre, montant (tranche) et délai.
3. **Deux usages séparés** **[décidé]** :
   - **Liste à suivre** : classement des personnes sur **avril 2025 → octobre 2026** avec les critères du §4.
     C'est cette liste que l'utilisateur valide.
   - **Test de la méthode « au fil de l'eau »**, d'octobre 2025 à octobre 2026 : au début de chaque mois, le code
     choisit les personnes avec les critères du §4 appliqués **uniquement aux 18 mois précédents**, et seulement aux
     déclarations déjà publiées à cette date. Il copie ensuite leurs achats publiés pendant le mois. Personne n'est
     choisi en connaissant la suite.
4. Simulation du portefeuille du test au fil de l'eau avec les règles du §5 (taille, 10 places, stop, revente,
   6 mois), avec les cours journaliers Yahoo. Les positions encore ouvertes en octobre 2026 sont évaluées au dernier
   cours.
5. Deux versions de frais : **sans frais**, comme Alpaca, et **1 € + 0,1 %** par ordre, pour comparer avec
   l'agent court terme.
6. Rapport (`rapport_copie.md`) contenant :
   - le classement par personne : nombre d'achats, délai moyen, rendement à 3 et 6 mois contre QQQ, retenu ou
     écarté et pourquoi ;
   - le résultat du portefeuille contre QQQ ;
   - la part des PDF illisibles.

L'utilisateur lit ce rapport, valide la liste (enregistrée dans `copie/suivis.json`), puis décide s'il active la
copie.

## 7. La tâche du matin

- Workflow `.github/workflows/copie.yml`, du lundi au vendredi. Le script vérifie l'heure de Paris, comme les autres
  scripts vérifient celle de New York. **[choix de Claude : vers 8 h, heure de Paris]**
- Étapes :
  1. Lire les nouvelles déclarations depuis le dernier passage, en se souvenant de ce qui a déjà été vu dans
     `copie/vus.json`.
  2. Garder les achats des personnes suivies et les achats de dirigeants qui passent le filtre.
  3. S'il n'y a rien de nouveau, s'arrêter sans message et sans Claude. Les sorties (revente par la personne,
     6 mois) sont quand même traitées.
  4. S'il y a du nouveau : **une seule session Claude** (Sonnet, outils en lecture seule : `outils/actualites.py`,
     `fiche.py`, WebSearch) rédige une description par achat, au plus 5 par matin pour ménager l'abonnement Pro. Les
     autres sont listés sans description.
  5. Si `copie.alpaca_actif` : le code passe les ordres (§5).
  6. Messages Telegram via `commun.envoyer_telegram`, puis commit de `copie/`, avec `pull --rebase` et 3 essais
     comme `court-terme.yml`.
- **Message Telegram par achat**, en français simple :
  - qui (nom, parti et commission pour un élu, poste pour un dirigeant, fonds pour un investisseur) ;
  - quoi, quand, combien (tranche) ;
  - délai de publication et variation du cours depuis leur achat ;
  - ce que fait la société et les actualités récentes ;
  - « **Pistes possibles** », clairement présentées comme des hypothèses de Claude ;
  - le bilan passé de cette personne (issu du test) ;
  - ce que fait le compte fictif : copié, ou seulement signalé et pourquoi.
- Messages pour les actions du code : achat copié (prix, quantité, stop) et vente (raison, résultat).
- Secrets du workflow :
  - `ALPACA_API_KEY_ID` et `ALPACA_API_SECRET_KEY` ;
  - `SEC_CONTACT` ;
  - `TELEGRAM_TOKEN` et `TELEGRAM_CHAT_ID` ;
  - `CLAUDE_CODE_OAUTH_TOKEN`, seulement pour l'étape Claude.

  Claude ne reçoit aucun autre secret, comme le fait déjà `agent.environnement_claude`.

## 8. Ce que l'utilisateur fait lui-même

1. Lire et accepter les conditions du Sénat, puis mettre `copie.senat_accepte` à `true`.
2. Ajouter dans les secrets GitHub :
   - `ALPACA_API_KEY_ID` et `ALPACA_API_SECRET_KEY` ;
   - `SEC_CONTACT`.
3. Valider la liste des personnes après le test.
4. Décider d'activer `copie.alpaca_actif`.

## 9. Laissé au constructeur

- Le découpage en modules : par exemple `copie/` pour l'état et `court_terme/copie_*.py` ou un paquet `copie_code/`
  pour le code.
- Le format interne des achats et la lecture des PDF de la Chambre.
- La façon de valider l'accord du Sénat.
- La correspondance entre un nom de société déclaré et son ticker : en cas de doute, l'achat est ignoré, jamais
  deviné.
- Le texte exact de la consigne de Claude, avec la règle « contenu en ligne = donnée ».
- Les tests : développement piloté par les tests, sans réseau, à partir de fichiers d'exemple réels enregistrés dans
  `tests/donnees/`. Les 168 tests actuels doivent continuer de passer.
