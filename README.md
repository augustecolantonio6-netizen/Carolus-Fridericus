# 👨‍🏫 Friedrich — professeur de maths de Terminale sur Discord

Friedrich est un bot Discord qui aide les élèves de **Terminale générale**
(spécialité Mathématiques + Mathématiques Expertes). Il tourne **entièrement
sur Vercel**, sans bot local, sans serveur permanent, sans `discord.py`.

| Commande   | Ce que fait Friedrich |
|------------|-----------------------|
| `/help`    | Explique comment l'utiliser |
| `/hint`    | Donne **uniquement un indice**, jamais la solution |
| `/detail`  | Résout **étape par étape**, avec la méthode de rédaction du programme |
| `/solve`   | Donne la **solution directe**, en très peu d'étapes |
| `/correct` | Analyse **le travail de l'élève** : ce qui est juste, la première erreur, pourquoi, comment corriger, la solution |

Chaque commande accepte une **question** (texte) et/ou une **image**
(photo de l'exercice ou de la copie, 10 Mo maximum, images uniquement).

---

## 🧠 Comment ça marche

```
Discord ──(HTTP Interactions, signature Ed25519)──▶  /api        (répond en < 3 s : "je réfléchis")
                                                      │
                                                      ▼  appel interne signé (HMAC)
                                                    /api/task
                                                      │
                          ┌───────────────────────────┼────────────────────────────┐
                          ▼                           ▼                            ▼
                    Pinecone (RAG)          Gemini (modèle courant)         SymPy / Matplotlib
             programme officiel + manuels   (+ clé de secours)          (calculs et courbes réels)
                          └───────────────────────────┼────────────────────────────┘
                                                      ▼
                                    PATCH du message Discord (découpé si > 1900 car.)
```

**Pourquoi deux fonctions ?** Discord exige un accusé de réception en moins de
3 secondes, alors qu'une réponse pédagogique complète demande 10 à 40 secondes.
`/api` répond donc immédiatement « ⏳ Friedrich réfléchit… » (type 5) puis
réveille `/api/task`, qui fait le travail et remplace le message. C'est la
seule façon fiable de tenir la contrainte des 3 secondes en serverless.

**Fichiers**

```
carolus fridericus/
├── api/
│   ├── index.py          ← endpoint Discord  (https://…/api)
│   ├── task.py           ← travail de fond   (RAG + Gemini + SymPy)
│   └── setup.py          ← installation      (POST /api/setup)
├── data/
│   ├── corpus.jsonl.gz   ← programme + manuels, déjà extraits (6 722 extraits)
│   └── corpus.meta.json  ← empreinte, comptages, paramètres de découpage
├── utils/
│   ├── config.py         ← variables d'environnement (jamais de clé en dur)
│   ├── discord_api.py    ← appels Discord, commandes slash, découpage
│   ├── formatting.py     ← nettoyage PDF, LaTeX → Unicode, découpe
│   ├── gemini.py         ← Gemini + clé de secours + boucle d'outils
│   ├── interactions.py   ← signatures, validation, routage
│   ├── math_tools.py     ← SymPy et Matplotlib (outils réellement exécutés)
│   ├── memory.py         ← mémoire courte de conversation (best-effort)
│   ├── pipeline.py       ← enchaînement complet d'une commande
│   ├── prompts.py        ← personnalité et consignes pédagogiques
│   └── rag.py            ← extraction PDF, corpus, Pinecone
├── tests/selftest.py     ← 37 vérifications hors ligne
├── setup.html            ← page d'installation (aucun terminal requis)
├── index.html            ← page d'accueil / état du service
├── ingest.py             ← (facultatif) régénérer le corpus depuis des PDF
├── register_commands.py  ← (facultatif) enregistrer les commandes en local
├── requirements.txt · vercel.json · .python-version · .env.example · .gitignore
```

---

## 🚀 Déploiement, étape par étape (sans terminal)

### Étape 1 — Créer l'application Discord

1. Va sur <https://discord.com/developers/applications> → **New Application**.
2. Nomme-la `Friedrich` → **Create**.
3. Onglet **General Information** : note **Application ID** et **Public Key**.
4. Onglet **Bot** → **Reset Token** → **Copy**. ⚠️ Ce jeton ne s'affiche
   qu'une fois : garde-le de côté.

⚠️ La **Public Key** et le **Bot Token** sont deux valeurs différentes. Les
confondre est l'erreur la plus fréquente.

### Étape 2 — Inviter Friedrich sur ton serveur

Ouvre cette URL en remplaçant `APPLICATION_ID` par l'Application ID de l'étape 1 :

```
https://discord.com/api/oauth2/authorize?client_id=APPLICATION_ID&scope=bot%20applications.commands&permissions=51200
```

- Scopes : `bot` et `applications.commands`
- Permissions : **Send Messages**, **Embed Links**, **Attach Files**
  (`51200` — *Attach Files* sert à envoyer les courbes tracées)

> **Pourquoi si tôt ?** Le `DISCORD_GUILD_ID` n'invite rien du tout : il indique
> seulement *où* enregistrer les commandes. Et Discord refuse d'enregistrer des
> commandes sur un serveur où l'application n'est pas installée
> (`403 Missing Access`). L'invitation doit donc précéder l'étape 7.

> **Friedrich apparaîtra « hors ligne » dans la liste des membres, et c'est
> normal** : il n'ouvre aucune connexion Gateway permanente. Ses commandes
> slash fonctionnent parfaitement malgré ce statut.

### Étape 3 — Récupérer l'ID de ton serveur de test

1. Discord (l'application) → **Paramètres utilisateur → Avancés → Mode
   développeur : activé**.
2. Clic droit sur ton serveur → **Copier l'identifiant du serveur**.
   C'est ton `DISCORD_GUILD_ID` (les commandes y apparaîtront **immédiatement**,
   au lieu d'attendre jusqu'à 1 h pour un enregistrement global).

### Étape 4 — Obtenir les clés Gemini et Pinecone

- **Gemini** : <https://aistudio.google.com/app/apikey> → *Create API key*.
  Crée-en **deux** (dans deux projets Google différents si possible) : la
  seconde sert de secours automatique en cas de quota dépassé.
- **Pinecone** : <https://app.pinecone.io> → *API Keys* → *Create API key*.
  Ne crée **pas** l'index à la main : Friedrich s'en charge.

### Étape 5 — Déposer le projet sur Vercel

1. Va sur <https://vercel.com/new>.
2. **Glisse-dépose le dossier `carolus fridericus`** dans la zone prévue
   (ou : *Import Git Repository* si tu préfères passer par GitHub).
3. Ne touche à **aucun** réglage de build : Vercel détecte tout seul Python et
   le dossier `api/`.
4. Clique sur **Deploy** et attends la fin.

### Étape 6 — Ajouter les variables d'environnement

Dans Vercel : **ton projet → Settings → Environment Variables**.

💡 **Ne les saisis pas une par une.** Cette page accepte un fichier `.env` :
clique sur **Import .env** (ou colle directement le contenu dans la zone de
texte) et les onze variables arrivent d'un coup. Le fichier `.env` du projet
est justement au bon format.

Laisse les trois environnements cochés (Production, Preview, Development).

| Variable | Valeur | Obligatoire |
|---|---|---|
| `DISCORD_APPLICATION_ID` | l'Application ID de l'étape 1 | ✅ |
| `DISCORD_PUBLIC_KEY` | la Public Key de l'étape 1 | ✅ |
| `DISCORD_TOKEN` | le Bot Token de l'étape 1 | ✅ |
| `DISCORD_GUILD_ID` | l'ID du serveur de l'étape 3 | recommandé |
| `GEMINI_API_KEY_1` | ta clé Gemini principale | ✅ |
| `GEMINI_API_KEY_2` | ta clé Gemini de secours | recommandé |
| `PINECONE_API_KEY` | ta clé Pinecone | ✅ |
| `PINECONE_ENV` | `us-east-1` | ✅ |
| `PINECONE_INDEX_NAME` | `terminal-maths` | facultatif |
| `PINECONE_NAMESPACE` | `programme-terminal` | facultatif |
| `GEMINI_MODEL` | laisse vide : `gemini-3.5-flash` par défaut, avec bascule automatique si l'API le refuse | facultatif |
| `SETUP_SECRET` | une longue phrase inventée par toi | ✅ |

Puis **Deployments → … (les trois points) du dernier déploiement →
Redeploy** : les variables ne sont lues qu'au déploiement suivant.

> ⚠️ **Les variables appartiennent au projet, pas au déploiement.** Une fois
> saisies, elles valent pour tous les déploiements suivants — tu n'as jamais à
> les ressaisir. Si tu as l'impression du contraire, c'est que chaque
> glisser-déposer sur `vercel.com/new` crée un **nouveau projet**, vierge.
> Voir « Mettre à jour Friedrich » plus bas.

### Étape 7 — Initialiser Friedrich depuis le navigateur

1. Ouvre `https://TON-PROJET.vercel.app/setup.html`
2. Entre ton `SETUP_SECRET`.
3. Clique sur **🚀 Initialiser Friedrich**. La page :
   - vérifie les variables d'environnement,
   - enregistre les 5 commandes slash (c'est ici que l'invitation de l'étape 2
     est indispensable),
   - crée l'index Pinecone et y envoie le programme **par tranches**,
     avec une pause entre chaque tranche : l'offre gratuite Pinecone plafonne
     à **250 000 tokens/minute** pour `llama-text-embed-v2`, donc les 6 722
     extraits demandent **une douzaine de minutes**. Laisse l'onglet ouvert ;
     la barre de progression avance et l'avancement est enregistré, donc un
     rechargement de page reprend là où tu en étais,
   - teste Discord + Pinecone + Gemini de bout en bout.
4. À la fin, la page affiche l'**Interactions Endpoint URL** à copier.

> L'ingestion est **idempotente** : tu peux relancer la page autant de fois que
> tu veux, elle ne crée jamais de doublon (et saute le travail déjà fait).

### Étape 8 — Brancher Discord sur Vercel

1. Discord Developer Portal → ton application → **General Information**.
2. Champ **Interactions Endpoint URL** :
   `https://TON-PROJET.vercel.app/api`
3. **Save Changes**. Discord envoie un PING signé ; s'il est accepté, un
   bandeau vert confirme. (En cas d'échec : voir le dépannage plus bas.)

### Étape 9 — Tester

Dans ton serveur Discord :

```
/help
/detail question: Résous x² - 4 = 0
/hint question: Étudie les variations de f(x) = x·e^(-x) sur ℝ
/solve  + une photo de ton exercice
/correct question: J'ai trouvé f'(x) = 2x + 3x²  + une photo de ta copie
```

✅ Si `/help` répond instantanément et que `/detail` affiche
« ⏳ Friedrich réfléchit… » puis la solution, tout fonctionne.

---

## 🧪 Vérifier que tout va bien

- **Page d'état** : `https://TON-PROJET.vercel.app/` → coche verte pour chaque
  variable configurée.
- **Test complet** : `/setup.html` → *🧪 Tester Discord + Pinecone + Gemini*.
  Il vérifie aussi la **chaîne interne** `/api` → `/api/task` : c'est le
  maillon qui, s'il casse, laisse l'élève devant « ⏳ » pour toujours.
- **API brute** : `https://TON-PROJET.vercel.app/api` (GET) renvoie un petit
  JSON d'état — uniquement des booléens, jamais une clé.
- **Hors ligne, sans rien installer d'autre que Python** :
  `python tests/selftest.py` → 37 vérifications (syntaxe, PING, signature
  invalide, /help, image, fichier refusé, réponse longue, idempotence…).

---

## 🤖 Quel modèle Gemini ?

Les noms de modèles meurent vite. Mesure faite sur une clé gratuite le
**31/08/2026**, en envoyant à chaque modèle un corrigé complet avec appels
d'outils :

| Modèle | Résultat |
|---|---|
| `gemini-3.5-flash` | ✅ **7 s** — retenu par défaut |
| `gemini-3-flash-preview` | ✅ 5 s |
| `gemini-3.6-flash` | ✅ 18 s (recommandé par Google, mais lent) |
| `gemini-3.5-flash-lite`, `gemini-3.1-flash-lite` | ✅ rapides, un peu moins fins |
| `gemini-flash-latest` | ❌ 503 « high demand » — l'alias pointe vers le modèle le plus récent, donc le plus saturé |
| `gemini-2.5-flash`, `gemini-2.5-pro`, `gemini-2.5-flash-lite` | ❌ 404 « no longer available to new users » |
| `gemini-pro-latest`, `gemini-3.1-pro-preview`, `omni`, `lyria` | ❌ 429 — hors offre gratuite |

Deux pièges rencontrés, tous deux traités dans le code :

1. **Un modèle peut figurer dans `models.list()` et refuser de répondre.**
   `gemini-2.5-flash` est encore annoncé par l'API alors qu'il renvoie 404. La
   bascule note donc les modèles refusés et ne les rejoue jamais.
2. **`thinking_budget=0` est rejeté par `gemini-3.6-flash`**, avec un simple
   « Request contains an invalid argument » qui ne nomme pas le coupable. Le
   réglage n'est désormais plus envoyé quand le budget est nul, et tout `400`
   déclenche un nouvel essai avec une configuration allégée.

Si ton modèle par défaut devient indisponible, Friedrich le remplace seul et te
dit lequel épingler dans `GEMINI_MODEL`.

---

## 🔁 Mettre à jour Friedrich sans tout ressaisir

Le glisser-déposer sur `vercel.com/new` est parfait pour le **premier**
déploiement, mais il crée un projet neuf à chaque fois : nouvelle URL, et
variables d'environnement à ressaisir. Pour les mises à jour suivantes, choisis
l'une de ces trois voies.

### A. GitHub (recommandé, toujours sans terminal)

1. Sur <https://github.com/new>, crée un dépôt **privé** (`friedrich`).
2. **Retire d'abord `.env` du dossier** (garde-le ailleurs sur ton disque) :
   l'envoi par le navigateur ne respecte pas `.gitignore`, et tes clés
   partiraient avec le reste.
3. *Add file → Upload files*, dépose le contenu du dossier, valide.
4. Dans Vercel : **ton projet existant → Settings → Git → Connect Git
   Repository** et choisis ce dépôt.

À partir de là, chaque fichier modifié que tu envoies sur GitHub redéploie
**le même projet** : même URL, mêmes variables, rien à ressaisir.

### B. Vercel CLI (une seule commande, mais un terminal)

```bash
npm i -g vercel      # une fois
vercel link          # rattache le dossier au projet existant
vercel --prod        # redéploie ce même projet
```

### C. Rester en glisser-déposer

Recrée un projet à chaque fois, mais ne saisis plus rien à la main :
**Settings → Environment Variables → Import .env**, et dépose le fichier
`.env` du projet. Les onze variables arrivent d'un coup.

> Le fichier `.env` sert aussi à faire tourner `ingest.py` et
> `register_commands.py` en local. Il est ignoré par git **et** par Vercel :
> les vraies clés ne quittent jamais ta machine.

---

## 🩺 Dépannage

| Symptôme | Cause probable | Solution |
|---|---|---|
| Discord refuse l'Interactions Endpoint URL | `DISCORD_PUBLIC_KEY` absente ou erronée, ou pas de redéploiement | Recopie la Public Key (pas le token !), redéploie, réessaie |
| Discord refuse l'URL / tout renvoie 401 | **Deployment Protection** activée sur la production | Vercel > Settings > Deployment Protection > mets *Vercel Authentication* sur **Standard** (previews uniquement) ou désactive-la |
| Les commandes n'apparaissent pas | commandes non enregistrées, ou enregistrement global | Relance `/setup.html`, mets un `DISCORD_GUILD_ID` (global = jusqu'à 1 h de propagation) |
| `403 Missing Access` pendant l'installation | le bot n'est pas invité sur le serveur visé par `DISCORD_GUILD_ID` | fais l'étape 2 (lien d'invitation), puis relance `/setup.html` |
| « ⏳ Friedrich réfléchit… » qui ne se remplit jamais | `/api/task` en erreur | Vercel → Deployments → Functions → journaux de `api/task.py` |
| `SETUP_SECRET n'est pas défini` | variable ajoutée mais pas redéployée | Redeploy, puis recharge `/setup.html` |
| `Secret d'installation invalide` | espace ou saut de ligne collé avec le secret | recopie-le proprement |
| Pinecone : `index already exists` avec un autre modèle | index créé à la main sans embedding intégré | supprime l'index dans Pinecone, ou change `PINECONE_INDEX_NAME`, puis relance l'installation |
| Réponses lentes (30 s et +) | démarrage à froid + RAG + réflexion | normal pour `/detail` ; `/solve` et `/hint` sont plus rapides |
| Friedrich apparaît hors ligne | aucune connexion Gateway (c'est l'architecture voulue) | rien à faire : les commandes slash fonctionnent quand même |
| Déploiement refusé : *function size exceeded* | `matplotlib` + `numpy` sont volumineux | retire `matplotlib` et `Pillow` de `requirements.txt` : le tracé de courbes se désactive proprement, tout le reste continue de marcher |
| J'ai atteint mon quota Gemini | quota de la clé 1 épuisé | la clé 2 prend le relais automatiquement ; sinon attends la remise à zéro |
| `no longer available to new users` / `404 NOT_FOUND` sur un modèle | Google a fermé ce modèle aux nouveaux comptes (arrivé à `gemini-2.5-flash` le 31/08/2026) | rien à faire : Friedrich lit le remplaçant cité dans le message d'erreur, bascule dessus et continue. Si tu avais épinglé `GEMINI_MODEL`, efface la variable ou mets le modèle recommandé. Liste vide ? Ta clé n'a pas accès à l'API Generative Language : recrée-la sur <https://aistudio.google.com/app/apikey> |
| `RESOURCE_EXHAUSTED` / `max tokens per minute (250000)` pendant l'ingestion | plafond d'embedding de l'offre gratuite Pinecone | rien à faire : la page attend une minute puis reprend toute seule à l'extrait exact où elle s'était arrêtée |
| `Les serveurs Gemini refusent la requête (503, « surchargé »)` | panne passagère chez Google — fréquente sur les alias `-latest` et les modèles *preview* | c'est bien temporaire : Friedrich réessaie sur les deux clés **puis sur d'autres modèles** avant d'abandonner, et le message nomme les modèles essayés. Si ça dure plus de quelques minutes, épingle un modèle stable dans `GEMINI_MODEL` |

---

## 🎓 Personnaliser Friedrich

- **Son ton, ses règles, ses phrases imposées** → `utils/prompts.py`
  (`SYSTEM_PROMPT`, `OFF_TOPIC_REPLY`, `OUT_OF_SYLLABUS_SENTENCE`,
  `MODE_INSTRUCTIONS`, `HELP_TEXT`).
- **Le contenu du programme** → remplace les PDF puis, en local :

  ```bash
  pip install pypdf
  python ingest.py --extraire "C:/Users/augus/Documents/data"
  ```

  → regénère `data/corpus.jsonl.gz` ; redéploie et relance `/setup.html`
  (bouton **♻️ Réingérer le programme**).
- **Le modèle** → variable `GEMINI_MODEL`. Laisse-la vide : Friedrich part de
  l'alias `gemini-flash-latest` et, si l'API le refuse, bascule tout seul sur
  un modèle réellement servi (en suivant le remplaçant que Google cite dans
  son message d'erreur).
- **La finesse du RAG** → `RAG_TOP_K` (candidats) et `RAG_TOP_N` (extraits
  gardés après reranking).

### D'où vient `data/corpus.jsonl.gz` ?

Les 6 PDF sources pèsent **293 Mo** : impossible à embarquer dans une fonction
Vercel (limite de 250 Mo, PDF compris). Le texte a donc été extrait **page par
page avec `pypdf`**, nettoyé, découpé en blocs de **1200 caractères avec 200 de
chevauchement**, et enregistré une fois pour toutes dans un corpus compressé de
**1,5 Mo** — le même découpage que celui décrit dans `utils/rag.py`. Le
déploiement reste léger, et l'ingestion Pinecone devient rapide et fiable.

| Document | Extraits |
|---|---|
| `JW • Tle Spé Maths.pdf` | 2 157 |
| `mstsspe_2020_v3.pdf` | 1 508 |
| `JW • Tle Maths Expertes.pdf` | 1 223 |
| `mstsexp_2020_v3.pdf` | 753 |
| `Correction_Poly_Transition_Prepa_LLG_H4.pdf` | 674 |
| `programme_terminal.pdf` | 407 |
| **Total** | **6 722** |

---

## 🔒 Sécurité

- Aucune clé n'est écrite dans le code : tout vient des variables
  d'environnement. `.env` est ignoré par git (`.gitignore`).
- Chaque requête Discord est vérifiée (**Ed25519**, en-têtes
  `X-Signature-Ed25519` / `X-Signature-Timestamp`) ; une signature invalide
  reçoit un `401`.
- `/api/setup` est protégé par `SETUP_SECRET` (comparaison à temps constant).
- `/api/task` n'est joignable qu'avec une **signature HMAC interne** horodatée :
  personne d'extérieur ne peut la déclencher.
- Taille des corps HTTP limitée (512 Ko) ; images limitées à **10 Mo** et
  refusées si le type MIME n'est pas `image/*` ; téléchargement restreint aux
  domaines Discord.
- `allowed_mentions: {"parse": []}` sur **tous** les messages : Friedrich ne
  peut jamais mentionner `@everyone`.
- Les journaux ne contiennent ni clé, ni jeton, ni contenu d'élève — seulement
  un petit rapport technique.

---

## 💸 Coûts et limites

- **Vercel Hobby** : gratuit, `maxDuration` plafonné à 60 s (respecté ici).
- **Gemini** : offre gratuite généreuse sur les modèles « flash », avec bascule
  automatique sur la seconde clé.
- **Pinecone Starter** : gratuit, largement suffisant pour 6 722 extraits.
- La **mémoire de conversation** est volontairement éphémère (`/tmp` de
  l'instance) : aucune donnée d'élève n'est stockée durablement.
