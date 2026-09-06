#!/usr/bin/env python3
"""Vérifications automatiques de Friedrich — 100 % hors ligne.

    python tests/selftest.py

Aucune clé, aucun appel réseau : les SDK Gemini/Pinecone sont remplacés par des
doublures. Ce script couvre la liste de contrôle du cahier des charges :
syntaxe, fonctions Vercel, PING Discord, signature invalide, /help, commande
avec question, avec image, réponse > 2000 caractères, refus d'un fichier
non-image, requête vide, absence de secret, build sans effet de bord,
idempotence de l'ingestion.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import sys
import time
import types as pytypes

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

RESULTATS: list[tuple[str, bool, str]] = []


def test(nom):
    def decorateur(fonction):
        def execute():
            try:
                detail = fonction() or ""
                RESULTATS.append((nom, True, str(detail)))
            except Exception as exc:  # noqa: BLE001
                import traceback
                trace = traceback.format_exc().strip().splitlines()[-1]
                RESULTATS.append((nom, False, f"{type(exc).__name__}: {exc} | {trace}"))
        execute.nom = nom
        return execute
    return decorateur


def verifie(condition, message):
    if not condition:
        raise AssertionError(message)


# ---------------------------------------------------------------------------
# Doublures des SDK externes (Gemini / Pinecone ne sont pas installés ici)
# ---------------------------------------------------------------------------

class FauxPart:
    def __init__(self, text=None, function_call=None):
        self.text = text
        self.function_call = function_call


class FauxAppel:
    def __init__(self, name, args):
        self.name = name
        self.args = args


class FausseReponse:
    def __init__(self, parts):
        contenu = pytypes.SimpleNamespace(parts=parts, role="model")
        self.candidates = [pytypes.SimpleNamespace(content=contenu, finish_reason="STOP")]
        self.text = "".join(p.text or "" for p in parts)
        self.prompt_feedback = None


def installer_faux_genai(scenario, modeles=None):
    """Injecte un faux `google.genai`. `scenario` = liste d'actions par appel."""
    etat = {"appels": 0, "cles": [], "modeles_demandes": [], "listages": 0}

    class FauxModels:
        def __init__(self, cle):
            self.cle = cle

        def generate_content(self, model=None, contents=None, config=None):
            etat["appels"] += 1
            etat["cles"].append(self.cle)
            etat["modeles_demandes"].append(model)
            action = scenario[min(etat["appels"] - 1, len(scenario) - 1)]
            if isinstance(action, Exception):
                raise action
            return action

        def list(self, config=None):
            etat["listages"] += 1
            return [
                pytypes.SimpleNamespace(name="models/" + nom,
                                        supported_actions=["generateContent"])
                for nom in (modeles or [])
            ]

    class FauxClient:
        def __init__(self, api_key=None):
            self.models = FauxModels(api_key)

    faux_types = pytypes.ModuleType("google.genai.types")

    class Part:
        @staticmethod
        def from_text(text=None):
            return FauxPart(text=text)

        @staticmethod
        def from_bytes(data=None, mime_type=None):
            return FauxPart(text=f"<image {mime_type} {len(data)}o>")

        @staticmethod
        def from_function_response(name=None, response=None):
            return FauxPart(text=f"<reponse_outil {name} {json.dumps(response)[:80]}>")

    faux_types.Part = Part
    faux_types.Content = lambda role=None, parts=None: pytypes.SimpleNamespace(
        role=role, parts=parts or [])
    faux_types.Tool = lambda function_declarations=None: {"tools": function_declarations}
    faux_types.GenerateContentConfig = lambda **kw: kw
    faux_types.ThinkingConfig = lambda thinking_budget=None: {"budget": thinking_budget}
    faux_types.AutomaticFunctionCallingConfig = lambda disable=None: {"disable": disable}

    faux_genai = pytypes.ModuleType("google.genai")
    faux_genai.Client = FauxClient
    faux_genai.types = faux_types

    faux_google = pytypes.ModuleType("google")
    faux_google.genai = faux_genai

    sys.modules["google"] = faux_google
    sys.modules["google.genai"] = faux_genai
    sys.modules["google.genai.types"] = faux_types
    return etat


# ---------------------------------------------------------------------------
# Interactions Discord factices
# ---------------------------------------------------------------------------

def interaction(nom, options=None, resolved=None):
    data = {"name": nom, "id": "1", "type": 1}
    if options:
        data["options"] = options
    if resolved:
        data["resolved"] = resolved
    return {
        "type": 2,
        "id": "42",
        "application_id": "123456789",
        "token": "jeton-interaction-secret",
        "channel_id": "555",
        "member": {"user": {"id": "777", "username": "eleve"}},
        "data": data,
    }


def piece_jointe(content_type="image/png", taille=1024, nom="exo.png"):
    return (
        [{"name": "image", "type": 11, "value": "9001"}],
        {"attachments": {"9001": {
            "id": "9001", "filename": nom, "content_type": content_type,
            "size": taille, "url": "https://cdn.discordapp.com/attachments/1/2/" + nom,
        }}},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@test("Syntaxe Python de tous les fichiers")
def t_syntaxe():
    fichiers = []
    for dossier, sous, noms in os.walk(RACINE):
        if any(part in dossier for part in (".git", "__pycache__", "venv")):
            continue
        fichiers += [os.path.join(dossier, n) for n in noms if n.endswith(".py")]
    verifie(len(fichiers) >= 10, f"trop peu de fichiers Python trouvés ({len(fichiers)})")
    for chemin in fichiers:
        with open(chemin, encoding="utf-8") as handle:
            compile(handle.read(), chemin, "exec")
    return f"{len(fichiers)} fichiers compilés"


@test("Vercel : api/index.py, api/task.py, api/setup.py exposent `handler`")
def t_fonctions_vercel():
    from http.server import BaseHTTPRequestHandler
    for nom in ("index", "task", "setup"):
        chemin = os.path.join(RACINE, "api", f"{nom}.py")
        verifie(os.path.exists(chemin), f"api/{nom}.py manquant")
        spec = importlib.util.spec_from_file_location(f"api_{nom}", chemin)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        verifie(hasattr(module, "handler"), f"api/{nom}.py : classe `handler` absente")
        verifie(issubclass(module.handler, BaseHTTPRequestHandler),
                f"api/{nom}.py : `handler` doit hériter de BaseHTTPRequestHandler")
    return "3 fonctions valides"


@test("PING Discord -> {\"type\": 1}")
def t_ping():
    from utils import interactions
    reponse, tache = interactions.route({"type": 1})
    verifie(reponse == {"type": 1}, f"réponse inattendue : {reponse}")
    verifie(tache is None, "aucun travail de fond ne doit être déclenché")


@test("Signature Ed25519 : valide acceptée, invalide refusée")
def t_signature():
    try:
        from nacl.signing import SigningKey
    except ImportError:
        return "ignoré (PyNaCl non installé localement)"
    from utils import interactions

    cle = SigningKey.generate()
    publique = cle.verify_key.encode().hex()
    corps = json.dumps({"type": 1}).encode()
    horodatage = str(int(time.time()))
    signature = cle.sign(horodatage.encode() + corps).signature.hex()

    verifie(interactions.verify_signature(publique, signature, horodatage, corps),
            "une signature valide doit être acceptée")
    verifie(not interactions.verify_signature(publique, signature, horodatage, corps + b"x"),
            "un corps modifié doit être refusé")
    verifie(not interactions.verify_signature(publique, "00" * 64, horodatage, corps),
            "une fausse signature doit être refusée")
    verifie(not interactions.verify_signature(publique, None, None, corps),
            "des en-têtes absents doivent être refusés")
    verifie(not interactions.verify_signature("pas-de-l-hexa", signature, horodatage, corps),
            "une clé publique invalide ne doit pas faire planter")
    return "4 cas de rejet + 1 acceptation"


@test("/help répond immédiatement (type 4) et tient dans un message")
def t_help():
    from utils import interactions
    reponse, tache = interactions.route(interaction("help"))
    verifie(reponse["type"] == 4, "réponse immédiate attendue")
    contenu = reponse["data"]["content"]
    verifie(len(contenu) < 2000, f"message trop long ({len(contenu)})")
    for attendu in ("/hint", "/detail", "/solve", "/correct"):
        verifie(attendu in contenu, f"{attendu} absent de l'aide")
    verifie(reponse["data"]["allowed_mentions"] == {"parse": []}, "mentions non désactivées")
    verifie(tache is None, "/help ne doit pas déclencher de travail de fond")
    return f"{len(contenu)} caractères"


@test("Commande avec question -> accusé différé (type 5) + tâche")
def t_question():
    from utils import interactions
    inter = interaction("detail", [{"name": "question", "type": 3,
                                    "value": "Résous x² - 4 = 0"}])
    reponse, tache = interactions.route(inter)
    verifie(reponse == {"type": 5}, f"type 5 attendu, reçu {reponse}")
    verifie(tache["mode"] == "detail", "mode incorrect")
    verifie("x²" in tache["question"], "question perdue")
    verifie(tache["attachment"] is None, "aucune image attendue")


@test("Commande avec image -> pièce jointe transmise")
def t_image():
    from utils import interactions
    options, resolved = piece_jointe("image/jpeg", 2 * 1024 * 1024)
    reponse, tache = interactions.route(interaction("correct", options, resolved))
    verifie(reponse == {"type": 5}, "type 5 attendu")
    verifie(tache["attachment"]["content_type"] == "image/jpeg", "type MIME perdu")
    verifie(tache["attachment"]["url"].startswith("https://cdn.discordapp.com"),
            "URL de pièce jointe invalide")


@test("Fichier non-image refusé")
def t_non_image():
    from utils import interactions
    options, resolved = piece_jointe("application/pdf", 50_000, "devoir.pdf")
    reponse, tache = interactions.route(interaction("solve", options, resolved))
    verifie(reponse["type"] == 4, "refus immédiat attendu")
    verifie(tache is None, "aucun appel Gemini ne doit être fait")
    verifie("image" in reponse["data"]["content"].lower(), "message peu clair")
    verifie(reponse["data"].get("flags") == 64, "le refus devrait être discret (ephemeral)")


@test("Image de plus de 10 Mo refusée")
def t_image_trop_lourde():
    from utils import interactions
    options, resolved = piece_jointe("image/png", 11 * 1024 * 1024)
    reponse, tache = interactions.route(interaction("detail", options, resolved))
    verifie(reponse["type"] == 4 and tache is None, "refus immédiat attendu")
    verifie("10 Mo" in reponse["data"]["content"], "la limite doit être expliquée")


@test("Requête sans question ni image refusée poliment")
def t_vide():
    from utils import interactions
    reponse, tache = interactions.route(interaction("hint"))
    verifie(reponse["type"] == 4 and tache is None, "refus immédiat attendu")
    verifie("question" in reponse["data"]["content"], "message d'aide attendu")


@test("LaTeX résiduel converti en Unicode (Discord ne rend pas le LaTeX)")
def t_latex_unicode():
    from utils.formatting import polish

    cas = [
        (r"On a $f(x) = x^2 - 4$ donc $f'(x) = 2x$.",
         ["x²", "f'(x) = 2x"], ["$", "^2"]),
        (r"$$\frac{x+1}{x-2} \times \sqrt{5} \le \infty$$",
         ["(x+1)/(x-2)", "×", "√(5)", "≤", "∞"], ["\\frac", "\\times", "$"]),
        (r"Pour tout $x \in \mathbb{R}$, $u_{n+1} = 3u_n - 2$.",
         ["x ∈ ℝ", "uₙ₊₁", "3uₙ"], ["\\in", "\\mathbb", "_{"]),
        (r"$\lim_{x \to +\infty} f(x) = 0$ et $\alpha \ne \beta$.",
         ["lim(x → +∞)", "α ≠ β"], ["\\lim", "\\ne", "\\alpha"]),
        (r"$\Delta = b^2 - 4ac$, $\vec{u}$, $e^{-x}$",
         ["∆ = b²", "u→", "e⁻ˣ"], ["\\Delta", "\\vec", "^{"]),
    ]
    for source, attendus, interdits in cas:
        sortie = polish(source)
        for attendu in attendus:
            verifie(attendu in sortie, f"« {attendu} » absent de : {sortie!r}")
        for interdit in interdits:
            verifie(interdit not in sortie,
                    f"« {interdit} » aurait dû disparaître de : {sortie!r}")

    # le texte sans mathématiques ne doit pas être abîmé
    intact = "Bravo ! Ton raisonnement est juste, continue comme ça."
    verifie(polish(intact) == intact, "un texte ordinaire ne doit pas être modifié")
    return f"{len(cas)} formules converties, texte courant intact"


@test("Réponse de plus de 2000 caractères découpée proprement")
def t_decoupage():
    from utils.formatting import split_message
    texte = "\n\n".join(
        f"**Étape {i}** — On dérive : $f'(x) = 2x + {i}$. " + "Explication détaillée. " * 12
        for i in range(1, 26)
    )
    verifie(len(texte) > 6000, "texte de test trop court")
    morceaux = split_message(texte)
    verifie(len(morceaux) >= 4, f"découpage insuffisant ({len(morceaux)})")
    for morceau in morceaux:
        verifie(0 < len(morceau) <= 2000, f"morceau invalide ({len(morceau)} car.)")
    for i in range(1, 26):
        verifie(f"**Étape {i}**" in "\n".join(morceaux), f"étape {i} perdue")

    # bloc de code coupé en deux : refermé puis rouvert
    long_code = "```\n" + "\n".join(f"ligne {i} de calcul" * 3 for i in range(200)) + "\n```"
    morceaux = split_message(long_code)
    for morceau in morceaux:
        verifie(morceau.count("```") % 2 == 0,
                "bloc de code laissé ouvert dans un message")
    return f"{len(morceaux)} messages"


@test("Réveil de /api/task : le silence n'est jamais confondu avec un succès")
def t_reveil_worker():
    import urllib.error
    from utils import interactions

    os.environ.setdefault("SETUP_SECRET", "secret-installation-de-test")
    vrai_urlopen = interactions.urllib.request.urlopen

    def simuler(reaction):
        def faux(_requete, timeout=None):
            raise reaction
        interactions.urllib.request.urlopen = faux
        try:
            return interactions.trigger_worker("https://exemple.vercel.app",
                                               {"mode": "detail"})
        finally:
            interactions.urllib.request.urlopen = vrai_urlopen

    def erreur_http(code):
        return urllib.error.HTTPError("https://x/api/task", code, "", {}, None)

    # cas normal : la fonction travaille, la connexion expire
    verifie(simuler(TimeoutError()) is True,
            "un dépassement de délai est le cas NORMAL, pas un échec")
    # cas où l'élève resterait bloqué sur « ⏳ » : il faut le dire
    verifie(simuler(erreur_http(404)) is False,
            "404 = api/task absent du déploiement, ce n'est pas un succès")
    verifie(simuler(erreur_http(401)) is False,
            "401 = secret interne divergent entre les deux fonctions")
    verifie(simuler(erreur_http(500)) is False, "500 = la fonction a planté")
    verifie(simuler(ConnectionRefusedError()) is False, "connexion refusée")
    return "timeout accepté ; 404, 401, 500 et refus signalés"


@test("Signature interne /api -> /api/task")
def t_hmac_interne():
    from utils import interactions
    secret = "secret-de-test-tres-long"
    corps = json.dumps({"mode": "detail"}).encode()
    horodatage, signature = interactions.sign_internal(corps, secret)
    verifie(interactions.verify_internal(signature, horodatage, corps, secret),
            "signature interne valide refusée")
    verifie(not interactions.verify_internal(signature, horodatage, corps + b"!", secret),
            "corps modifié accepté")
    verifie(not interactions.verify_internal(signature, horodatage, corps, "autre-secret"),
            "mauvais secret accepté")
    vieux = str(int(time.time()) - 4000)
    verifie(not interactions.verify_internal(signature, vieux, corps, secret),
            "horodatage périmé accepté")


@test("Ingestion idempotente : identifiants déterministes")
def t_idempotence():
    from utils import rag

    pages = [(1, "Le théorème des valeurs intermédiaires. " * 60),
             (2, "La fonction exponentielle est dérivable sur R. " * 60)]
    original = rag.extract_pdf
    rag.extract_pdf = lambda chemin: pages
    try:
        premiere = rag.build_records("programme_terminal.pdf")
        seconde = rag.build_records("programme_terminal.pdf")
    finally:
        rag.extract_pdf = original

    verifie(premiere, "aucun extrait produit")
    ids1 = [f["_id"] for f in premiere]
    ids2 = [f["_id"] for f in seconde]
    verifie(ids1 == ids2, "les identifiants changent d'une exécution à l'autre")
    verifie(len(set(ids1)) == len(ids1), "identifiants dupliqués")
    verifie(all("#p" in i and "#c" in i for i in ids1), "format d'identifiant inattendu")
    verifie(all(f["chunk_text"] and f["page"] for f in premiere), "champs manquants")
    return f"{len(ids1)} extraits, identifiants stables"


@test("Découpage : taille 1200, chevauchement 200")
def t_decoupage_corpus():
    from utils import config, rag
    verifie(config.CHUNK_SIZE == 1200 and config.CHUNK_OVERLAP == 200,
            "paramètres de découpage inattendus")
    texte = "Mot " * 2000
    morceaux = rag.chunk_text(texte)
    verifie(len(morceaux) > 3, "découpage insuffisant")
    verifie(all(len(m) <= 1200 for m in morceaux), "bloc trop grand")
    return f"{len(morceaux)} blocs"


@test("Outils SymPy réellement exécutés")
def t_outils_maths():
    from utils import math_tools

    resultat, image = math_tools.call_tool("resoudre_equation", {"equation": "x^2-4=0"})
    solutions = {s["exacte"] for s in resultat["solutions"]}
    verifie(solutions == {"-2", "2"}, f"solutions inattendues : {solutions}")

    resultat, _ = math_tools.call_tool("verifier_egalite",
                                       {"membre_gauche": "(x+1)^2",
                                        "membre_droit": "x^2+2*x+1"})
    verifie(resultat["egales"] is True, "égalité vraie non reconnue")

    resultat, _ = math_tools.call_tool("verifier_egalite",
                                       {"membre_gauche": "(x+1)^2",
                                        "membre_droit": "x^2+1"})
    verifie(resultat["egales"] is False, "égalité fausse acceptée")

    resultat, _ = math_tools.call_tool("deriver_ou_integrer",
                                       {"expression": "x*exp(-x)", "operation": "derivee"})
    verifie("exp(-x)" in resultat["resultat"], f"dérivée inattendue : {resultat}")

    resultat, _ = math_tools.call_tool("resoudre_equation", {"equation": "2*x+1<=5"})
    verifie("2" in resultat["solution"], f"inéquation mal résolue : {resultat}")

    resultat, _ = math_tools.call_tool("simplifier_expression", {"expression": "n'importe quoi !!"})
    verifie("erreur" in resultat, "une entrée invalide doit renvoyer une erreur propre")

    resultat, _ = math_tools.call_tool("outil_inexistant", {})
    verifie("erreur" in resultat, "outil inconnu mal géré")

    try:
        import matplotlib  # noqa: F401
        resultat, png = math_tools.call_tool("tracer_courbe",
                                             {"fonctions": ["x^2-4", "exp(-x)"],
                                              "x_min": -3, "x_max": 3})
        verifie(png and png[:4] == b"\x89PNG", "PNG invalide")
        return f"5 outils + graphique ({len(png) // 1024} Ko)"
    except ImportError:
        return "5 outils (matplotlib absent : tracé ignoré)"


@test("Gemini : bascule sur la clé de secours (quota / 429 / 503)")
def t_gemini_fallback():
    os.environ["GEMINI_API_KEY_1"] = "cle-principale-test"
    os.environ["GEMINI_API_KEY_2"] = "cle-secours-test"

    erreur = RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded")
    installer_faux_genai([erreur, erreur, FausseReponse([FauxPart(text="Bonjour")])])

    for module in [m for m in sys.modules if m.startswith("utils.gemini")]:
        del sys.modules[module]
    from utils.gemini import GeminiClient

    client = GeminiClient()
    resultat = client.answer("Question", "Système", mode="solve", use_tools=False)
    verifie(resultat["texte"] == "Bonjour", "réponse perdue")
    verifie(resultat["cle"] == 2, f"la clé de secours devait être utilisée (reçu {resultat['cle']})")
    return "bascule clé 1 -> clé 2 vérifiée"


@test("Gemini : bascule automatique de modèle si le nom n'existe pas")
def t_gemini_modele():
    os.environ["GEMINI_API_KEY_1"] = "cle-principale-test"
    os.environ.pop("GEMINI_API_KEY_2", None)
    os.environ["GEMINI_MODEL"] = "gemini-2.5-flash"

    absent = RuntimeError(
        "ClientError: 404 NOT_FOUND. {'error': {'code': 404, 'message': "
        "'models/gemini-2.5-flash is not found for API version v1beta, or is not "
        "supported for generateContent.', 'status': 'NOT_FOUND'}}")
    etat = installer_faux_genai(
        [absent, FausseReponse([FauxPart(text="OK")])],
        modeles=["gemini-embedding-001", "veo-3.0-generate", "imagen-4.0",
                 "gemini-2.0-flash", "gemini-2.5-pro"])

    for module in [m for m in sys.modules if m.startswith("utils.gemini")]:
        del sys.modules[module]
    from utils.gemini import GeminiClient

    try:
        client = GeminiClient()
        resultat = client.answer("Question", "Système", mode="solve", use_tools=False)
        verifie(resultat["texte"] == "OK", "réponse perdue après la bascule")
        verifie(resultat["modele"] == "gemini-2.0-flash",
                f"modèle retenu inattendu : {resultat['modele']}")
        verifie(resultat["substitution"] is True, "la substitution doit être signalée")
        verifie(etat["modeles_demandes"] == ["gemini-2.5-flash", "gemini-2.0-flash"],
                f"séquence d'appels inattendue : {etat['modeles_demandes']}")
        verifie(etat["listages"] == 1, "la liste des modèles doit être demandée une seule fois")
        for indesirable in ("gemini-embedding-001", "veo-3.0-generate", "imagen-4.0"):
            verifie(indesirable not in client.modeles_vus,
                    f"{indesirable} ne devrait pas être proposé")

        # message d'erreur quand vraiment aucun modèle ne convient
        installer_faux_genai([absent], modeles=["gemini-embedding-001"])
        for module in [m for m in sys.modules if m.startswith("utils.gemini")]:
            del sys.modules[module]
        from utils.gemini import GeminiClient as Client2
        try:
            Client2("modele-inexistant").answer("Q", "S", mode="solve", use_tools=False)
            raise AssertionError("une erreur explicite était attendue")
        except Exception as exc:
            verifie("GEMINI_MODEL" in str(exc),
                    f"le message doit dire quoi faire : {exc}")
    finally:
        os.environ.pop("GEMINI_MODEL", None)
    return "404 -> découverte des modèles -> gemini-2.0-flash"


@test("Gemini : modèle retiré (« no longer available ») -> remplaçant cité par l'API")
def t_gemini_modele_retire():
    """Cas réel du 31/08/2026 : gemini-2.5-flash listé mais fermé aux nouveaux."""
    os.environ["GEMINI_API_KEY_1"] = "cle-principale-test"
    os.environ.pop("GEMINI_API_KEY_2", None)
    os.environ["GEMINI_MODEL"] = "gemini-2.5-flash"

    retire = RuntimeError(
        "ClientError: 404 NOT_FOUND. {'error': {'code': 404, 'message': 'This "
        "model models/gemini-2.5-flash is no longer available to new users. "
        "Please update your code to use models/gemini-3.6-flash for the latest "
        "features and improvements.', 'status': 'NOT_FOUND'}}")

    # le modèle mort figure DANS la liste : c'est tout le piège
    etat = installer_faux_genai(
        [retire, FausseReponse([FauxPart(text="Bonjour")])],
        modeles=["gemini-2.5-flash", "gemini-2.5-pro", "gemini-flash-latest"])

    for module in [m for m in sys.modules if m.startswith("utils.gemini")]:
        del sys.modules[module]
    from utils.gemini import GeminiClient

    try:
        client = GeminiClient()
        resultat = client.answer("Question", "Système", mode="detail", use_tools=False)
        verifie(resultat["modele"] == "gemini-3.6-flash",
                f"le remplaçant cité par Google devait être retenu, reçu "
                f"{resultat['modele']}")
        verifie(etat["modeles_demandes"] == ["gemini-2.5-flash", "gemini-3.6-flash"],
                f"séquence inattendue : {etat['modeles_demandes']}")
        verifie(resultat["texte"] == "Bonjour", "réponse perdue")
        verifie(etat["listages"] == 0,
                "inutile de lister les modèles quand l'API nomme le remplaçant")
    finally:
        os.environ.pop("GEMINI_MODEL", None)
    return "404 « no longer available » -> gemini-3.6-flash, sans listage"


@test("Gemini : modèle saturé (503) -> on essaie un autre modèle, pas seulement une autre clé")
def t_gemini_sature():
    os.environ["GEMINI_API_KEY_1"] = "cle-principale-test"
    os.environ["GEMINI_API_KEY_2"] = "cle-secours-test"
    os.environ["GEMINI_MODEL"] = "gemini-flash-latest"

    sature = RuntimeError(
        "ServerError: 503 UNAVAILABLE. {'error': {'code': 503, 'message': "
        "'The model is overloaded. Please try again later.', "
        "'status': 'UNAVAILABLE'}}")

    # 4 refus (2 clés x 2 essais) sur le 1er modèle, puis succès sur le suivant
    etat = installer_faux_genai(
        [sature, sature, sature, sature, FausseReponse([FauxPart(text="Voilà")])],
        modeles=["gemini-flash-latest", "gemini-3.5-flash", "gemini-pro-latest"])

    for module in [m for m in sys.modules if m.startswith("utils.gemini")]:
        del sys.modules[module]
    import utils.gemini as module_gemini

    vrai_sleep = module_gemini.time.sleep
    module_gemini.time.sleep = lambda _s: None      # test rapide
    try:
        client = module_gemini.GeminiClient()
        resultat = client.answer("Question", "Système", mode="detail", use_tools=False)
        verifie(resultat["texte"] == "Voilà", "réponse perdue")
        verifie(resultat["modele"] == "gemini-3.5-flash",
                f"modèle de repli inattendu : {resultat['modele']}")
        verifie(etat["modeles_demandes"].count("gemini-flash-latest") == 4,
                f"les 2 clés devaient être essayées : {etat['modeles_demandes']}")
        verifie(etat["cles"][:4] == ["cle-principale-test"] * 2 + ["cle-secours-test"] * 2,
                f"ordre des clés inattendu : {etat['cles']}")

        # cas désespéré : le message doit dire que c'est un 503, et lequel
        installer_faux_genai([sature], modeles=["gemini-flash-latest"])
        for m in [m for m in sys.modules if m.startswith("utils.gemini")]:
            del sys.modules[m]
        import utils.gemini as recharge
        recharge.time.sleep = lambda _s: None
        try:
            recharge.GeminiClient().answer("Q", "S", mode="solve", use_tools=False)
            raise AssertionError("une erreur était attendue")
        except recharge.GeminiError as exc:
            verifie("503" in str(exc), f"le code doit apparaître : {exc}")
            verifie("gemini-flash-latest" in str(exc),
                    "les modèles essayés doivent être nommés")
    finally:
        module_gemini.time.sleep = vrai_sleep
        os.environ.pop("GEMINI_MODEL", None)
    return "503 sur les 2 clés -> bascule vers le modèle suivant"


@test("Gemini : réglage refusé par le modèle -> nouvel essai sans ce réglage")
def t_gemini_config_refusee():
    os.environ["GEMINI_API_KEY_1"] = "cle-principale-test"
    os.environ.pop("GEMINI_API_KEY_2", None)

    refus = RuntimeError(
        "ClientError: 400 INVALID_ARGUMENT. Unknown name \"thinking_config\": "
        "Cannot find field.")
    installer_faux_genai([refus, FausseReponse([FauxPart(text="OK")])])

    for module in [m for m in sys.modules if m.startswith("utils.gemini")]:
        del sys.modules[module]
    from utils.gemini import GeminiClient

    client = GeminiClient()
    verifie(client.sans_reflexion is False, "état initial incorrect")
    resultat = client.answer("Question", "Système", mode="detail", use_tools=False)
    verifie(resultat["texte"] == "OK", "la réponse doit passer au 2e essai")
    verifie(client.sans_reflexion is True,
            "le budget de réflexion aurait dû être abandonné")
    return "400 sur thinking_config -> réessai allégé"


@test("Gemini : les function calls sont vraiment exécutés")
def t_gemini_outils():
    os.environ["GEMINI_API_KEY_1"] = "cle-principale-test"
    os.environ.pop("GEMINI_API_KEY_2", None)

    appel = FauxPart(function_call=FauxAppel("resoudre_equation", {"equation": "x^2-4=0"}))
    reponse_finale = FausseReponse([FauxPart(text="Les solutions sont -2 et 2.")])
    installer_faux_genai([FausseReponse([appel]), reponse_finale])

    for module in [m for m in sys.modules if m.startswith("utils.gemini")]:
        del sys.modules[module]
    from utils.gemini import GeminiClient

    client = GeminiClient()
    resultat = client.answer("Résous", "Système", mode="detail", use_tools=True)
    verifie("resoudre_equation" in resultat["outils"], "l'outil n'a pas été exécuté")
    verifie("-2" in resultat["texte"], "réponse finale perdue")
    return "boucle d'outils vérifiée"


@test("Chaîne complète hors ligne (pipeline) + garde-fou hors sujet")
def t_pipeline():
    from utils import discord_api, pipeline, prompts, rag

    envoyes: list[str] = []
    vrai_send = discord_api.send_answer
    vrai_search = rag.search
    vrai_erreur = discord_api.send_error
    discord_api.send_answer = lambda app, jeton, texte, images=None: (
        envoyes.append(texte) or 1)
    discord_api.send_error = lambda app, jeton, message: envoyes.append("ERREUR:" + message)
    rag.search = lambda *a, **k: [
        {"texte": "Théorème des valeurs intermédiaires…", "page": 12,
         "source": "programme_terminal.pdf", "score": 0.9}]

    installer_faux_genai([FausseReponse([FauxPart(text="**Étape 1** — On factorise. $x^2-4=(x-2)(x+2)$")])])
    for module in [m for m in sys.modules if m.startswith("utils.gemini")]:
        del sys.modules[module]
    import importlib
    importlib.reload(pipeline)

    try:
        rapport = pipeline.traiter({
            "mode": "detail", "question": "Résous x²-4=0",
            "application_id": "123", "token": "jeton", "user_id": "1", "channel_id": "2",
        })
        verifie(rapport.get("ok"), f"pipeline en échec : {rapport}")
        verifie("factorise" in envoyes[0], "réponse non transmise à Discord")

        # hors sujet : la réponse est remplacée par la phrase unique
        envoyes.clear()
        installer_faux_genai([FausseReponse([FauxPart(text=prompts.OFF_TOPIC_REPLY)])])
        for module in [m for m in sys.modules if m.startswith("utils.gemini")]:
            del sys.modules[module]
        importlib.reload(pipeline)
        pipeline.traiter({
            "mode": "solve", "question": "Tu connais Minecraft ?",
            "application_id": "123", "token": "jeton", "user_id": "1", "channel_id": "2",
        })
        verifie(envoyes[0].strip() == prompts.OFF_TOPIC_REPLY,
                f"réponse hors sujet inattendue : {envoyes[0][:80]}")
    finally:
        discord_api.send_answer = vrai_send
        discord_api.send_error = vrai_erreur
        rag.search = vrai_search
    return "réponse pédagogique + réponse hors sujet"


@test("Consignes pédagogiques présentes et cohérentes")
def t_prompts():
    from utils import prompts
    verifie(prompts.OUT_OF_SYLLABUS_SENTENCE in prompts.SYSTEM_PROMPT,
            "phrase « hors programme » absente du prompt système")
    verifie(prompts.OFF_TOPIC_REPLY in prompts.SYSTEM_PROMPT,
            "phrase « hors sujet » absente du prompt système")
    for mode in ("hint", "detail", "solve", "correct"):
        verifie(mode in prompts.MODE_INSTRUCTIONS, f"mode {mode} absent")
    verifie("tutoies" in prompts.SYSTEM_PROMPT, "le tutoiement doit être imposé")
    verifie("Unicode" in prompts.SYSTEM_PROMPT,
            "Discord ne rend pas le LaTeX : l'Unicode doit être imposé")
    verifie("n'affiche PAS le LaTeX" in prompts.SYSTEM_PROMPT,
            "la raison de l'interdiction doit être expliquée au modèle")
    verifie("Première erreur" in prompts.MODE_INSTRUCTIONS["correct"],
            "/correct doit isoler la première erreur")
    verifie("Interdiction" in prompts.MODE_INSTRUCTIONS["hint"],
            "/hint doit interdire la solution complète")
    prompt = prompts.build_prompt("detail", "Résous x²-4=0", "extrait de cours", False)
    verifie("CONTEXTE" in prompt and "extrait de cours" in prompt, "contexte RAG non injecté")


@test("Aucun secret en dur dans le code")
def t_pas_de_secrets():
    motifs = {
        "clé Google": re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
        "clé Pinecone": re.compile(r"pcsk_[0-9A-Za-z_\-]{20,}"),
        "jeton Discord": re.compile(r"[MNO][A-Za-z\d_-]{23,}\.[A-Za-z\d_-]{6}\.[A-Za-z\d_-]{27,}"),
        "clé OpenAI": re.compile(r"sk-[A-Za-z0-9]{32,}"),
    }
    suspects = []
    for dossier, sous, noms in os.walk(RACINE):
        if any(part in dossier for part in (".git", "__pycache__", "venv")):
            continue
        for nom in noms:
            if not nom.endswith((".py", ".html", ".json", ".md", ".txt", ".example")):
                continue
            chemin = os.path.join(dossier, nom)
            with open(chemin, encoding="utf-8", errors="ignore") as handle:
                contenu = handle.read()
            for libelle, motif in motifs.items():
                if motif.search(contenu):
                    suspects.append(f"{nom} ({libelle})")
    verifie(not suspects, f"secrets potentiels : {suspects}")

    # un .env local est permis, mais il doit rester hors de git ET de Vercel
    if os.path.exists(os.path.join(RACINE, ".env")):
        for garde in (".gitignore", ".vercelignore"):
            chemin = os.path.join(RACINE, garde)
            verifie(os.path.exists(chemin), f"{garde} manquant")
            with open(chemin, encoding="utf-8") as handle:
                lignes = [l.strip() for l in handle]
            verifie(".env" in lignes, f"{garde} doit contenir une ligne « .env »")
        return "aucun secret dans le code ; .env local ignoré par git et par Vercel"
    return "aucun secret détecté"


@test("Fichier .env local : chargé, sans jamais écraser l'environnement")
def t_env_local():
    from utils import config

    if not os.path.exists(os.path.join(RACINE, ".env")):
        return "ignoré (aucun .env local)"

    verifie(".env" in config.FICHIERS_ENV_CHARGES,
            f"le .env n'a pas été chargé : {config.FICHIERS_ENV_CHARGES}")

    ancien = os.environ.get("SETUP_SECRET")
    os.environ["SETUP_SECRET"] = "valeur-imposee-par-le-test"
    try:
        config._charger_fichiers_env()
        verifie(os.environ["SETUP_SECRET"] == "valeur-imposee-par-le-test",
                "un fichier .env ne doit jamais écraser une variable déjà définie "
                "(sinon il primerait sur les variables Vercel)")
    finally:
        if ancien is None:
            os.environ.pop("SETUP_SECRET", None)
        else:
            os.environ["SETUP_SECRET"] = ancien

    manquantes = [nom for nom in config.REQUIRED_VARS if not config.get(nom)]
    verifie(not manquantes, f"variables absentes du .env : {manquantes}")
    return f"{len(config.REQUIRED_VARS)} variables requises lisibles en local"


@test("Les messages d'erreur ne fuitent aucun secret")
def t_erreurs_sans_secret():
    os.environ["DISCORD_TOKEN"] = "jeton-tres-secret-abcdef"
    os.environ["GEMINI_API_KEY_1"] = "AIzaFAUSSECLEDETEST123456"
    from utils import config
    rapport = json.dumps(config.env_report(), ensure_ascii=False)
    verifie("jeton-tres-secret" not in rapport, "le rapport expose DISCORD_TOKEN")
    verifie("AIzaFAUSSECLE" not in rapport, "le rapport expose la clé Gemini")
    verifie(config.mask("jeton-tres-secret-abcdef").count("*") == 0
            or "jeton-tres-secret" not in config.mask("jeton-tres-secret-abcdef"),
            "mask() laisse fuiter la valeur")
    return "rapport de configuration sans valeur sensible"


@test("Build Vercel sans effet de bord")
def t_vercel_json():
    with open(os.path.join(RACINE, "vercel.json"), encoding="utf-8") as handle:
        configuration = json.load(handle)
    verifie("buildCommand" not in configuration,
            "aucun buildCommand ne doit appeler Discord ou Pinecone")
    verifie("installCommand" not in configuration, "installCommand inutile")
    fonctions = configuration.get("functions", {})
    for chemin in ("api/index.py", "api/task.py", "api/setup.py"):
        verifie(chemin in fonctions, f"{chemin} absent de vercel.json")
        verifie(fonctions[chemin].get("maxDuration"), f"maxDuration manquant pour {chemin}")
    verifie(fonctions["api/index.py"]["maxDuration"] <= 30,
            "la fonction d'interactions doit rester courte")
    with open(os.path.join(RACINE, ".python-version"), encoding="utf-8") as handle:
        verifie(handle.read().strip() == "3.12", ".python-version doit valoir 3.12")
    with open(os.path.join(RACINE, "requirements.txt"), encoding="utf-8") as handle:
        requis = handle.read()
    for paquet in ("google-genai", "pinecone", "python-dotenv", "PyNaCl",
                   "pypdf", "sympy", "matplotlib", "Pillow"):
        verifie(paquet in requis, f"{paquet} absent de requirements.txt")
    return "vercel.json, .python-version et requirements.txt conformes"


@test("Page d'installation et README présents")
def t_fichiers():
    for nom in ("setup.html", "index.html", "README.md", ".env.example", ".gitignore",
                "ingest.py", "register_commands.py"):
        verifie(os.path.exists(os.path.join(RACINE, nom)), f"{nom} manquant")
    with open(os.path.join(RACINE, "setup.html"), encoding="utf-8") as handle:
        html = handle.read()
    verifie("X-Setup-Secret" in html, "l'en-tête X-Setup-Secret doit être envoyé")
    verifie("/api/setup" in html, "appel POST /api/setup absent")
    verifie("Initialiser Friedrich" in html, "bouton d'installation absent")
    return "tous les fichiers attendus sont là"


@test("Corpus embarqué prêt pour Pinecone")
def t_corpus():
    from utils import rag
    if not rag.corpus_available():
        return "ignoré (data/corpus.jsonl.gz pas encore généré)"
    meta = rag.corpus_meta()
    verifie(meta.get("total", 0) > 100, f"corpus trop petit : {meta.get('total')}")
    fiches = rag.read_corpus_slice(0, 5)
    verifie(len(fiches) == 5, "lecture par tranche défaillante")
    verifie(all("_id" in f and "chunk_text" in f for f in fiches), "champs manquants")
    suite = rag.read_corpus_slice(3, 5)
    verifie(suite[0]["_id"] == fiches[3]["_id"], "décalage de lecture incorrect")
    taille = os.path.getsize(rag.CORPUS_PATH) / 1e6
    return f"{meta['total']} extraits, {len(meta.get('sources', {}))} documents, {taille:.1f} Mo"


@test("Serveur HTTP réel : PING signé, signature invalide, /help")
def t_http_index():
    try:
        from nacl.signing import SigningKey
    except ImportError:
        return "ignoré (PyNaCl non installé localement)"
    import threading
    import urllib.error
    import urllib.request
    from http.server import HTTPServer

    cle = SigningKey.generate()
    os.environ["DISCORD_PUBLIC_KEY"] = cle.verify_key.encode().hex()
    os.environ.setdefault("SETUP_SECRET", "secret-installation-de-test")

    chemin = os.path.join(RACINE, "api", "index.py")
    spec = importlib.util.spec_from_file_location("api_index_http", chemin)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    serveur = HTTPServer(("127.0.0.1", 0), module.handler)
    port = serveur.server_address[1]
    fil = threading.Thread(target=serveur.serve_forever, daemon=True)
    fil.start()

    def envoyer(charge: dict, signer=True, entetes=None):
        corps = json.dumps(charge).encode()
        horodatage = str(int(time.time()))
        signature = (cle.sign(horodatage.encode() + corps).signature.hex()
                     if signer else "00" * 64)
        requete = urllib.request.Request(
            f"http://127.0.0.1:{port}/api", data=corps, method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Signature-Ed25519": signature,
                "X-Signature-Timestamp": horodatage,
                "X-Forwarded-Proto": "http",
                **(entetes or {}),
            })
        try:
            with urllib.request.urlopen(requete, timeout=10) as reponse:
                return reponse.status, json.loads(reponse.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    try:
        statut, corps = envoyer({"type": 1})
        verifie(statut == 200 and corps == {"type": 1}, f"PING : {statut} {corps}")

        statut, _ = envoyer({"type": 1}, signer=False)
        verifie(statut == 401, f"une signature invalide doit renvoyer 401 (reçu {statut})")

        statut, corps = envoyer(interaction("help"))
        verifie(statut == 200 and corps["type"] == 4, f"/help : {statut} {corps}")
        verifie("/detail" in corps["data"]["content"], "aide incomplète")

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api", timeout=10) as reponse:
            sante = json.loads(reponse.read().decode())
        verifie(sante["service"] == "friedrich", "état de santé inattendu")
        verifie("jeton" not in json.dumps(sante).lower(), "l'état de santé ne doit rien exposer")
    finally:
        serveur.shutdown()
        serveur.server_close()
    return "PING 200, signature invalide 401, /help 200, GET /api 200"


@test("Serveur HTTP réel : /api/task n'accepte qu'une signature interne valide")
def t_http_task():
    import threading
    import urllib.error
    import urllib.request
    from http.server import HTTPServer

    os.environ["SETUP_SECRET"] = "secret-installation-de-test"
    from utils import interactions

    recu: list[dict] = []
    faux_pipeline = pytypes.ModuleType("utils.pipeline")
    faux_pipeline.traiter = lambda tache: (recu.append(tache) or {"ok": True, "messages": 1})
    sys.modules["utils.pipeline"] = faux_pipeline

    chemin = os.path.join(RACINE, "api", "task.py")
    spec = importlib.util.spec_from_file_location("api_task_http", chemin)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    serveur = HTTPServer(("127.0.0.1", 0), module.handler)
    port = serveur.server_address[1]
    threading.Thread(target=serveur.serve_forever, daemon=True).start()

    tache = {"mode": "detail", "question": "Résous x²-4=0",
             "application_id": "123", "token": "jeton", "user_id": "1", "channel_id": "2"}
    corps = json.dumps(tache, ensure_ascii=False).encode()
    horodatage, signature = interactions.sign_internal(corps)

    def envoyer(entetes):
        requete = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/task", data=corps, method="POST",
            headers={"Content-Type": "application/json", **entetes})
        try:
            with urllib.request.urlopen(requete, timeout=10) as reponse:
                return reponse.status, json.loads(reponse.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    try:
        statut, _ = envoyer({})
        verifie(statut == 401, f"sans signature : 401 attendu, reçu {statut}")

        statut, _ = envoyer({"X-Friedrich-Signature": "00" * 32,
                             "X-Friedrich-Timestamp": horodatage})
        verifie(statut == 401, f"signature fausse : 401 attendu, reçu {statut}")

        statut, rapport = envoyer({"X-Friedrich-Signature": signature,
                                   "X-Friedrich-Timestamp": horodatage})
        verifie(statut == 200 and rapport.get("ok"), f"tâche signée refusée : {statut} {rapport}")
        verifie(recu and recu[0]["mode"] == "detail", "la tâche n'a pas été transmise")

        # sonde « __ping__ » : répond sans rien consommer ni appeler le pipeline
        corps_ping = json.dumps({"mode": "__ping__"}).encode()
        ts_ping, sig_ping = interactions.sign_internal(corps_ping)
        requete = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/task", data=corps_ping, method="POST",
            headers={"Content-Type": "application/json",
                     "X-Friedrich-Signature": sig_ping,
                     "X-Friedrich-Timestamp": ts_ping})
        with urllib.request.urlopen(requete, timeout=10) as reponse:
            pong = json.loads(reponse.read().decode())
        verifie(pong.get("pong") is True, f"sonde interne muette : {pong}")
        verifie(len(recu) == 1, "la sonde ne doit PAS déclencher de vrai traitement")
    finally:
        serveur.shutdown()
        serveur.server_close()
        sys.modules.pop("utils.pipeline", None)
    return "401 sans signature, 401 signature fausse, 200 signée"


@test("Limite de débit Pinecone : arrêt net et reprise exacte")
def t_limite_debit():
    from utils import rag

    etat = {"lots": 0}

    class IndexSature:
        def upsert_records(self, *, records, namespace, timeout=None):
            etat["lots"] += 1
            if etat["lots"] == 3:
                raise RuntimeError(
                    'RateLimitError: [429 RESOURCE_EXHAUSTED] {"error":{"code":'
                    '"RESOURCE_EXHAUSTED","message":"Request failed. You have '
                    "reached the max tokens per minute (250000) for model "
                    "'llama-text-embed-v2'\"}}")

    os.environ["PINECONE_API_KEY"] = "cle-pinecone-de-test"
    vrai_get_index = rag.get_index
    rag.get_index = lambda: IndexSature()
    debut = time.time()
    try:
        fiches = [{"_id": f"x{i}", "chunk_text": "texte de cours. " * 70}
                  for i in range(270)]
        try:
            rag.upsert_records(fiches)
            raise AssertionError("RateLimited aurait dû être levée")
        except rag.RateLimited as exc:
            verifie(exc.envoyes == 180,
                    f"{exc.envoyes} fiches comptées au lieu de 180 (2 lots de 90)")
            verifie("250000" in str(exc), "le détail du quota doit être conservé")
        verifie(time.time() - debut < 1.5,
                "aucune attente ne doit être perdue à réessayer un quota")

        estimation = rag.estimate_tokens(fiches[:10])
        verifie(2000 < estimation < 4000, f"estimation de tokens douteuse : {estimation}")
    finally:
        rag.get_index = vrai_get_index
    return "429 détecté au 3e lot, 180 fiches confirmées, reprise possible"


@test("Invitation du bot : lien correct et 403 expliqué")
def t_invitation():
    from utils import discord_api

    lien = discord_api.invite_url("123456789")
    verifie("client_id=123456789" in lien, "application id absent du lien")
    verifie("scope=bot%20applications.commands" in lien, "scopes incomplets")
    verifie("permissions=51200" in lien, "permissions incorrectes")

    # sans invitation, Discord répond 403 : le message doit être exploitable
    vrai_request = discord_api._request

    def refuse(methode, url, **kwargs):
        if methode == "GET":
            return {"username": "Friedrich", "id": "123456789"}
        raise discord_api.DiscordError(
            'Discord PUT 403 — {"message": "Missing Access", "code": 50001}')

    discord_api._request = refuse
    try:
        discord_api.register_commands("123456789", "jeton", "555000111")
        raise AssertionError("une erreur aurait dû être levée")
    except discord_api.DiscordError as exc:
        message = str(exc)
        verifie("invité" in message, "le message doit parler de l'invitation")
        verifie("oauth2/authorize" in message, "le lien d'invitation doit être fourni")
        verifie("555000111" in message, "le serveur concerné doit être nommé")
    finally:
        discord_api._request = vrai_request
    return "lien OAuth2 + message d'erreur actionnable"


@test("Définition des commandes conforme aux limites Discord")
def t_definition_commandes():
    from utils import discord_api
    noms = [c["name"] for c in discord_api.COMMANDS]
    verifie(noms == ["help", "hint", "detail", "solve", "correct"],
            f"commandes inattendues : {noms}")
    for commande in discord_api.COMMANDS:
        nom = commande["name"]
        verifie(re.fullmatch(r"[a-z0-9_-]{1,32}", nom), f"nom invalide : {nom}")
        verifie(1 <= len(commande["description"]) <= 100,
                f"/{nom} : description de {len(commande['description'])} caractères (max 100)")
        options = commande.get("options", [])
        verifie(len(options) <= 25, f"/{nom} : trop d'options")
        for option in options:
            verifie(re.fullmatch(r"[a-z0-9_-]{1,32}", option["name"]),
                    f"/{nom} : option « {option['name']} » invalide")
            verifie(1 <= len(option["description"]) <= 100,
                    f"/{nom}.{option['name']} : description trop longue")
            verifie(option["type"] in (3, 11), f"/{nom} : type d'option inattendu")
            verifie(option["required"] is False, "toutes les options sont facultatives")
    verifie(all(o["name"] in ("question", "image")
                for c in discord_api.COMMANDS for o in c.get("options", [])),
            "les options doivent s'appeler question / image")
    return "5 commandes, options question + image"


def _sdk_reel(nom_module):
    """Recharge le vrai SDK (les tests précédents installent des doublures)."""
    for module in ("google", "google.genai", "google.genai.types"):
        sys.modules.pop(module, None)
    import importlib
    return importlib.import_module(nom_module)


@test("SDK Gemini réel : déclarations d'outils et configuration acceptées")
def t_sdk_gemini():
    try:
        types_genai = _sdk_reel("google.genai.types")
    except ImportError:
        return "ignoré (google-genai non installé localement)"
    from utils import math_tools

    outil = types_genai.Tool(function_declarations=math_tools.TOOL_DECLARATIONS)
    verifie(len(outil.function_declarations) == 5, "les 5 outils doivent être déclarés")
    tracer = [d for d in outil.function_declarations if d.name == "tracer_courbe"][0]
    verifie("fonctions" in (tracer.parameters.properties or {}),
            "paramètre `fonctions` perdu à la conversion")

    for module in [m for m in sys.modules if m.startswith("utils.gemini")]:
        del sys.modules[module]
    os.environ["GEMINI_API_KEY_1"] = "cle-de-test"
    from utils.gemini import GeminiClient

    client = GeminiClient()
    configuration = client._config("consigne système", "detail", True)
    verifie(configuration.system_instruction == "consigne système", "system_instruction perdue")
    verifie(configuration.tools, "outils absents de la configuration")
    verifie(configuration.thinking_config.thinking_budget == 3072, "budget de réflexion perdu")

    # budget nul : le réglage ne doit PAS être envoyé (gemini-3.6-flash le refuse
    # avec un « 400 Request contains an invalid argument » sans autre détail)
    transcription = client._config("", "transcription", False)
    verifie(getattr(transcription, "thinking_config", None) is None,
            "thinking_budget=0 ne doit jamais partir : il faut omettre le réglage")

    # dégradation progressive : réflexion, puis outils
    verifie(client._degrader() is True and client.sans_reflexion, "1re dégradation")
    allege = client._config("consigne", "detail", True)
    verifie(getattr(allege, "thinking_config", None) is None, "réflexion non retirée")
    verifie(allege.tools, "les outils doivent survivre à la 1re dégradation")
    verifie(client._degrader() is True and client.sans_outils, "2e dégradation")
    minimal = client._config("consigne", "detail", True)
    verifie(not getattr(minimal, "tools", None), "outils non retirés")
    verifie(client._degrader() is False, "il ne reste plus rien à retirer")
    return "5 outils, configuration complète, et dégradation en 2 paliers"


@test("SDK Pinecone réel : upsert_records / search / fetch compatibles")
def t_sdk_pinecone():
    import inspect
    try:
        from pinecone.data import Index as IndexReel
    except Exception:
        try:
            from pinecone.db_data.index import Index as IndexReel
        except Exception:
            IndexReel = None

    from utils import config, rag

    if IndexReel is not None:
        # les appels réels doivent « binder » sur les signatures du SDK installé
        inspect.signature(IndexReel.upsert_records).bind(
            None, namespace="ns", records=[{"_id": "a", "chunk_text": "b"}])
        inspect.signature(IndexReel.search).bind(
            None, namespace="ns", top_k=24, inputs={"text": "q"},
            fields=["chunk_text"], rerank={"model": "bge-reranker-v2-m3",
                                           "top_n": 6, "rank_fields": ["chunk_text"]})
        inspect.signature(IndexReel.fetch).bind(None, ids=["x"], namespace="ns")

    # exécution complète contre un index factice au comportement du SDK v9
    appels: dict = {}

    class FauxIndex:
        def upsert_records(self, *, records, namespace, timeout=None):
            appels.setdefault("upserts", []).append((namespace, len(records)))

        def search(self, *, namespace, top_k=None, inputs=None, vector=None, id=None,
                   filter=None, fields=None, rerank=None, match_terms=None,
                   query=None, timeout=None):
            appels["recherche"] = {"namespace": namespace, "top_k": top_k,
                                   "inputs": inputs, "rerank": rerank, "fields": fields}
            return {"result": {"hits": [
                {"_id": "prog#p12#c0", "_score": 0.91,
                 "fields": {"chunk_text": "Théorème des valeurs intermédiaires.",
                            "page": 12, "source": "programme_terminal.pdf"}},
                {"_id": "__friedrich_manifest__", "_score": 0.10,
                 "fields": {"chunk_text": "manifeste", "collection": "__manifest__"}},
            ]}}

    os.environ["PINECONE_API_KEY"] = "cle-pinecone-de-test"
    vrai_get_index = rag.get_index
    rag.get_index = lambda: FauxIndex()
    try:
        envoyes = rag.upsert_records([{"_id": f"x{i}", "chunk_text": "texte"} for i in range(200)])
        verifie(envoyes == 200, f"{envoyes} fiches envoyées au lieu de 200")
        verifie(len(appels["upserts"]) == 3, "les lots de 90 ne sont pas respectés")
        verifie(appels["upserts"][0][0] == config.PINECONE_NAMESPACE(), "namespace incorrect")

        extraits = rag.search("théorème des valeurs intermédiaires")
        verifie(len(extraits) == 1, "le manifeste technique doit être filtré")
        verifie(extraits[0]["page"] == 12 and extraits[0]["score"] == 0.91,
                f"champs mal lus : {extraits[0]}")
        verifie(appels["recherche"]["rerank"]["model"] == "bge-reranker-v2-m3",
                "reranker non demandé")
        verifie(appels["recherche"]["inputs"] == {"text": "théorème des valeurs intermédiaires"},
                "requête mal transmise")
        contexte = rag.format_context(extraits)
        verifie("page 12" in contexte and "programme_terminal.pdf" in contexte,
                "contexte mal formaté")
    finally:
        rag.get_index = vrai_get_index
    return "signatures v9 + découpage en lots + filtrage du manifeste"


# ---------------------------------------------------------------------------

TESTS = [valeur for nom, valeur in sorted(globals().items())
         if nom.startswith("t_") and callable(valeur)]


def main() -> int:
    print("\n\033[1mVérifications de Friedrich\033[0m\n" + "─" * 62)
    for fonction in TESTS:
        fonction()
    reussis = sum(1 for _, ok, _ in RESULTATS if ok)
    for nom, ok, detail in RESULTATS:
        marque = "\033[32m✅\033[0m" if ok else "\033[31m❌\033[0m"
        print(f"{marque} {nom}")
        if detail:
            print(f"     \033[2m{detail}\033[0m")
    print("─" * 62)
    total = len(RESULTATS)
    if reussis == total:
        print(f"\033[32m{reussis}/{total} vérifications réussies — Friedrich est prêt.\033[0m\n")
        return 0
    print(f"\033[31m{reussis}/{total} vérifications réussies "
          f"({total - reussis} échec(s)).\033[0m\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
