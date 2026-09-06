"""RAG : extraction des PDF, corpus, et recherche Pinecone.

Chaîne complète :
  PDF  --pypdf-->  texte page par page  --découpage-->  corpus JSONL compressé
  corpus  --Pinecone (llama-text-embed-v2)-->  index vectoriel
  question  --recherche + reranking (bge-reranker-v2-m3)-->  CONTEXTE pour Gemini

L'ingestion est IDEMPOTENTE : les identifiants sont déterministes
(`source#pPAGE#cINDEX`), donc relancer l'ingestion écrase les mêmes vecteurs
au lieu d'en créer de nouveaux. Une fiche « manifeste » enregistre en plus
l'empreinte du corpus déjà ingéré pour pouvoir sauter le travail inutile.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import time
import unicodedata

from . import config
from .formatting import clean_pdf_text

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
CORPUS_PATH = os.path.join(DATA_DIR, "corpus.jsonl.gz")
CORPUS_META_PATH = os.path.join(DATA_DIR, "corpus.meta.json")
MANIFEST_ID = "__friedrich_manifest__"
PROGRESS_ID = "__friedrich_progress__"

TEXT_FIELD = "chunk_text"


# --------------------------------------------------------------------------
# Outils texte
# --------------------------------------------------------------------------

def slugify(name: str) -> str:
    """Identifiant court, stable et lisible pour une source."""
    base = os.path.splitext(os.path.basename(str(name)))[0]
    base = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode()
    base = re.sub(r"[^A-Za-z0-9]+", "-", base).strip("-").lower()
    return base[:48] or "source"


def chunk_text(text: str, size: int | None = None, overlap: int | None = None) -> list[str]:
    """Découpe un texte en blocs de `size` caractères avec `overlap` de recouvrement."""
    size = size or config.CHUNK_SIZE
    overlap = overlap or config.CHUNK_OVERLAP
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    step = max(1, size - overlap)
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            # on préfère couper à une frontière naturelle
            window = text[start:end]
            for sep in ("\n\n", "\n", ". ", " "):
                cut = window.rfind(sep)
                if cut > size * 0.55:
                    end = start + cut + len(sep)
                    break
        piece = text[start:end].strip()
        if len(piece) > 60:            # on jette les miettes
            chunks.append(piece)
        if end >= len(text):
            break
        start = max(start + step, end - overlap)
    return chunks


def extract_pdf(path: str) -> list[tuple[int, str]]:
    """Extrait le texte page par page d'un PDF (pypdf), nettoyé."""
    from pypdf import PdfReader

    reader = PdfReader(path)
    pages: list[tuple[int, str]] = []
    for number, page in enumerate(reader.pages, start=1):
        try:
            raw = page.extract_text() or ""
        except Exception:
            raw = ""
        cleaned = clean_pdf_text(raw)
        if len(cleaned) >= 120:        # on ignore couvertures et pages d'images
            pages.append((number, cleaned))
    return pages


def build_records(path: str, collection: str = "") -> list[dict]:
    """Transforme un PDF en fiches prêtes pour Pinecone (ids déterministes)."""
    slug = slugify(path)
    label = os.path.basename(path)
    records: list[dict] = []
    for page_number, text in extract_pdf(path):
        for index, piece in enumerate(chunk_text(text)):
            records.append(
                {
                    "_id": f"{slug}#p{page_number}#c{index}",
                    TEXT_FIELD: piece,
                    "page": page_number,
                    "source": label,
                    "collection": collection or slug,
                }
            )
    return records


# --------------------------------------------------------------------------
# Corpus (fichier embarqué dans le déploiement)
# --------------------------------------------------------------------------

def write_corpus(records: list[dict], path: str = CORPUS_PATH) -> dict:
    """Écrit le corpus compressé + son fichier de métadonnées."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    digest = hashlib.sha256()
    sources: dict[str, int] = {}
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=9) as handle:
        for record in records:
            line = json.dumps(record, ensure_ascii=False)
            handle.write(line + "\n")
            digest.update(line.encode("utf-8"))
            sources[record.get("source", "?")] = sources.get(record.get("source", "?"), 0) + 1
    meta = {
        "total": len(records),
        "sources": sources,
        "fingerprint": digest.hexdigest()[:32],
        "chunk_size": config.CHUNK_SIZE,
        "chunk_overlap": config.CHUNK_OVERLAP,
        "genere_le": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(CORPUS_META_PATH, "w", encoding="utf-8") as handle:
        json.dump(meta, handle, ensure_ascii=False, indent=2)
    return meta


def corpus_meta(path: str = CORPUS_META_PATH) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return {}


def corpus_available() -> bool:
    return os.path.exists(CORPUS_PATH)


def read_corpus_slice(offset: int, limit: int, path: str = CORPUS_PATH) -> list[dict]:
    """Lit `limit` fiches du corpus à partir de `offset` (sans tout charger)."""
    records: list[dict] = []
    if not os.path.exists(path):
        return records
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for index, line in enumerate(handle):
            if index < offset:
                continue
            if len(records) >= limit:
                break
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records


# --------------------------------------------------------------------------
# Pinecone
# --------------------------------------------------------------------------

_client = None
_index = None


def client():
    """Client Pinecone (mis en cache pour réutiliser la connexion à chaud)."""
    global _client
    if _client is None:
        from pinecone import Pinecone

        _client = Pinecone(api_key=config.require("PINECONE_API_KEY"))
    return _client


def index_exists() -> bool:
    pc = client()
    name = config.PINECONE_INDEX_NAME()
    try:
        return bool(pc.has_index(name))
    except AttributeError:
        names = []
        for item in pc.list_indexes():
            names.append(item["name"] if isinstance(item, dict) else getattr(item, "name", ""))
        return name in names


def ensure_index(timeout: int = 90) -> dict:
    """Crée si besoin un index Pinecone à embedding intégré, puis attend qu'il soit prêt."""
    pc = client()
    name = config.PINECONE_INDEX_NAME()
    created = False
    if not index_exists():
        pc.create_index_for_model(
            name=name,
            cloud=config.PINECONE_CLOUD(),
            region=config.PINECONE_ENV(),
            embed={
                "model": config.PINECONE_EMBED_MODEL(),
                "field_map": {"text": TEXT_FIELD},
            },
        )
        created = True

    deadline = time.time() + timeout
    ready = False
    while time.time() < deadline:
        try:
            description = pc.describe_index(name)
            status = description.get("status") if isinstance(description, dict) else description.status
            ready = bool(status["ready"] if isinstance(status, dict) else getattr(status, "ready", False))
        except Exception:
            ready = False
        if ready:
            break
        time.sleep(2)

    return {"index": name, "cree": created, "pret": ready,
            "modele_embedding": config.PINECONE_EMBED_MODEL(),
            "region": config.PINECONE_ENV()}


def get_index():
    global _index
    if _index is None:
        _index = client().Index(config.PINECONE_INDEX_NAME())
    return _index


class RateLimited(RuntimeError):
    """Quota d'embedding Pinecone atteint (250 000 tokens/min en offre gratuite).

    Porte le nombre de fiches déjà envoyées : l'appelant reprend exactement là.
    """

    def __init__(self, envoyes: int, detail: str = ""):
        super().__init__(detail or "limite de débit Pinecone atteinte")
        self.envoyes = envoyes


_MARQUEURS_DEBIT = (
    "429", "resource_exhausted", "ratelimit", "rate limit",
    "max tokens per minute", "too many requests", "quota",
)


def _est_limite_debit(exc: Exception) -> bool:
    return any(marqueur in f"{type(exc).__name__} {exc}".lower()
               for marqueur in _MARQUEURS_DEBIT)


def estimate_tokens(records: list[dict]) -> int:
    """Estimation grossière des tokens facturés à l'embedding (~4 car./token)."""
    return sum(len(str(f.get(TEXT_FIELD) or "")) // 4 + 8 for f in records)


def upsert_records(records: list[dict]) -> int:
    """Envoie des fiches (embedding calculé par Pinecone) — idempotent.

    Lève `RateLimited` si le quota d'embedding est atteint, en indiquant
    combien de fiches sont réellement passées.
    """
    if not records:
        return 0
    index = get_index()
    namespace = config.PINECONE_NAMESPACE()
    sent = 0
    batch_size = max(1, min(config.UPSERT_BATCH, 95))
    for start in range(0, len(records), batch_size):
        batch = records[start:start + batch_size]
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                # arguments nommés : compatible pinecone v6 → v9 (keyword-only)
                index.upsert_records(namespace=namespace, records=batch)
                sent += len(batch)
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                if _est_limite_debit(exc):
                    # inutile d'insister : la fenêtre de quota est à la minute
                    raise RateLimited(sent, str(exc)[:200]) from exc
                time.sleep(1.5 * (attempt + 1))     # incident réseau passager
        if last_error is not None:
            raise RuntimeError(
                f"échec de l'envoi vers Pinecone après 3 tentatives : "
                f"{type(last_error).__name__}: {last_error}"
            )
    return sent


def stats() -> dict:
    """Statistiques de l'index (nombre de vecteurs par namespace)."""
    try:
        raw = get_index().describe_index_stats()
        data = raw.to_dict() if hasattr(raw, "to_dict") else dict(raw)
        namespaces = data.get("namespaces") or {}
        ours = namespaces.get(config.PINECONE_NAMESPACE()) or {}
        return {
            "total_vecteurs": data.get("total_vector_count"),
            "namespace": config.PINECONE_NAMESPACE(),
            "vecteurs_namespace": ours.get("record_count") or ours.get("vector_count"),
        }
    except Exception as exc:
        return {"erreur": f"{type(exc).__name__}: {exc}"}


def read_manifest() -> dict | None:
    """Empreinte du corpus déjà ingéré (None si absente)."""
    try:
        raw = get_index().fetch(ids=[MANIFEST_ID], namespace=config.PINECONE_NAMESPACE())
        data = raw.to_dict() if hasattr(raw, "to_dict") else dict(raw)
        vectors = data.get("vectors") or data.get("records") or {}
        entry = vectors.get(MANIFEST_ID)
        if not entry:
            return None
        fields = entry.get("metadata") or entry.get("fields") or {}
        return dict(fields)
    except Exception:
        return None


def read_progress() -> dict | None:
    """Où en était l'ingestion (permet de reprendre après un rechargement)."""
    try:
        raw = get_index().fetch(ids=[PROGRESS_ID], namespace=config.PINECONE_NAMESPACE())
        data = raw.to_dict() if hasattr(raw, "to_dict") else dict(raw)
        vectors = data.get("vectors") or data.get("records") or {}
        entry = vectors.get(PROGRESS_ID)
        if not entry:
            return None
        return dict(entry.get("metadata") or entry.get("fields") or {})
    except Exception:
        return None


def write_progress(fingerprint: str, offset: int) -> None:
    """Enregistre l'avancement (fiche technique, filtrée à la recherche)."""
    try:
        get_index().upsert_records(
            namespace=config.PINECONE_NAMESPACE(),
            records=[{
                "_id": PROGRESS_ID,
                TEXT_FIELD: "avancement technique friedrich (ne pas utiliser comme cours)",
                "fingerprint": str(fingerprint),
                "offset": int(offset),
                "source": "__manifest__",
                "collection": "__manifest__",
                "page": 0,
            }],
        )
    except Exception:
        pass


def write_manifest(fingerprint: str, total: int) -> None:
    """Marque le corpus comme ingéré (fiche technique, filtrée à la recherche)."""
    try:
        upsert_records([{
            "_id": MANIFEST_ID,
            TEXT_FIELD: "manifeste technique friedrich (ne pas utiliser comme cours)",
            "fingerprint": str(fingerprint),
            "total": int(total),
            "source": "__manifest__",
            "collection": "__manifest__",
            "page": 0,
        }])
    except Exception:
        pass


# --------------------------------------------------------------------------
# Recherche
# --------------------------------------------------------------------------

def _hits_from(response) -> list[dict]:
    data = response.to_dict() if hasattr(response, "to_dict") else response
    if not isinstance(data, dict):
        return []
    result = data.get("result") or {}
    hits = result.get("hits") or data.get("hits") or []
    return hits if isinstance(hits, list) else []


def _valeur(source, *cles):
    """Lit la première clé présente (le SDK nomme tantôt `_id`, tantôt `id_`)."""
    for cle in cles:
        try:
            valeur = source.get(cle)
        except AttributeError:
            valeur = getattr(source, cle, None)
        if valeur is not None:
            return valeur
    return None


def search(query: str, top_k: int | None = None, top_n: int | None = None) -> list[dict]:
    """Recherche sémantique + reranking. Ne lève jamais : renvoie [] en cas d'échec."""
    query = (query or "").strip()
    if not query or not config.get("PINECONE_API_KEY"):
        return []
    query = query[:900]
    top_k = top_k or config.RAG_TOP_K()
    top_n = top_n or config.RAG_TOP_N()

    namespace = config.PINECONE_NAMESPACE()
    champs = [TEXT_FIELD, "page", "source", "collection"]
    rerank = {
        "model": config.PINECONE_RERANK_MODEL(),
        "top_n": top_n,
        "rank_fields": [TEXT_FIELD],
    }

    try:
        index = get_index()
        searcher = getattr(index, "search", None) or getattr(index, "search_records", None)
        if searcher is None:
            return []

        # pinecone v9 : arguments à plat ; v6/v7 : ancien paramètre `query`
        tentatives = [
            {"namespace": namespace, "top_k": top_k, "inputs": {"text": query},
             "fields": champs, "rerank": rerank},
            {"namespace": namespace, "query": {"inputs": {"text": query}, "top_k": top_k},
             "fields": champs, "rerank": rerank},
            {"namespace": namespace, "query": {"inputs": {"text": query}, "top_k": top_k},
             "fields": champs},                       # sans reranking, en dernier recours
        ]
        response = None
        derniere: Exception | None = None
        for arguments in tentatives:
            try:
                response = searcher(**arguments)
                break
            except TypeError as exc:                  # signature incompatible : on essaie l'autre
                derniere = exc
            except Exception as exc:
                derniere = exc
        if response is None:
            raise derniere or RuntimeError("recherche impossible")
    except Exception:
        return []

    results: list[dict] = []
    for hit in _hits_from(response):
        fields = _valeur(hit, "fields") or {}
        text = str(fields.get(TEXT_FIELD) or "").strip()
        if not text or fields.get("collection") == "__manifest__":
            continue
        results.append({
            "id": _valeur(hit, "_id", "id", "id_"),
            "score": _valeur(hit, "_score", "score", "score_"),
            "texte": text,
            "page": fields.get("page"),
            "source": fields.get("source"),
        })
    return results


def format_context(hits: list[dict], max_chars: int = 9000) -> str:
    """Assemble les extraits retrouvés en un bloc CONTEXTE lisible par Gemini."""
    if not hits:
        return ""
    lines: list[str] = []
    total = 0
    for number, hit in enumerate(hits, start=1):
        source = hit.get("source") or "programme"
        page = hit.get("page")
        entete = f"[Extrait {number} — {source}" + (f", page {page}]" if page else "]")
        bloc = f"{entete}\n{hit['texte']}"
        if total + len(bloc) > max_chars:
            break
        lines.append(bloc)
        total += len(bloc)
    return "\n\n".join(lines)


def build_query(question: str | None, transcription: str | None = None) -> str:
    """Construit la requête envoyée à Pinecone (texte élève + transcription image)."""
    morceaux = [p.strip() for p in (question, transcription) if p and p.strip()]
    return " ".join(morceaux)[:900]
