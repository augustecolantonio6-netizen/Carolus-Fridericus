"""Client Gemini avec clé de secours et outils mathématiques réels.

- SDK officiel `google-genai`.
- Deux clés : `GEMINI_API_KEY_1` puis, en cas de quota / 429 / 503, bascule
  automatique sur `GEMINI_API_KEY_2`.
- Les *function calls* déclarés sont réellement exécutés (SymPy / Matplotlib) :
  Friedrich ne prétend jamais avoir calculé quelque chose qu'il n'a pas calculé.
"""

from __future__ import annotations

import re
import time

from . import config, math_tools

MAX_TOOL_ROUNDS = 4

#: Budget de « réflexion » par commande (latence vs. qualité du raisonnement).
THINKING_BUDGET = {
    "hint": 512,
    "solve": 1024,
    "detail": 3072,
    "correct": 3072,
    "transcription": 0,
}

_RETRYABLE_MARKERS = (
    "429", "resource_exhausted", "quota", "rate limit", "rate_limit",
    "503", "unavailable", "overloaded", "500", "internal", "502",
    "deadline", "timeout", "temporarily",
)

#: Si le modèle demandé n'est pas utilisable, on essaie ceux-ci dans l'ordre.
#: Ordre établi par mesure réelle sur une clé gratuite (31/08/2026) : temps de
#: réponse pour un corrigé complet avec appels d'outils.
#:   gemini-3.5-flash 8 s · gemini-3-flash-preview 7 s · gemini-3.6-flash 20 s
#: Les alias « -latest » sont volontairement en fin de liste : ils pointent vers
#: le modèle le plus récent, donc le plus sollicité — `gemini-flash-latest`
#: répondait 503 « high demand » au moment de la mesure.
MODELES_PREFERES = (
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-3-flash-preview",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-flash-latest",
    "gemini-2.5-flash",            # comptes plus anciens
    "gemini-2.0-flash",
    "gemini-pro-latest",           # dernier recours : plus lent, mais il répond
)

#: Google nomme souvent le remplaçant dans son message d'erreur :
#: « … use models/gemini-3.6-flash for the latest features … »
_MODELE_CITE = re.compile(r"models/(gemini[a-z0-9.\-]*)", re.IGNORECASE)

#: Familles inutilisables ici : génération d'images, audio, embeddings, temps
#: réel, robotique, recherche approfondie… (relevé sur l'API le 31/08/2026).
_MODELES_EXCLUS = (
    "embedding", "embed", "aqa", "imagen", "veo", "tts", "audio",
    "live", "computer-use", "gemma", "image", "banana",
    "robotics", "transcribe", "deep-research", "antigravity", "lyria",
    "omni",          # 429 hors offre payante
)

#: Cache « modèle demandé -> modèle réellement disponible », partagé par les
#: invocations servies par une même instance chaude (évite de redécouvrir).
_SUBSTITUTIONS: dict[str, str] = {}


def _est_modele_absent(exc: Exception) -> bool:
    """Le modèle n'est pas utilisable : inexistant, retiré, ou fermé aux nouveaux.

    Attention : un modèle peut apparaître dans `models.list()` et refuser
    quand même de répondre (« no longer available to new users »).
    """
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    blob = f"{type(exc).__name__} {exc}".lower()
    if code == 404:
        return True
    return ("not_found" in blob or "404" in blob
            or ("not found" in blob and "model" in blob)
            or "no longer available" in blob
            or "is not supported for generatecontent" in blob
            or "only supports interactions api" in blob
            or "deprecated" in blob)


def _est_sature(exc: Exception) -> bool:
    """503 / UNAVAILABLE / overloaded : les serveurs de Google sont débordés."""
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code == 503:
        return True
    blob = f"{type(exc).__name__} {exc}".lower()
    return "503" in blob or "unavailable" in blob or "overloaded" in blob


def _est_config_refusee(exc: Exception) -> bool:
    """400 INVALID_ARGUMENT : un réglage envoyé ne convient pas à ce modèle.

    Google reste souvent muet sur le coupable — `gemini-3.6-flash` répond un
    laconique « Request contains an invalid argument » quand on lui envoie
    `thinking_budget=0`. On ne peut donc pas filtrer sur le mot « thinking » :
    tout 400 déclenche un nouvel essai avec une configuration allégée.
    """
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code not in (400, None):
        return False
    blob = f"{type(exc).__name__} {exc}".lower()
    return "400" in blob or "invalid_argument" in blob or "invalid argument" in blob


class GeminiError(RuntimeError):
    """Erreur Gemini déjà « traduite » pour l'élève (aucun secret dedans)."""


def _is_retryable(exc: Exception) -> bool:
    """Quota dépassé, 429, 503, service indisponible -> on tente la clé suivante."""
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code in (429, 500, 502, 503, 504):
        return True
    blob = f"{type(exc).__name__} {exc}".lower()
    return any(marker in blob for marker in _RETRYABLE_MARKERS)


class GeminiClient:
    """Enveloppe minimale autour de `google-genai`, avec bascule de clé."""

    def __init__(self, model: str | None = None):
        self.model_demande = model or config.GEMINI_MODEL()
        # un modèle de remplacement déjà découvert est réutilisé tel quel
        self.model = _SUBSTITUTIONS.get(self.model_demande, self.model_demande)
        self.keys = config.gemini_keys()
        if not self.keys:
            raise GeminiError(
                "Aucune clé Gemini configurée : ajoute GEMINI_API_KEY_1 dans Vercel."
            )
        self._clients: dict[int, object] = {}
        self.last_key_index: int | None = None
        self._modeles_rejetes: set[str] = set()
        self.sans_reflexion = False
        self.sans_outils = False
        self.modeles_vus: list[str] = []

    # -- infrastructure ----------------------------------------------------
    def _client(self, position: int):
        if position not in self._clients:
            from google import genai

            self._clients[position] = genai.Client(api_key=self.keys[position])
        return self._clients[position]

    def _config(self, system_instruction: str, mode: str, with_tools: bool):
        from google.genai import types

        kwargs: dict = {
            "temperature": 0.25 if mode != "transcription" else 0.0,
            "max_output_tokens": 8192,
        }
        if system_instruction:
            kwargs["system_instruction"] = system_instruction
        if with_tools and not self.sans_outils:
            kwargs["tools"] = [types.Tool(function_declarations=math_tools.TOOL_DECLARATIONS)]
            auto = getattr(types, "AutomaticFunctionCallingConfig", None)
            if auto is not None:
                kwargs["automatic_function_calling"] = auto(disable=True)

        # `thinking_budget=0` est refusé par certains modèles (gemini-3.6-flash) :
        # quand on ne veut pas de réflexion, on n'envoie simplement pas le réglage.
        budget = THINKING_BUDGET.get(mode, 2048)
        thinking = getattr(types, "ThinkingConfig", None)
        if thinking is not None and budget and not self.sans_reflexion:
            try:
                kwargs["thinking_config"] = thinking(thinking_budget=budget)
            except Exception:
                pass
        try:
            return types.GenerateContentConfig(**kwargs)
        except Exception:
            kwargs.pop("thinking_config", None)
            kwargs.pop("automatic_function_calling", None)
            return types.GenerateContentConfig(**kwargs)

    # -- découverte des modèles -------------------------------------------
    def available_models(self, position: int = 0) -> list[str]:
        """Modèles réellement proposés par l'API pour cette clé (texte seulement)."""
        if self.modeles_vus:
            return self.modeles_vus
        try:
            pager = self._client(position).models.list()
        except Exception:
            return []
        noms: list[str] = []
        try:
            for modele in pager:
                nom = (getattr(modele, "name", "") or "").split("/")[-1]
                if not nom or any(mot in nom.lower() for mot in _MODELES_EXCLUS):
                    continue
                actions = getattr(modele, "supported_actions", None)
                if actions and "generateContent" not in actions:
                    continue
                if nom not in noms:
                    noms.append(nom)
        except Exception:
            pass
        self.modeles_vus = noms
        return noms

    def _resoudre_modele(self, position: int, exc: Exception | None = None) -> str | None:
        """Choisit un autre modèle après un refus, sans jamais reprendre un raté."""
        self._modeles_rejetes.add(self.model)
        if len(self._modeles_rejetes) > 3:            # on ne s'acharne pas
            return None

        def retenir(nom: str) -> str:
            _SUBSTITUTIONS[self.model_demande] = nom
            self.model = nom
            return nom

        # 1) Google nomme souvent lui-même le remplaçant dans son message.
        if exc is not None:
            for nom in _MODELE_CITE.findall(str(exc)):
                if nom not in self._modeles_rejetes:
                    return retenir(nom)

        # 2) sinon : la liste réelle de l'API, privée des modèles déjà refusés.
        disponibles = [nom for nom in self.available_models(position)
                       if nom not in self._modeles_rejetes]
        if not disponibles:
            return None

        for prefere in MODELES_PREFERES:
            if prefere in disponibles:
                return retenir(prefere)
        for nom in disponibles:                        # à défaut, un « flash »
            if "flash" in nom:
                return retenir(nom)
        for nom in disponibles:
            if nom.startswith("gemini"):
                return retenir(nom)
        return None

    def _call(self, contents, fabriquer_config):
        """Appel réseau, avec trois filets : clé, modèle, réglages.

        - quota / 429 / 503 -> on passe à la clé de secours ;
        - modèle retiré ou fermé -> on bascule sur un modèle réellement servi ;
        - réglage refusé (budget de réflexion) -> on rejoue sans lui.
        """
        last: Exception | None = None
        for _tentative_modele in range(3):
            for position in range(len(self.keys)):
                essais_reseau = 0    # incidents passagers : 2 au plus par clé
                tours = 0            # garde-fou global de la boucle
                while essais_reseau < 2 and tours < 7:
                    tours += 1
                    try:
                        response = self._client(position).models.generate_content(
                            model=self.model,
                            contents=contents,
                            config=fabriquer_config(),
                        )
                        self.last_key_index = position + 1
                        return response
                    except Exception as exc:
                        last = exc
                        if _est_modele_absent(exc):
                            # changer de modèle ne consomme pas d'essai réseau
                            if self._resoudre_modele(position, exc):
                                continue
                            raise GeminiError(self._modele_introuvable()) from exc
                        if _est_config_refusee(exc) and self._degrader():
                            continue    # nouvel essai, configuration allégée
                        if not _is_retryable(exc):
                            raise GeminiError(self._friendly(exc)) from exc
                        essais_reseau += 1
                        time.sleep(1.0 * essais_reseau)
                # clé suivante (clé de secours)

            # Toutes les clés ont échoué sur ce modèle. Un 503 « surchargé » peut
            # ne toucher qu'un modèle : on tente le suivant avant d'abandonner.
            if self._resoudre_modele(0, None) is None:
                break

        if last is not None and _est_sature(last):
            raise GeminiError(self._sature()) from last
        raise GeminiError(
            self._friendly(last) if last else "Gemini est injoignable.") from last

    def _degrader(self) -> bool:
        """Retire un réglage optionnel avant de réessayer. False si plus rien à retirer."""
        if not self.sans_reflexion:
            self.sans_reflexion = True      # on abandonne le budget de réflexion
            return True
        if not self.sans_outils:
            self.sans_outils = True         # puis les outils SymPy
            return True
        return False

    def _sature(self) -> str:
        essayes = ", ".join(sorted(self._modeles_rejetes | {self.model}))
        return (
            f"Les serveurs Gemini refusent la requête (503, « surchargé ») sur "
            f"tous les modèles essayés : {essayes}. C'est temporaire et côté "
            "Google — réessaie dans une ou deux minutes. Si ça dure, épingle un "
            "autre modèle dans GEMINI_MODEL."
        )

    def _modele_introuvable(self) -> str:
        refuses = ", ".join(sorted(self._modeles_rejetes)) or self.model_demande
        dispo = ", ".join(self.modeles_vus[:8]) if self.modeles_vus else "(liste vide)"
        return (
            f"Aucun modèle utilisable : {refuses} refusé(s) par l'API. "
            f"Modèles annoncés pour cette clé : {dispo}. "
            "Choisis-en un et mets-le dans la variable GEMINI_MODEL (Vercel), "
            "puis redéploie."
        )

    @staticmethod
    def _friendly(exc: Exception | None) -> str:
        blob = f"{exc}".lower()
        if "api key" in blob or "unauthenticated" in blob or "401" in blob or "403" in blob:
            return ("Ma clé Gemini est refusée. Vérifie GEMINI_API_KEY_1 / "
                    "GEMINI_API_KEY_2 dans les variables Vercel.")
        if "quota" in blob or "429" in blob or "resource_exhausted" in blob:
            return ("J'ai atteint mon quota Gemini pour le moment. "
                    "Réessaie dans quelques minutes.")
        if "not found" in blob or "404" in blob:
            return (f"Le modèle « {config.GEMINI_MODEL()} » est introuvable pour cette clé. "
                    "Vérifie la variable GEMINI_MODEL.")
        if "503" in blob or "unavailable" in blob or "overloaded" in blob:
            return "Le service Gemini est saturé. Réessaie dans un instant."
        return "Je n'arrive pas à joindre Gemini pour l'instant. Réessaie dans un instant."

    # -- extraction des réponses ------------------------------------------
    @staticmethod
    def _parts(response) -> list:
        try:
            candidate = response.candidates[0]
            return list(candidate.content.parts or [])
        except Exception:
            return []

    @staticmethod
    def _text_of(parts) -> str:
        morceaux = []
        for part in parts:
            text = getattr(part, "text", None)
            if text:
                morceaux.append(text)
        return "".join(morceaux).strip()

    @staticmethod
    def _blocked_reason(response) -> str | None:
        try:
            feedback = getattr(response, "prompt_feedback", None)
            if feedback and getattr(feedback, "block_reason", None):
                return str(feedback.block_reason)
            reason = getattr(response.candidates[0], "finish_reason", None)
            if reason and str(reason).upper().endswith(("SAFETY", "RECITATION", "BLOCKLIST")):
                return str(reason)
        except Exception:
            pass
        return None

    # -- API publique ------------------------------------------------------
    def answer(
        self,
        prompt: str,
        system_instruction: str,
        mode: str = "detail",
        image: tuple[bytes, str] | None = None,
        use_tools: bool = True,
    ) -> dict:
        """Répond à l'élève. Exécute réellement les outils demandés par le modèle."""
        from google.genai import types

        parts = [types.Part.from_text(text=prompt)]
        if image is not None:
            data, mime = image
            parts.append(types.Part.from_bytes(data=data, mime_type=mime))

        contents = [types.Content(role="user", parts=parts)]

        # la configuration est refabriquée à chaque essai : si le modèle refuse
        # un réglage, l'essai suivant part d'une configuration allégée.
        def fabriquer_config():
            return self._config(system_instruction, mode, use_tools)

        images: list[bytes] = []
        outils_utilises: list[str] = []

        for _ in range(MAX_TOOL_ROUNDS):
            response = self._call(contents, fabriquer_config)
            reponse_parts = self._parts(response)

            appels = [
                getattr(part, "function_call", None)
                for part in reponse_parts
                if getattr(part, "function_call", None)
            ]
            if not appels:
                texte = self._text_of(reponse_parts) or (getattr(response, "text", "") or "")
                if not texte.strip():
                    raison = self._blocked_reason(response)
                    if raison:
                        raise GeminiError(
                            "Je ne peux pas traiter cette demande telle quelle "
                            "(contenu bloqué). Reformule ton exercice."
                        )
                    raise GeminiError("Je n'ai pas réussi à formuler de réponse. Réessaie.")
                return {
                    "texte": texte.strip(),
                    "images": images,
                    "outils": outils_utilises,
                    "cle": self.last_key_index,
                    "modele": self.model,
                    "modele_demande": self.model_demande,
                    "substitution": self.model != self.model_demande,
                }

            # le modèle demande des calculs : on les exécute pour de vrai
            contents.append(response.candidates[0].content)
            reponses_outils = []
            for appel in appels:
                nom = getattr(appel, "name", "") or ""
                arguments = dict(getattr(appel, "args", None) or {})
                resultat, png = math_tools.call_tool(nom, arguments)
                if png:
                    images.append(png)
                outils_utilises.append(nom)
                reponses_outils.append(
                    types.Part.from_function_response(name=nom, response={"resultat": resultat})
                )
            contents.append(types.Content(role="user", parts=reponses_outils))

        raise GeminiError("Le calcul est trop long à aboutir. Simplifie ta question.")

    def transcribe_image(self, image: tuple[bytes, str], consigne: str) -> str:
        """Transcrit l'énoncé d'une image en texte (pour interroger Pinecone)."""
        from google.genai import types

        data, mime = image
        contents = [types.Content(role="user", parts=[
            types.Part.from_text(text=consigne),
            types.Part.from_bytes(data=data, mime_type=mime),
        ])]
        try:
            response = self._call(
                contents, lambda: self._config("", "transcription", False))
            texte = self._text_of(self._parts(response)) or (getattr(response, "text", "") or "")
        except GeminiError:
            return ""
        texte = texte.strip()
        return "" if texte.upper().startswith("AUCUNE") else texte[:1500]
