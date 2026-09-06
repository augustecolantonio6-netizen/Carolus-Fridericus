"""Installation de Friedrich depuis le navigateur — POST /api/setup.

Protégé par l'en-tête `X-Setup-Secret` (variable `SETUP_SECRET`).
Actions disponibles (champ `action` du corps JSON) :

  - "status"    : état de la configuration (aucune clé n'est renvoyée)
  - "commands"  : enregistre / met à jour les commandes slash Discord
  - "ingest"    : ingère un morceau du corpus dans Pinecone (reprend où il en
                  était grâce à `offset` : aucune fonction ne dépasse sa durée)
  - "verifier"  : test de bout en bout (Discord + Pinecone + Gemini)

L'ingestion est idempotente : identifiants déterministes + empreinte du corpus.
"""

from __future__ import annotations

import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from utils import config, discord_api, interactions, rag  # noqa: E402

BUDGET_SECONDES = 30          # marge sous maxDuration
LOT = 90                      # fiches par requête Pinecone (95 maximum)

# Offre gratuite Pinecone : 250 000 tokens/minute pour llama-text-embed-v2.
# On s'arrête avant, et la page attend `PAUSE_S` avant l'appel suivant :
# ~140 000 tokens toutes les ~45 s ≈ 190 000 tokens/minute, marge confortable.
TOKENS_PAR_TOUR = 140_000
PAUSE_S = 25                  # attente demandée au navigateur entre deux tours
PAUSE_LIMITE_S = 60           # attente si le quota a quand même été atteint


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------

def action_status() -> dict:
    rapport = config.env_report()
    resultat: dict = {
        "configuration": rapport,
        "corpus": rag.corpus_meta() or {"present": rag.corpus_available()},
    }

    application_id = config.DISCORD_APPLICATION_ID()
    if application_id:
        # à ouvrir AVANT l'initialisation : sans invitation, Discord refuse
        # d'enregistrer les commandes sur le serveur (403 Missing Access).
        resultat["lien_invitation"] = discord_api.invite_url(application_id)

    jeton = config.DISCORD_TOKEN()
    if jeton:
        try:
            resultat["discord"] = discord_api.bot_identity(jeton)
        except Exception as exc:
            resultat["discord"] = {"erreur": str(exc)[:200]}

    if config.get("PINECONE_API_KEY"):
        try:
            resultat["pinecone"] = {
                "index_existe": rag.index_exists(),
                "statistiques": rag.stats() if rag.index_exists() else None,
                "manifeste": rag.read_manifest() if rag.index_exists() else None,
            }
        except Exception as exc:
            resultat["pinecone"] = {"erreur": str(exc)[:200]}
    return resultat


def action_commands() -> dict:
    application_id = config.require("DISCORD_APPLICATION_ID")
    jeton = config.require("DISCORD_TOKEN")
    guild = config.DISCORD_GUILD_ID()
    identite = discord_api.bot_identity(jeton)
    resultat = discord_api.register_commands(application_id, jeton, guild)
    resultat["bot"] = identite
    return resultat


def action_ingest(offset: int, force: bool) -> dict:
    debut = time.time()
    config.require("PINECONE_API_KEY")

    if not rag.corpus_available():
        return {
            "termine": True,
            "erreur": "Aucun corpus trouvé (data/corpus.jsonl.gz). "
                      "Génère-le avec `python ingest.py --extraire` puis redéploie.",
        }

    meta = rag.corpus_meta()
    total = int(meta.get("total") or 0)
    empreinte = str(meta.get("fingerprint") or "")

    reprise = False
    if offset <= 0:
        etat = rag.ensure_index(timeout=25)
        if not etat.get("pret"):
            return {"termine": False, "offset": 0, "total": total,
                    "attente_index": True, "attente_s": 5, "index": etat,
                    "message": "Création de l'index Pinecone en cours…"}
        if not force:
            manifeste = rag.read_manifest() or {}
            if manifeste and str(manifeste.get("fingerprint") or "") == empreinte:
                return {"termine": True, "offset": total, "total": total,
                        "deja_ingere": True, "index": etat,
                        "statistiques": rag.stats(),
                        "message": "Corpus déjà ingéré (rien à refaire)."}
            # reprise après un rechargement de page ou une coupure
            avancement = rag.read_progress() or {}
            if str(avancement.get("fingerprint") or "") == empreinte:
                try:
                    offset = max(0, min(total, int(float(avancement.get("offset") or 0))))
                except (TypeError, ValueError):
                    offset = 0
                reprise = offset > 0

    envoyes, tokens = 0, 0
    while (offset < total and tokens < TOKENS_PAR_TOUR
           and (time.time() - debut) < BUDGET_SECONDES):
        fiches = rag.read_corpus_slice(offset, LOT)
        if not fiches:
            offset = total
            break
        try:
            rag.upsert_records(fiches)
        except rag.RateLimited as exc:
            offset += exc.envoyes
            rag.write_progress(empreinte, offset)
            return {
                "termine": False, "offset": offset, "total": total,
                "envoyes_ce_tour": envoyes + exc.envoyes,
                "limite_debit": True, "attente_s": PAUSE_LIMITE_S,
                "message": "Quota d'embedding Pinecone atteint (250 000 tokens/min "
                           "en offre gratuite). Pause d'une minute, puis reprise.",
            }
        offset += len(fiches)
        envoyes += len(fiches)
        tokens += rag.estimate_tokens(fiches)

    termine = offset >= total
    resultat = {
        "termine": termine,
        "offset": offset,
        "total": total,
        "envoyes_ce_tour": envoyes,
        "tokens_estimes": tokens,
        "reprise": reprise,
        "duree_s": round(time.time() - debut, 1),
    }
    if termine:
        rag.write_manifest(empreinte, total)
        resultat["statistiques"] = rag.stats()
        resultat["message"] = f"Ingestion terminée : {total} extraits indexés."
    else:
        rag.write_progress(empreinte, offset)
        resultat["attente_s"] = PAUSE_S
    return resultat


def _tester_chaine_interne(url_base: str | None) -> dict:
    """Vérifie que /api est réellement capable de réveiller /api/task.

    C'est le maillon que rien d'autre ne teste : sans lui, l'élève resterait
    devant « ⏳ Friedrich réfléchit… » sans jamais voir de réponse.
    """
    if not url_base:
        return {"ok": False, "erreur": "URL du déploiement inconnue"}
    corps = json.dumps({"mode": "__ping__"}).encode("utf-8")
    horodatage, signature = interactions.sign_internal(corps)
    requete = urllib.request.Request(
        f"{url_base.rstrip('/')}/api/task",
        data=corps,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Friedrich-Signature": signature,
            "X-Friedrich-Timestamp": horodatage,
        },
    )
    debut = time.time()
    try:
        with urllib.request.urlopen(requete, timeout=25) as reponse:
            donnees = json.loads(reponse.read().decode("utf-8") or "{}")
        return {"ok": bool(donnees.get("pong")),
                "duree_s": round(time.time() - debut, 1),
                "message": donnees.get("message")}
    except urllib.error.HTTPError as exc:
        explication = {
            401: "la signature interne est refusée : SETUP_SECRET diffère entre "
                 "les deux fonctions (redéploie après avoir fixé la variable)",
            403: "accès refusé — Deployment Protection active sur la production ?",
            404: "api/task.py n'est pas déployé : vérifie que le fichier est bien "
                 "dans le dossier envoyé à Vercel",
        }.get(exc.code, f"la fonction a répondu {exc.code}")
        return {"ok": False, "code": exc.code, "erreur": explication}
    except Exception as exc:
        return {"ok": False, "erreur": f"{type(exc).__name__}: {str(exc)[:160]}"}


def action_verifier(url_base: str | None = None) -> dict:
    resultat: dict = {"chaine_interne": _tester_chaine_interne(url_base)}

    jeton = config.DISCORD_TOKEN()
    try:
        resultat["discord"] = {"ok": True, "bot": discord_api.bot_identity(jeton)} if jeton \
            else {"ok": False, "erreur": "DISCORD_TOKEN manquant"}
    except Exception as exc:
        resultat["discord"] = {"ok": False, "erreur": str(exc)[:200]}

    try:
        extraits = rag.search("dérivée d'une fonction composée convexité", top_k=10, top_n=3)
        resultat["pinecone"] = {
            "ok": bool(extraits),
            "extraits_trouves": len(extraits),
            "exemple_source": extraits[0].get("source") if extraits else None,
            "statistiques": rag.stats(),
        }
    except Exception as exc:
        resultat["pinecone"] = {"ok": False, "erreur": str(exc)[:200]}

    client = None
    try:
        from utils.gemini import GeminiClient

        client = GeminiClient()
        reponse = client.answer(
            prompt="Réponds exactement : OK",
            system_instruction="Tu réponds en un mot.",
            mode="solve",
            use_tools=False,
        )
        resultat["gemini"] = {
            "ok": True,
            "modele": reponse.get("modele"),
            "modele_demande": reponse.get("modele_demande"),
            "substitution": bool(reponse.get("substitution")),
            "cle_utilisee": reponse.get("cle"),
            "reponse": (reponse.get("texte") or "")[:40],
        }
        if reponse.get("substitution"):
            if client.modeles_vus:
                resultat["gemini"]["modeles_disponibles"] = client.modeles_vus[:15]
            resultat["gemini"]["conseil"] = (
                f"« {reponse.get('modele_demande')} » n'existe pas pour cette clé : "
                f"j'utilise « {reponse.get('modele')} ». Mets GEMINI_MODEL="
                f"{reponse.get('modele')} dans Vercel pour éviter la recherche "
                "à chaque question."
            )
    except Exception as exc:
        bloc = {"ok": False, "erreur": str(exc)[:400]}
        cause = getattr(exc, "__cause__", None)
        if cause is not None:
            bloc["detail_technique"] = f"{type(cause).__name__}: {cause}"[:400]
        try:
            liste = (client.available_models() if client is not None
                     else GeminiClient().available_models())
            if liste:
                bloc["modeles_disponibles"] = liste[:15]
        except Exception:
            pass
        resultat["gemini"] = bloc

    resultat["ok"] = all(
        bloc.get("ok") for bloc in (resultat["chaine_interne"], resultat["discord"],
                                    resultat["pinecone"], resultat["gemini"])
    )
    return resultat


# --------------------------------------------------------------------------
# Fonction HTTP
# --------------------------------------------------------------------------

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
        sys.stderr.write("friedrich setup: " + (fmt % args) + "\n")

    def do_GET(self):
        self._json(405, {
            "erreur": "Utilise la page /setup.html (POST /api/setup avec "
                      "l'en-tête X-Setup-Secret)."
        })

    def do_POST(self):
        secret_attendu = config.SETUP_SECRET()
        if not secret_attendu:
            return self._json(503, {
                "ok": False,
                "erreur": "SETUP_SECRET n'est pas défini dans les variables "
                          "d'environnement Vercel. Ajoute-le, redéploie, puis réessaie.",
            })

        fourni = self.headers.get("X-Setup-Secret") or ""
        if not hmac.compare_digest(fourni, secret_attendu):
            time.sleep(0.5)                     # ralentit les essais répétés
            return self._json(401, {"ok": False, "erreur": "Secret d'installation invalide."})

        try:
            longueur = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            longueur = 0
        if longueur > config.MAX_BODY_BYTES:
            return self._json(413, {"ok": False, "erreur": "corps trop volumineux"})

        corps = self.rfile.read(longueur) if longueur > 0 else b"{}"
        try:
            requete = json.loads(corps.decode("utf-8") or "{}")
        except Exception:
            requete = {}

        action = (requete.get("action") or "status").lower()
        try:
            if action == "status":
                rapport = action_status()
            elif action == "commands":
                rapport = action_commands()
            elif action == "ingest":
                rapport = action_ingest(
                    offset=int(requete.get("offset") or 0),
                    force=bool(requete.get("force")),
                )
            elif action == "verifier":
                try:
                    url_base = interactions.base_url(self.headers)
                except Exception:
                    url_base = None
                rapport = action_verifier(url_base)
            else:
                return self._json(400, {"ok": False, "erreur": f"action inconnue : {action}"})
        except Exception as exc:
            self.log_message("action %s a échoué: %s", action, type(exc).__name__)
            return self._json(500, {
                "ok": False,
                "action": action,
                "erreur": f"{type(exc).__name__}: {str(exc)[:300]}",
            })

        rapport.setdefault("ok", True)
        rapport["action"] = action
        self._json(200, rapport)
