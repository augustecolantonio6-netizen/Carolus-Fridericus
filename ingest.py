#!/usr/bin/env python3
"""Ingestion du programme dans Pinecone — utilisable en local (facultatif).

Tu n'as PAS besoin de ce script pour utiliser Friedrich : la page
`/setup.html` fait tout depuis le navigateur. Il sert à deux choses :

  1. Régénérer le corpus texte à partir de tes PDF :
        python ingest.py --extraire "C:/Users/augus/Documents/data"
     -> écrit data/corpus.jsonl.gz (embarqué dans le déploiement Vercel)

  2. Pousser ce corpus dans Pinecone sans passer par le navigateur :
        python ingest.py --envoyer

L'opération est idempotente : les identifiants sont déterministes
(`source#pPAGE#cINDEX`), relancer n'ajoute donc aucun doublon.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils import config, rag  # noqa: E402

DOSSIER_PDF_PAR_DEFAUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def extraire(dossier: str) -> int:
    """Lit tous les PDF d'un dossier et fabrique data/corpus.jsonl.gz."""
    if not os.path.isdir(dossier):
        print(f"❌ Dossier introuvable : {dossier}")
        return 1

    pdfs = sorted(
        os.path.join(dossier, nom)
        for nom in os.listdir(dossier)
        if nom.lower().endswith(".pdf")
    )
    if not pdfs:
        print(f"❌ Aucun PDF dans {dossier}")
        return 1

    toutes: list[dict] = []
    for chemin in pdfs:
        debut = time.time()
        print(f"📄 {os.path.basename(chemin)} …", flush=True)
        try:
            fiches = rag.build_records(chemin)
        except Exception as exc:
            print(f"   ⚠️  ignoré ({type(exc).__name__}: {exc})")
            continue
        toutes.extend(fiches)
        print(f"   → {len(fiches)} extraits en {time.time() - debut:.1f}s")

    if not toutes:
        print("❌ Aucun texte extrait (PDF scannés sans couche texte ?)")
        return 1

    meta = rag.write_corpus(toutes)
    taille = os.path.getsize(rag.CORPUS_PATH) / 1e6
    print(f"\n✅ Corpus écrit : {rag.CORPUS_PATH} ({taille:.1f} Mo)")
    print(f"   {meta['total']} extraits — empreinte {meta['fingerprint']}")
    for source, nombre in meta["sources"].items():
        print(f"   • {source} : {nombre}")
    return 0


def envoyer(force: bool) -> int:
    """Envoie le corpus dans Pinecone, par lots, en reprenant proprement."""
    if not config.get("PINECONE_API_KEY"):
        print("❌ PINECONE_API_KEY manquante (mets-la dans un fichier .env local).")
        return 1
    if not rag.corpus_available():
        print("❌ data/corpus.jsonl.gz absent : lance d'abord --extraire.")
        return 1

    meta = rag.corpus_meta()
    total = int(meta.get("total") or 0)
    print(f"🔧 Index « {config.PINECONE_INDEX_NAME()} » ({config.PINECONE_ENV()}) …")
    etat = rag.ensure_index()
    print(f"   {'créé' if etat['cree'] else 'déjà présent'} — prêt : {etat['pret']}")

    if not force:
        manifeste = rag.read_manifest() or {}
        if str(manifeste.get("fingerprint") or "") == str(meta.get("fingerprint")):
            print("✅ Corpus déjà ingéré (identique). Utilise --force pour recommencer.")
            return 0

    envoyes = 0
    debut = time.time()
    while envoyes < total:
        fiches = rag.read_corpus_slice(envoyes, 180)
        if not fiches:
            break
        rag.upsert_records(fiches)
        envoyes += len(fiches)
        pourcent = 100 * envoyes / max(1, total)
        print(f"   {envoyes}/{total} ({pourcent:.0f} %)", end="\r", flush=True)

    rag.write_manifest(str(meta.get("fingerprint")), total)
    print(f"\n✅ {envoyes} extraits indexés en {time.time() - debut:.0f}s")
    print(f"   {rag.stats()}")
    return 0


def main() -> int:
    parseur = argparse.ArgumentParser(description="Ingestion Friedrich (facultatif)")
    parseur.add_argument("--extraire", nargs="?", const=DOSSIER_PDF_PAR_DEFAUT,
                         metavar="DOSSIER_PDF",
                         help="fabrique data/corpus.jsonl.gz depuis un dossier de PDF")
    parseur.add_argument("--envoyer", action="store_true",
                         help="envoie data/corpus.jsonl.gz dans Pinecone")
    parseur.add_argument("--force", action="store_true",
                         help="réingère même si l'empreinte est identique")
    arguments = parseur.parse_args()

    if not arguments.extraire and not arguments.envoyer:
        parseur.print_help()
        return 0

    code = 0
    if arguments.extraire:
        code = extraire(arguments.extraire)
    if code == 0 and arguments.envoyer:
        code = envoyer(arguments.force)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
