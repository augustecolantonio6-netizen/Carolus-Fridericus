"""Point d'entrée Discord (Vercel Serverless Function).

URL publique : https://<projet>.vercel.app/api
À coller dans « Interactions Endpoint URL » du Discord Developer Portal.

Cette fonction doit répondre en moins de 3 secondes :
  - PING              -> {"type": 1}
  - /help             -> réponse immédiate (type 4)
  - autres commandes  -> accusé de réception différé (type 5), puis /api/task
                         fait le vrai travail et modifie le message.
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler

# les modules partagés vivent à la racine du projet
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from utils import config, discord_api, interactions  # noqa: E402


class handler(BaseHTTPRequestHandler):
    server_version = "Friedrich/1.0"

    # -- utilitaires -------------------------------------------------------
    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _text(self, status: int, message: str) -> None:
        body = message.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # journal minimal, jamais de secret
        sys.stderr.write("friedrich api: " + (fmt % args) + "\n")

    # -- santé -------------------------------------------------------------
    def do_GET(self):
        """Petit état de santé — n'expose que des booléens, jamais les clés."""
        rapport = config.env_report()
        self._json(200, {
            "service": "friedrich",
            "statut": "en ligne",
            "endpoint_interactions": "POST /api",
            "configuration": rapport["requises"],
            "manquantes": rapport["manquantes"],
            "modele": rapport["modele_gemini"],
        })

    # -- interactions Discord ---------------------------------------------
    def do_POST(self):
        try:
            longueur = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            longueur = 0
        if longueur > config.MAX_BODY_BYTES:
            return self._text(413, "Corps de requête trop volumineux.")

        corps = self.rfile.read(longueur) if longueur > 0 else b""

        cle_publique = config.DISCORD_PUBLIC_KEY()
        if not cle_publique:
            self.log_message("%s", "DISCORD_PUBLIC_KEY absente")
            return self._text(500, "Configuration incomplète : DISCORD_PUBLIC_KEY manquante.")

        signature = self.headers.get("X-Signature-Ed25519")
        horodatage = self.headers.get("X-Signature-Timestamp")
        if not interactions.verify_signature(cle_publique, signature, horodatage, corps):
            # Discord exige un 401 pour valider l'URL d'interactions
            return self._text(401, "invalid request signature")

        try:
            interaction = json.loads(corps.decode("utf-8"))
        except Exception:
            return self._text(400, "Corps JSON invalide.")

        try:
            reponse, tache = interactions.route(interaction)
        except Exception as exc:
            self.log_message("routage impossible: %s", type(exc).__name__)
            return self._json(200, discord_api.message_response(
                "Je n'ai pas compris cette commande. Tape `/help`.", ephemeral=True))

        if tache is not None:
            try:
                url = interactions.base_url(self.headers)
                lancee = interactions.trigger_worker(url, tache)
            except Exception as exc:
                self.log_message("déclenchement impossible: %s", type(exc).__name__)
                lancee = False
            if not lancee:
                return self._json(200, discord_api.message_response(
                    "⚠️ Je n'arrive pas à lancer ma réflexion (fonction `/api/task` "
                    "injoignable). Réessaie dans un instant.", ephemeral=True))

        self._json(200, reponse)
