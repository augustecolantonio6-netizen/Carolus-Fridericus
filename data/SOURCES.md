# Sources du corpus

`corpus.jsonl.gz` est la version **texte, nettoyée et découpée** des PDF de
programme et de manuels de Terminale. C'est ce fichier qui est déployé sur
Vercel puis vectorisé dans Pinecone.

| Document source | Pages retenues → extraits |
|---|---|
| `programme_terminal.pdf` — programme officiel 2020 (spécialité, fiches de cours et d'exercices) | 407 |
| `JW • Tle Spé Maths.pdf` — manuel spécialité Mathématiques | 2 157 |
| `JW • Tle Maths Expertes.pdf` — manuel Mathématiques Expertes | 1 223 |
| `mstsspe_2020_v3.pdf` — manuel spécialité | 1 508 |
| `mstsexp_2020_v3.pdf` — manuel maths expertes | 753 |
| `Correction_Poly_Transition_Prepa_LLG_H4.pdf` — corrigés transition prépa | 674 |
| **Total** | **6 722** |

## Pourquoi le texte et pas les PDF ?

Les 6 PDF pèsent **293 Mo**. Une fonction Vercel est limitée à 250 Mo (code +
dépendances + fichiers). Le texte extrait, lui, tient en **1,5 Mo** compressé :
le déploiement reste léger et l'ingestion Pinecone devient rapide et fiable.

## Comment le régénérer

Depuis la racine du projet, avec les PDF dans un dossier :

```bash
pip install pypdf
python ingest.py --extraire "C:/Users/augus/Documents/data"
```

Traitement appliqué (identique à celui décrit dans `utils/rag.py`) :

1. extraction **page par page** avec `pypdf` ;
2. nettoyage : espaces multiples, ligatures, mots coupés en fin de ligne,
   numéros de page isolés ; pages de moins de 120 caractères ignorées ;
3. découpage en blocs de **1200 caractères** avec **200 de chevauchement**,
   en coupant de préférence sur une frontière naturelle (paragraphe, phrase) ;
4. identifiant **déterministe** par bloc : `source#pPAGE#cINDEX` — c'est ce qui
   rend l'ingestion idempotente (relancer écrase, n'ajoute jamais de doublon) ;
5. écriture de `corpus.jsonl.gz` + `corpus.meta.json` (empreinte SHA-256 du
   corpus, comptages, paramètres de découpage).

Ensuite : redéploie sur Vercel, ouvre `/setup.html` et clique sur
**♻️ Réingérer le programme**.
