"""Appels HTTP vers l'API Discord (uniquement la bibliothèque standard).

Aucun bot « Gateway » n'est lancé : Vercel reçoit les *HTTP Interactions* et
répond en modifiant le message différé via le webhook d'interaction.
"""

from __future__ import annotations

import json
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import config
from .formatting import split_message

USER_AGENT = "Friedrich (https://vercel.com, 1.0)"

# Types d'options d'application command
OPTION_STRING = 3
OPTION_ATTACHMENT = 11

QUESTION_OPTION = {
    "type": OPTION_STRING,
    "name": "question",
    "description": "Ton énoncé ou ta question (facultatif si tu envoies une image)",
    "required": False,
}

IMAGE_OPTION = {
    "type": OPTION_ATTACHMENT,
    "name": "image",
    "description": "Photo de l'exercice ou de ta copie (image uniquement, 10 Mo max)",
    "required": False,
}

COMMANDS: list[dict] = [
    {
        "name": "help",
        "type": 1,
        "description": "Comment utiliser Friedrich, ton professeur de maths",
        "options": [],
    },
    {
        "name": "hint",
        "type": 1,
        "description": "Un indice pour démarrer, sans te donner la solution",
        "options": [QUESTION_OPTION, IMAGE_OPTION],
    },
    {
        "name": "detail",
        "type": 1,
        "description": "La résolution complète, étape par étape, avec les explications",
        "options": [QUESTION_OPTION, IMAGE_OPTION],
    },
    {
        "name": "solve",
        "type": 1,
        "description": "La solution directe, en très peu d'étapes",
        "options": [QUESTION_OPTION, IMAGE_OPTION],
    },
    {
        "name": "correct",
        "type": 1,
        "description": "Je corrige ton travail : ce qui est juste, ta première erreur",
        "options": [
            {
                "type": OPTION_STRING,
                "name": "question",
                "description": "Ton énoncé et ton raisonnement (facultatif si tu envoies une photo)",
                "required": False,
            },
            {
                "type": OPTION_ATTACHMENT,
                "name": "image",
                "description": "Photo de ta copie (image uniquement, 10 Mo max)",
                "required": False,
            },
        ],
    },
]

ALLOWED_MENTIONS = {"parse": []}


class DiscordError(RuntimeError):
    pass


def _request(method: str, url: str, *, token: str | None = None,
             payload: dict | None = None, files: list[tuple[str, bytes]] | None = None,
             timeout: int = 20) -> dict:
    """Requête HTTP vers Discord. Ne journalise jamais le jeton."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bot {token}"

    if files:
        boundary = f"----friedrich{uuid.uuid4().hex}"
        body = bytearray()
        payload_json = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        body += f"--{boundary}\r\n".encode()
        body += b'Content-Disposition: form-data; name="payload_json"\r\n'
        body += b"Content-Type: application/json\r\n\r\n"
        body += payload_json + b"\r\n"
        for position, (filename, content) in enumerate(files):
            mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            body += f"--{boundary}\r\n".encode()
            body += (
                f'Content-Disposition: form-data; name="files[{position}]"; '
                f'filename="{filename}"\r\n'
            ).encode()
            body += f"Content-Type: {mime}\r\n\r\n".encode()
            body += content + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        data = bytes(body)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    else:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        if data is not None:
            headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            if not raw:
                return {}
            try:
                return json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                return {}
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        raise DiscordError(f"Discord {method} {exc.code} — {detail}") from exc
    except Exception as exc:
        raise DiscordError(f"Discord injoignable ({type(exc).__name__})") from exc


# --------------------------------------------------------------------------
# Enregistrement des commandes
# --------------------------------------------------------------------------

def invite_url(application_id: str, permissions: str = "51200") -> str:
    """Lien d'invitation du bot (scopes bot + applications.commands).

    Permissions 51200 = Send Messages + Embed Links + Attach Files.
    """
    return (
        "https://discord.com/api/oauth2/authorize"
        f"?client_id={application_id}"
        "&scope=bot%20applications.commands"
        f"&permissions={permissions}"
    )


def register_commands(application_id: str, bot_token: str, guild_id: str | None = None) -> dict:
    """Enregistre (ou met à jour) les commandes slash. Idempotent : PUT global.

    Attention : l'enregistrement sur un serveur précis suppose que le bot y a
    déjà été invité, sinon Discord répond 403 Missing Access.
    """
    if guild_id:
        url = f"{config.DISCORD_API}/applications/{application_id}/guilds/{guild_id}/commands"
        portee = f"serveur {guild_id} (immédiat)"
    else:
        url = f"{config.DISCORD_API}/applications/{application_id}/commands"
        portee = "global (propagation jusqu'à 1 h)"

    try:
        resultat = _request("PUT", url, token=bot_token, payload=COMMANDS)
    except DiscordError as exc:
        message = str(exc)
        if guild_id and ("403" in message or "50001" in message):
            raise DiscordError(
                f"Friedrich n'est pas encore invité sur le serveur {guild_id}, "
                "Discord refuse donc d'y enregistrer les commandes. Ouvre ce lien, "
                "choisis ton serveur, autorise, puis relance l'installation : "
                + invite_url(application_id)
            ) from exc
        raise
    noms = []
    if isinstance(resultat, list):
        noms = [item.get("name") for item in resultat if isinstance(item, dict)]
    return {
        "portee": portee,
        "commandes": noms or [command["name"] for command in COMMANDS],
        "nombre": len(noms) if noms else len(COMMANDS),
    }


def bot_identity(bot_token: str) -> dict:
    """Vérifie le jeton du bot (renvoie juste son nom, jamais le jeton)."""
    data = _request("GET", f"{config.DISCORD_API}/users/@me", token=bot_token)
    return {"nom": data.get("username"), "id": data.get("id")}


# --------------------------------------------------------------------------
# Réponses aux interactions
# --------------------------------------------------------------------------

def _webhook(application_id: str, interaction_token: str) -> str:
    return f"{config.DISCORD_API}/webhooks/{application_id}/{interaction_token}"


def edit_original(application_id: str, interaction_token: str, content: str,
                  files: list[tuple[str, bytes]] | None = None) -> dict:
    """PATCH /webhooks/{app}/{token}/messages/@original — remplit la réponse différée."""
    payload = {"content": content, "allowed_mentions": ALLOWED_MENTIONS}
    return _request(
        "PATCH",
        f"{_webhook(application_id, interaction_token)}/messages/@original",
        payload=payload,
        files=files,
    )


def followup(application_id: str, interaction_token: str, content: str,
             files: list[tuple[str, bytes]] | None = None) -> dict:
    """POST /webhooks/{app}/{token} — messages suivants (réponses longues)."""
    payload = {"content": content, "allowed_mentions": ALLOWED_MENTIONS}
    return _request(
        "POST",
        _webhook(application_id, interaction_token),
        payload=payload,
        files=files,
    )


def send_answer(application_id: str, interaction_token: str, text: str,
                images: list[bytes] | None = None) -> int:
    """Envoie une réponse de longueur quelconque, découpée en messages Discord."""
    morceaux = split_message(text) or ["(réponse vide)"]
    fichiers: list[tuple[str, bytes]] = []
    for position, png in enumerate(images or [], start=1):
        if png and len(png) <= 7 * 1024 * 1024:
            fichiers.append((f"graphique{position}.png", png))

    edit_original(application_id, interaction_token, morceaux[0],
                  files=fichiers if len(morceaux) == 1 else None)
    for suite in morceaux[1:-1]:
        followup(application_id, interaction_token, suite)
    if len(morceaux) > 1:
        followup(application_id, interaction_token, morceaux[-1],
                 files=fichiers or None)
    return len(morceaux)


def send_error(application_id: str, interaction_token: str, message: str) -> None:
    """Message d'erreur lisible par l'élève, sans aucune information sensible."""
    try:
        edit_original(application_id, interaction_token, f"⚠️ {message}")
    except Exception:
        pass


def deferred_response() -> dict:
    """Accusé de réception « je réfléchis » (type 5), envoyé en moins de 3 s."""
    return {"type": 5}


def message_response(content: str, ephemeral: bool = False) -> dict:
    """Réponse immédiate (type 4)."""
    data = {"content": content, "allowed_mentions": ALLOWED_MENTIONS}
    if ephemeral:
        data["flags"] = 64
    return {"type": 4, "data": data}


def guess_extension(mime: str) -> str:
    return mimetypes.guess_extension(mime or "") or ".png"


def download_attachment(url: str, max_bytes: int = config.MAX_IMAGE_BYTES) -> bytes:
    """Télécharge une pièce jointe Discord en refusant tout dépassement de taille."""
    if not url.startswith("https://"):
        raise DiscordError("URL de pièce jointe invalide.")
    host = urllib.parse.urlsplit(url).hostname or ""
    if not (host.endswith("discordapp.com") or host.endswith("discord.com")
            or host.endswith("discordapp.net")):
        raise DiscordError("Pièce jointe refusée : origine inattendue.")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        declared = response.headers.get("Content-Length")
        if declared and int(declared) > max_bytes:
            raise DiscordError("Ton image dépasse 10 Mo.")
        data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise DiscordError("Ton image dépasse 10 Mo.")
    return data
