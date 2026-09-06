"""Traitement complet d'une commande (exécuté par `api/task.py`).

Enchaînement : image → transcription → recherche Pinecone → Gemini (+ outils
SymPy) → découpage → réponse Discord.
"""

from __future__ import annotations

import time

from . import config, discord_api, formatting, memory, prompts, rag
from .gemini import GeminiClient, GeminiError


def _recuperer_image(attachment: dict | None) -> tuple[bytes, str] | None:
    """Télécharge la pièce jointe si (et seulement si) c'est une image valide."""
    if not attachment or not attachment.get("url"):
        return None
    content_type = (attachment.get("content_type") or "").lower()
    if not content_type.startswith("image/"):
        raise ValueError("Je ne sais lire que des images.")
    if int(attachment.get("size") or 0) > config.MAX_IMAGE_BYTES:
        raise ValueError("Ton image dépasse 10 Mo.")
    donnees = discord_api.download_attachment(attachment["url"])
    if content_type in ("image/heic", "image/heif"):
        content_type = "image/jpeg"          # Gemini ne lit pas le HEIC
    return donnees, content_type


def traiter(tache: dict) -> dict:
    """Traite une tâche et modifie le message Discord différé. Ne lève jamais."""
    debut = time.time()
    application_id = tache.get("application_id") or config.DISCORD_APPLICATION_ID() or ""
    jeton = tache.get("token") or ""
    mode = tache.get("mode") or "detail"
    question = tache.get("question")
    rapport: dict = {"mode": mode, "avec_image": bool(tache.get("attachment"))}

    if not application_id or not jeton:
        return {"ok": False, "erreur": "interaction incomplète"}

    try:
        # 1) image ------------------------------------------------------
        try:
            image = _recuperer_image(tache.get("attachment"))
        except Exception as exc:
            discord_api.send_error(application_id, jeton, str(exc) or
                                   "Je n'ai pas réussi à ouvrir ton image.")
            return {"ok": False, "erreur": "image refusée"}

        client = GeminiClient()

        # 2) transcription de l'image pour interroger le programme -------
        transcription = ""
        if image is not None:
            transcription = client.transcribe_image(image, prompts.TRANSCRIPTION_PROMPT)
            rapport["transcription"] = bool(transcription)

        # 3) recherche dans le programme officiel (Pinecone) -------------
        requete = rag.build_query(question, transcription)
        extraits = rag.search(requete) if requete else []
        contexte = rag.format_context(extraits)
        rapport["extraits"] = len(extraits)

        # 4) mémoire courte ---------------------------------------------
        historique = memory.format_history(
            memory.load(tache.get("user_id", ""), tache.get("channel_id", ""))
        )

        # 5) génération --------------------------------------------------
        prompt = prompts.build_prompt(
            mode=mode,
            question=question,
            context=contexte,
            has_image=image is not None,
            history=historique,
        )
        resultat = client.answer(
            prompt=prompt,
            system_instruction=prompts.SYSTEM_PROMPT,
            mode=mode,
            image=image,
            use_tools=True,
        )
        texte = formatting.polish(resultat["texte"])

        # 6) garde-fou « hors sujet » ------------------------------------
        if prompts.OFF_TOPIC_REPLY[:40].lower() in texte.lower():
            texte = prompts.OFF_TOPIC_REPLY
            resultat["images"] = []

        # 7) envoi --------------------------------------------------------
        nb_messages = discord_api.send_answer(
            application_id, jeton, texte, resultat.get("images")
        )

        memory.append(tache.get("user_id", ""), tache.get("channel_id", ""),
                      mode, question or "(image)", texte)

        rapport.update({
            "ok": True,
            "messages": nb_messages,
            "outils": resultat.get("outils"),
            "cle_gemini": resultat.get("cle"),
            "duree_s": round(time.time() - debut, 1),
        })
        return rapport

    except GeminiError as exc:
        discord_api.send_error(application_id, jeton, str(exc))
        return {"ok": False, "erreur": "gemini"}
    except discord_api.DiscordError:
        return {"ok": False, "erreur": "discord"}
    except Exception as exc:
        discord_api.send_error(
            application_id, jeton,
            "Une erreur inattendue m'a empêché de répondre. Réessaie dans un "
            "instant, et si ça recommence, simplifie ton énoncé.",
        )
        return {"ok": False, "erreur": type(exc).__name__}
