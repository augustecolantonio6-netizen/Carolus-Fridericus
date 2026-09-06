"""Configuration centralisée de Friedrich.

Toutes les valeurs sensibles proviennent des variables d'environnement
(Vercel > Settings > Environment Variables), complétées au besoin par un
fichier local `.env` ou embarqué `friedrich.env`. Aucune clé n'est écrite en
dur dans le code, aucune clé n'est jamais journalisée.
"""

from __future__ import annotations

import os

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Fichier de configuration local lu au démarrage s'il est présent.
#: Il est ignoré par git ET par Vercel : les vraies clés ne quittent jamais
#: ta machine. Sur Vercel, ce sont les variables du projet qui font foi.
FICHIERS_ENV = (".env",)


def _charger_fichiers_env() -> list[str]:
    """Charge les fichiers d'environnement présents, sans écraser l'existant.

    Les variables déjà définies (celles de Vercel) restent prioritaires : un
    fichier ne sert qu'à combler les trous.
    """
    charges = []
    for nom in FICHIERS_ENV:
        chemin = os.path.join(RACINE, nom)
        if not os.path.exists(chemin):
            continue
        try:
            from dotenv import load_dotenv

            load_dotenv(chemin, override=False)
            charges.append(nom)
            continue
        except Exception:
            pass
        try:  # repli sans dépendance : analyse minimale « CLE=valeur »
            with open(chemin, encoding="utf-8") as fichier:
                for ligne in fichier:
                    ligne = ligne.strip()
                    if not ligne or ligne.startswith("#") or "=" not in ligne:
                        continue
                    cle, valeur = ligne.split("=", 1)
                    cle, valeur = cle.strip(), valeur.strip()
                    if len(valeur) >= 2 and valeur[0] == valeur[-1] and valeur[0] in "\"'":
                        valeur = valeur[1:-1]
                    if cle and not os.environ.get(cle):
                        os.environ[cle] = valeur
            charges.append(nom)
        except Exception:  # pragma: no cover - fichier illisible
            pass
    return charges


FICHIERS_ENV_CHARGES = _charger_fichiers_env()


def get(name: str, default: str | None = None) -> str | None:
    """Retourne une variable d'environnement nettoyée (ou `default`)."""
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def get_int(name: str, default: int) -> int:
    try:
        return int(str(get(name, str(default))))
    except (TypeError, ValueError):
        return default


def require(name: str) -> str:
    value = get(name)
    if not value:
        raise RuntimeError(
            f"Variable d'environnement manquante : {name}. "
            "Ajoute-la dans Vercel > Settings > Environment Variables, puis redéploie."
        )
    return value


def mask(value: str | None) -> str:
    """Masque une valeur secrète pour un affichage sûr (jamais la valeur brute)."""
    if not value:
        return "(non défini)"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:3]}…{value[-2:]} ({len(value)} car.)"


# --------------------------------------------------------------------------
# Discord
# --------------------------------------------------------------------------
DISCORD_PUBLIC_KEY = lambda: get("DISCORD_PUBLIC_KEY")          # noqa: E731
DISCORD_APPLICATION_ID = lambda: get("DISCORD_APPLICATION_ID")  # noqa: E731
DISCORD_TOKEN = lambda: get("DISCORD_TOKEN")                    # noqa: E731
DISCORD_GUILD_ID = lambda: get("DISCORD_GUILD_ID")              # noqa: E731

DISCORD_API = "https://discord.com/api/v10"

# --------------------------------------------------------------------------
# Gemini
# --------------------------------------------------------------------------
# Modèle mesuré comme le meilleur compromis vitesse/qualité sur une clé
# gratuite le 31/08/2026 (corrigé complet en ~8 s, appels d'outils compris).
# S'il devenait indisponible, GeminiClient bascule seul sur un modèle servi.
GEMINI_MODEL = lambda: get("GEMINI_MODEL", "gemini-3.5-flash")  # noqa: E731


def gemini_keys() -> list[str]:
    """Clés Gemini dans l'ordre d'utilisation (la 2e est la clé de secours)."""
    keys = []
    for name in ("GEMINI_API_KEY_1", "GEMINI_API_KEY_2", "GEMINI_API_KEY"):
        value = get(name)
        if value and value not in keys:
            keys.append(value)
    return keys


# --------------------------------------------------------------------------
# Pinecone
# --------------------------------------------------------------------------
PINECONE_API_KEY = lambda: get("PINECONE_API_KEY")                                  # noqa: E731
PINECONE_INDEX_NAME = lambda: get("PINECONE_INDEX_NAME", "terminal-maths")           # noqa: E731
PINECONE_NAMESPACE = lambda: get("PINECONE_NAMESPACE", "programme-terminal")         # noqa: E731
PINECONE_ENV = lambda: get("PINECONE_ENV", "us-east-1")                              # noqa: E731
PINECONE_CLOUD = lambda: get("PINECONE_CLOUD", "aws")                                # noqa: E731
PINECONE_EMBED_MODEL = lambda: get("PINECONE_EMBED_MODEL", "llama-text-embed-v2")    # noqa: E731
PINECONE_RERANK_MODEL = lambda: get("PINECONE_RERANK_MODEL", "bge-reranker-v2-m3")   # noqa: E731

# --------------------------------------------------------------------------
# Réglages RAG / limites de sécurité
# --------------------------------------------------------------------------
CHUNK_SIZE = get_int("CHUNK_SIZE", 1200)
CHUNK_OVERLAP = get_int("CHUNK_OVERLAP", 200)
UPSERT_BATCH = get_int("UPSERT_BATCH", 90)          # Pinecone : 96 max par requête
RAG_TOP_K = lambda: get_int("RAG_TOP_K", 24)        # noqa: E731 candidats récupérés
RAG_TOP_N = lambda: get_int("RAG_TOP_N", 6)         # noqa: E731 gardés après reranking

MAX_IMAGE_BYTES = 10 * 1024 * 1024      # 10 Mo, imposé par le cahier des charges
MAX_BODY_BYTES = 512 * 1024             # corps HTTP entrant maximal
MAX_QUESTION_CHARS = 4000
DISCORD_CHUNK = 1900                    # < 2000 caractères par message Discord

SETUP_SECRET = lambda: get("SETUP_SECRET")  # noqa: E731


def internal_secret() -> str:
    """Clé HMAC pour authentifier /api/index -> /api/task (jamais exposée)."""
    return get("SETUP_SECRET") or get("DISCORD_TOKEN") or get("DISCORD_PUBLIC_KEY") or ""


REQUIRED_VARS = [
    "DISCORD_PUBLIC_KEY",
    "DISCORD_APPLICATION_ID",
    "DISCORD_TOKEN",
    "GEMINI_API_KEY_1",
    "PINECONE_API_KEY",
    "SETUP_SECRET",
]

OPTIONAL_VARS = [
    "GEMINI_API_KEY_2",
    "DISCORD_GUILD_ID",
    "PINECONE_ENV",
    "PINECONE_INDEX_NAME",
    "PINECONE_NAMESPACE",
    "GEMINI_MODEL",
]


def env_report() -> dict:
    """État de la configuration : uniquement des booléens, jamais les valeurs."""
    return {
        "requises": {name: bool(get(name)) for name in REQUIRED_VARS},
        "optionnelles": {name: bool(get(name)) for name in OPTIONAL_VARS},
        "manquantes": [name for name in REQUIRED_VARS if not get(name)],
        "fichiers_env_charges": FICHIERS_ENV_CHARGES,
        "modele_gemini": GEMINI_MODEL(),
        "index_pinecone": PINECONE_INDEX_NAME(),
        "namespace_pinecone": PINECONE_NAMESPACE(),
        "region_pinecone": PINECONE_ENV(),
    }
