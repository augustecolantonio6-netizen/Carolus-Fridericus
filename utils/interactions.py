"""Cœur logique des interactions Discord (testable sans serveur).

`api/index.py` n'est qu'une coquille HTTP : toute la logique (vérification de
signature, validation des entrées, routage des commandes) vit ici.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import socket
import time
import urllib.error
import urllib.request

from . import config, discord_api, prompts

# Types d'interactions Discord
PING = 1
APPLICATION_COMMAND = 2

# Types de réponses Discord
PONG = 1
CHANNEL_MESSAGE = 4
DEFERRED_CHANNEL_MESSAGE = 5

MODES = ("hint", "detail", "solve", "correct")

MESSAGE_SANS_CONTENU = (
    "Il me faut au moins quelque chose à me mettre sous la dent 🙂\n"
    "Écris ta question dans `question:` **ou** joins une photo dans `image:`.\n"
    "Exemple : `/detail question: Résous x² - 4 = 0`"
)

MESSAGE_FICHIER_INVALIDE = (
    "Je ne sais lire que des **images** (png, jpg, webp, heic…).\n"
    "Ton fichier « {nom} » n'en est pas une ({type}). Prends plutôt une photo "
    "ou une capture d'écran de ton exercice."
)

MESSAGE_IMAGE_TROP_LOURDE = (
    "Ton image fait {taille:.1f} Mo, c'est au-dessus de la limite de 10 Mo.\n"
    "Réduis-la (capture d'écran, ou photo en qualité moyenne) et renvoie-la."
)


# --------------------------------------------------------------------------
# Signature Discord (Ed25519)
# --------------------------------------------------------------------------

def verify_signature(public_key: str | None, signature: str | None,
                     timestamp: str | None, body: bytes) -> bool:
    """Vérifie X-Signature-Ed25519 / X-Signature-Timestamp. Aucune exception."""
    if not public_key or not signature or not timestamp:
        return False
    try:
        from nacl.exceptions import BadSignatureError
        from nacl.signing import VerifyKey

        verify_key = VerifyKey(bytes.fromhex(public_key))
        message = timestamp.encode("utf-8") + (body or b"")
        verify_key.verify(message, bytes.fromhex(signature))
        return True
    except BadSignatureError:
        return False
    except Exception:
        return False


# --------------------------------------------------------------------------
# Signature interne /api/index -> /api/task
# --------------------------------------------------------------------------

def sign_internal(body: bytes, secret: str | None = None) -> tuple[str, str]:
    secret = secret or config.internal_secret()
    timestamp = str(int(time.time()))
    mac = hmac.new(
        secret.encode("utf-8"),
        timestamp.encode("utf-8") + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    return timestamp, mac


def verify_internal(signature: str | None, timestamp: str | None, body: bytes,
                    secret: str | None = None, tolerance: int = 300) -> bool:
    secret = secret or config.internal_secret()
    if not secret or not signature or not timestamp:
        return False
    try:
        if abs(time.time() - int(timestamp)) > tolerance:
            return False
    except (TypeError, ValueError):
        return False
    expected = hmac.new(
        secret.encode("utf-8"),
        timestamp.encode("utf-8") + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


# --------------------------------------------------------------------------
# Lecture d'une interaction
# --------------------------------------------------------------------------

def parse_command(interaction: dict) -> dict:
    """Extrait le mode, la question et la pièce jointe d'une interaction."""
    data = interaction.get("data") or {}
    mode = (data.get("name") or "").lower()

    question = None
    attachment_id = None
    for option in data.get("options") or []:
        nom = option.get("name")
        if nom == "question":
            question = option.get("value")
        elif nom == "image":
            attachment_id = option.get("value")

    attachment = None
    if attachment_id:
        resolved = (data.get("resolved") or {}).get("attachments") or {}
        brut = resolved.get(str(attachment_id)) or {}
        if brut:
            attachment = {
                "url": brut.get("url"),
                "filename": brut.get("filename") or "image",
                "content_type": (brut.get("content_type") or "").split(";")[0].strip(),
                "size": int(brut.get("size") or 0),
            }

    membre = interaction.get("member") or {}
    utilisateur = membre.get("user") or interaction.get("user") or {}

    return {
        "mode": mode,
        "question": (question or "").strip()[:config.MAX_QUESTION_CHARS] or None,
        "attachment": attachment,
        "application_id": str(interaction.get("application_id") or ""),
        "token": interaction.get("token") or "",
        "user_id": str(utilisateur.get("id") or "anonyme"),
        "channel_id": str(interaction.get("channel_id") or "direct"),
        "locale": interaction.get("locale") or "fr",
    }


def validate(commande: dict) -> str | None:
    """Retourne un message d'erreur destiné à l'élève, ou None si tout va bien."""
    attachment = commande.get("attachment")
    if attachment:
        content_type = (attachment.get("content_type") or "").lower()
        nom = attachment.get("filename") or "fichier"
        if not content_type.startswith("image/"):
            return MESSAGE_FICHIER_INVALIDE.format(
                nom=nom, type=content_type or "type inconnu"
            )
        if attachment.get("size", 0) > config.MAX_IMAGE_BYTES:
            return MESSAGE_IMAGE_TROP_LOURDE.format(
                taille=attachment["size"] / (1024 * 1024)
            )
    if not commande.get("question") and not attachment:
        return MESSAGE_SANS_CONTENU
    return None


# --------------------------------------------------------------------------
# Routage
# --------------------------------------------------------------------------

def route(interaction: dict) -> tuple[dict, dict | None]:
    """Décide de la réponse immédiate et, si besoin, du travail de fond.

    Renvoie (réponse Discord, tâche à exécuter en arrière-plan ou None).
    """
    type_interaction = interaction.get("type")

    if type_interaction == PING:
        return {"type": PONG}, None

    if type_interaction != APPLICATION_COMMAND:
        return discord_api.message_response(
            "Je ne sais traiter que les commandes slash. Tape `/help`.", ephemeral=True
        ), None

    commande = parse_command(interaction)
    mode = commande["mode"]

    if mode == "help":
        return discord_api.message_response(prompts.HELP_TEXT[:1990]), None

    if mode not in MODES:
        return discord_api.message_response(
            "Commande inconnue. Tape `/help` pour voir ce que je sais faire.",
            ephemeral=True,
        ), None

    erreur = validate(commande)
    if erreur:
        return discord_api.message_response(erreur, ephemeral=True), None

    # Accusé de réception immédiat (< 3 s), le travail continue en arrière-plan.
    return {"type": DEFERRED_CHANNEL_MESSAGE}, commande


# --------------------------------------------------------------------------
# Déclenchement de la fonction de travail
# --------------------------------------------------------------------------

def base_url(headers) -> str:
    """URL publique du déploiement, déduite des en-têtes de la requête entrante."""
    hote = None
    for cle in ("x-forwarded-host", "host"):
        try:
            valeur = headers.get(cle)
        except AttributeError:
            valeur = None
        if valeur:
            hote = valeur
            break
    if not hote:
        hote = config.get("VERCEL_PROJECT_PRODUCTION_URL") or config.get("VERCEL_URL")
    if not hote:
        raise RuntimeError("Impossible de déterminer l'URL du déploiement.")
    schema = "https"
    try:
        schema = headers.get("x-forwarded-proto") or "https"
    except AttributeError:
        pass
    schema = schema.split(",")[0].strip() or "https"
    return f"{schema}://{hote}"


def trigger_worker(url_base: str, tache: dict, timeout: float = 0.8) -> bool:
    """Réveille /api/task sans attendre sa réponse (fire-and-forget signé).

    Le dépassement de délai est le cas NORMAL : la requête est partie, la
    fonction de travail s'exécute de son côté et modifiera le message Discord.

    Le délai est court (0,8 s) parce qu'il s'ajoute au démarrage à froid dans
    le budget de 3 secondes imposé par Discord pour l'accusé de réception.
    """
    corps = json.dumps(tache, ensure_ascii=False).encode("utf-8")
    timestamp, signature = sign_internal(corps)
    requete = urllib.request.Request(
        f"{url_base.rstrip('/')}/api/task",
        data=corps,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Friedrich-Signature": signature,
            "X-Friedrich-Timestamp": timestamp,
            "User-Agent": discord_api.USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(requete, timeout=timeout):
            return True                     # tâche déjà terminée (rare, mais valide)
    except urllib.error.HTTPError as exc:
        # 404 : fonction absente du déploiement ; 401/403 : secret interne
        # différent entre les deux fonctions ; 5xx : elle a planté au démarrage.
        # Dans ces trois cas l'élève resterait devant « ⏳ » pour toujours :
        # mieux vaut l'avouer tout de suite.
        return exc.code < 400
    except TimeoutError:
        # cas NORMAL : la requête est partie, /api/task travaille de son côté
        return True
    except urllib.error.URLError as exc:
        return isinstance(exc.reason, (TimeoutError, socket.timeout))
    except Exception:
        return False
