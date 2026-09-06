"""Fonction de travail (Vercel Serverless Function) — appelée par /api.

Elle fait le travail long (Pinecone + Gemini + SymPy) puis modifie la réponse
différée de Discord. Elle n'est joignable qu'avec une signature HMAC interne :
personne d'autre que `/api/index.py` ne peut la déclencher.
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from utils import config, interactions  # noqa: E402


class handler(BaseHTTPRequestHandler):
    server_version = "Friedrich/1.0"

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stderr.write("friedrich task: " + (fmt % args) + "\n")

    def do_GET(self):
        self._json(405, {"erreur": "POST uniquement (fonction interne)."})

    def do_POST(self):
        try:
            longueur = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            longueur = 0
        if longueur > config.MAX_BODY_BYTES:
            return self._json(413, {"erreur": "corps trop volumineux"})
        corps = self.rfile.read(longueur) if longueur > 0 else b""

        if not interactions.verify_internal(
            self.headers.get("X-Friedrich-Signature"),
            self.headers.get("X-Friedrich-Timestamp"),
            corps,
        ):
            return self._json(401, {"erreur": "signature interne invalide"})

        try:
            tache = json.loads(corps.decode("utf-8"))
        except Exception:
            return self._json(400, {"erreur": "JSON invalide"})

        # Sonde de la page d'installation : prouve que cette fonction est bien
        # déployée et qu'elle accepte la signature interne, sans rien consommer.
        if tache.get("mode") == "__ping__":
            return self._json(200, {"ok": True, "pong": True,
                                    "message": "chaîne interne opérationnelle"})

        from utils import pipeline  # import tardif : démarrage à froid plus rapide

        rapport = pipeline.traiter(tache)
        # le rapport ne contient ni jeton, ni clé, ni contenu d'élève
        self.log_message("terminé: %s", json.dumps(rapport, ensure_ascii=False))
        try:
            self._json(200, rapport)
        except Exception:
            # /api n'attend pas notre réponse : il a raccroché au bout d'une
            # seconde. C'est le fonctionnement voulu — le travail est fait et
            # le message Discord est déjà modifié.
            pass
