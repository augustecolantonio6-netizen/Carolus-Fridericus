"""Mémoire courte de conversation (best-effort, sans base de données).

Une fonction serverless n'a pas de disque permanent : on stocke les derniers
échanges dans le dossier temporaire de l'instance (`/tmp` sur Vercel). La
mémoire survit donc aux appels rapprochés servis par la même instance
« chaude », et disparaît ensuite. C'est volontaire : aucune donnée d'élève
n'est conservée durablement, et aucun service externe n'est nécessaire.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time

TTL_SECONDS = 30 * 60          # 30 minutes
MAX_TOURS = 3                  # nombre d'échanges gardés
MAX_CHARS = 700                # par message stocké

_DIR = os.path.join(tempfile.gettempdir(), "friedrich-memoire")


def _path(user_id: str, channel_id: str) -> str:
    clef = hashlib.sha256(f"{user_id}:{channel_id}".encode("utf-8")).hexdigest()[:24]
    return os.path.join(_DIR, f"{clef}.json")


def _ensure_dir() -> bool:
    try:
        os.makedirs(_DIR, exist_ok=True)
        return True
    except Exception:
        return False


def load(user_id: str, channel_id: str) -> list[dict]:
    """Derniers échanges (liste vide si rien, jamais d'exception)."""
    try:
        chemin = _path(user_id, channel_id)
        if not os.path.exists(chemin):
            return []
        if time.time() - os.path.getmtime(chemin) > TTL_SECONDS:
            os.remove(chemin)
            return []
        with open(chemin, encoding="utf-8") as handle:
            data = json.load(handle)
        return data.get("tours", []) if isinstance(data, dict) else []
    except Exception:
        return []


def append(user_id: str, channel_id: str, mode: str, question: str, reponse: str) -> None:
    """Ajoute un échange (silencieux en cas d'échec : la mémoire est un bonus)."""
    if not _ensure_dir():
        return
    try:
        tours = load(user_id, channel_id)
        tours.append({
            "mode": mode,
            "eleve": (question or "")[:MAX_CHARS],
            "friedrich": (reponse or "")[:MAX_CHARS],
            "t": int(time.time()),
        })
        tours = tours[-MAX_TOURS:]
        with open(_path(user_id, channel_id), "w", encoding="utf-8") as handle:
            json.dump({"tours": tours}, handle, ensure_ascii=False)
    except Exception:
        pass


def format_history(tours: list[dict]) -> str:
    """Met en forme l'historique pour le prompt (vide si aucun échange)."""
    if not tours:
        return ""
    lignes = []
    for tour in tours[-MAX_TOURS:]:
        eleve = (tour.get("eleve") or "").strip()
        friedrich = (tour.get("friedrich") or "").strip()
        if eleve:
            lignes.append(f"Élève (/{tour.get('mode', '?')}) : {eleve}")
        if friedrich:
            lignes.append(f"Toi : {friedrich[:400]}")
    return "\n".join(lignes)


def clear(user_id: str, channel_id: str) -> None:
    try:
        chemin = _path(user_id, channel_id)
        if os.path.exists(chemin):
            os.remove(chemin)
    except Exception:
        pass
