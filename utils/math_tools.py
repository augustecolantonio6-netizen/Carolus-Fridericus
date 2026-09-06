"""Outils mathématiques réellement exécutés (SymPy + Matplotlib).

Ces fonctions sont exposées à Gemini sous forme de *function calls*. Elles sont
VRAIMENT exécutées : Friedrich n'a donc jamais à prétendre avoir vérifié un
calcul qu'il n'a pas vérifié.

Sécurité : aucune évaluation de code arbitraire. `parse_expr` est appelé avec un
espace de noms restreint (`_SAFE_NS`) et la taille des expressions est limitée.
"""

from __future__ import annotations

import os
import re

# Matplotlib doit pouvoir écrire son cache : sur Vercel, seul /tmp est ouvert.
os.environ.setdefault("MPLCONFIGDIR", os.path.join(os.sep + "tmp", "matplotlib"))

import sympy  # noqa: E402
from sympy.parsing.sympy_parser import (  # noqa: E402
    convert_xor,
    implicit_multiplication_application,
    parse_expr,
    standard_transformations,
)

MAX_EXPR_CHARS = 400
MAX_PLOT_FUNCTIONS = 4

_TRANSFORMATIONS = standard_transformations + (
    implicit_multiplication_application,
    convert_xor,
)

_ALLOWED_NAMES = [
    "Symbol", "Integer", "Float", "Rational", "Add", "Mul", "Pow", "Eq",
    "pi", "oo", "zoo", "nan",
    "sqrt", "cbrt", "exp", "log", "Abs", "sign", "factorial", "binomial",
    "sin", "cos", "tan", "asin", "acos", "atan", "atan2",
    "sinh", "cosh", "tanh", "asinh", "acosh", "atanh",
    "floor", "ceiling", "Max", "Min", "gcd", "lcm", "Mod",
    "re", "im", "conjugate", "arg", "Abs",
    "diff", "integrate", "limit", "simplify", "expand", "factor", "solve",
    "Sum", "Product", "Piecewise", "Matrix",
]


def _safe_namespace() -> dict:
    ns = {name: getattr(sympy, name) for name in _ALLOWED_NAMES if hasattr(sympy, name)}
    ns["ln"] = sympy.log
    # lettres usuelles en Terminale
    for letter in "abcdfghjklmnpqrstuvwxyz":
        ns.setdefault(letter, sympy.Symbol(letter))
    ns["e"] = sympy.E          # e^x = exponentielle
    ns["i"] = sympy.I          # nombre complexe i (maths expertes)
    ns["I"] = sympy.I
    ns["E"] = sympy.E
    ns["infini"] = sympy.oo
    return ns


_SAFE_NS = _safe_namespace()


class MathToolError(Exception):
    """Erreur « propre » renvoyée au modèle (jamais à l'élève telle quelle)."""


def _parse(expression: str):
    """Transforme une chaîne en expression SymPy, de façon sûre."""
    if not expression or not str(expression).strip():
        raise MathToolError("expression vide")
    text = str(expression).strip()
    if len(text) > MAX_EXPR_CHARS:
        raise MathToolError(f"expression trop longue (> {MAX_EXPR_CHARS} caractères)")
    # petites tolérances d'écriture côté élève / modèle
    text = text.replace("−", "-").replace("×", "*").replace("÷", "/")
    text = text.replace("²", "**2").replace("³", "**3")
    text = text.replace("\\", "").replace("$", "")
    # virgule décimale française : 3,14 -> 3.14 (sans casser Max(1, 2))
    text = re.sub(r"(?<=\d),(?=\d)", ".", text)
    try:
        return parse_expr(
            text,
            local_dict=dict(_SAFE_NS),
            global_dict={},
            transformations=_TRANSFORMATIONS,
            evaluate=True,
        )
    except MathToolError:
        raise
    except Exception as exc:  # syntaxe invalide
        raise MathToolError(f"expression illisible ({type(exc).__name__})") from exc


def _split_equation(text: str):
    """Sépare « membre gauche = membre droit » (renvoie une équation SymPy)."""
    raw = str(text or "").strip()
    if raw.count("=") == 1:
        left, right = raw.split("=")
        return sympy.Eq(_parse(left), _parse(right))
    return sympy.Eq(_parse(raw), 0)


def _pretty(expr) -> str:
    try:
        return sympy.sstr(expr)
    except Exception:
        return str(expr)


def _approx(expr) -> str | None:
    try:
        value = sympy.N(expr, 8)
        if value.is_number and value.is_finite:
            return str(value)
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------
# Outils
# --------------------------------------------------------------------------

def simplifier_expression(expression: str) -> dict:
    """Simplifie / développe / factorise une expression."""
    expr = _parse(expression)
    out = {"entree": _pretty(expr)}
    for label, func in (
        ("simplifiee", sympy.simplify),
        ("developpee", sympy.expand),
        ("factorisee", sympy.factor),
    ):
        try:
            out[label] = _pretty(func(expr))
        except Exception:
            out[label] = None
    valeur = _approx(expr)
    if valeur:
        out["valeur_approchee"] = valeur
    return out


def verifier_egalite(membre_gauche: str, membre_droit: str) -> dict:
    """Vérifie si deux expressions sont mathématiquement égales."""
    left = _parse(membre_gauche)
    right = _parse(membre_droit)
    difference = sympy.simplify(left - right)
    egales = bool(difference == 0)
    if not egales:
        try:  # second essai, plus agressif
            egales = bool(sympy.simplify(sympy.expand(left - right)) == 0)
        except Exception:
            pass
    return {
        "membre_gauche": _pretty(left),
        "membre_droit": _pretty(right),
        "egales": egales,
        "difference_simplifiee": _pretty(difference),
        "commentaire": (
            "Les deux écritures sont équivalentes."
            if egales
            else "Les deux écritures ne sont PAS équivalentes."
        ),
    }


def resoudre_equation(equation: str, variable: str = "x") -> dict:
    """Résout une équation (ou une inéquation simple) par rapport à `variable`."""
    var = sympy.Symbol(str(variable or "x").strip() or "x")
    raw = str(equation or "").strip()

    for op in ("<=", ">=", "<", ">"):
        if op in raw:
            left, right = raw.split(op, 1)
            relation = {"<=": sympy.Le, ">=": sympy.Ge, "<": sympy.Lt, ">": sympy.Gt}[op]
            solution = sympy.solve_univariate_inequality(
                relation(_parse(left), _parse(right)), var, relational=False
            )
            return {
                "type": "inequation",
                "entree": raw,
                "variable": str(var),
                "solution": _pretty(solution),
            }

    eq = _split_equation(raw)
    solutions = sympy.solve(eq, var, dict=False)
    if not isinstance(solutions, (list, tuple)):
        solutions = [solutions]
    detail = []
    for sol in solutions:
        item = {"exacte": _pretty(sol)}
        approx = _approx(sol)
        if approx:
            item["approchee"] = approx
        detail.append(item)
    return {
        "type": "equation",
        "entree": _pretty(eq),
        "variable": str(var),
        "nombre_de_solutions": len(detail),
        "solutions": detail,
    }


def deriver_ou_integrer(expression: str, operation: str = "derivee", variable: str = "x") -> dict:
    """Calcule une dérivée ou une primitive."""
    var = sympy.Symbol(str(variable or "x").strip() or "x")
    expr = _parse(expression)
    op = (operation or "derivee").lower()
    if op.startswith("prim") or op.startswith("integ"):
        result = sympy.integrate(expr, var)
        return {"operation": "primitive", "entree": _pretty(expr),
                "resultat": _pretty(result) + " + C"}
    result = sympy.diff(expr, var)
    return {"operation": "derivee", "entree": _pretty(expr),
            "resultat": _pretty(sympy.simplify(result))}


def tracer_courbe(
    fonctions: list[str],
    x_min: float = -10.0,
    x_max: float = 10.0,
    titre: str = "",
) -> tuple[dict, bytes | None]:
    """Trace une ou plusieurs courbes et renvoie une image PNG."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except Exception as exc:  # matplotlib non installé : dégradation propre
        return (
            {"erreur": "outil de tracé indisponible sur ce serveur "
                       f"({type(exc).__name__}) : décris la courbe avec des mots"},
            None,
        )

    if isinstance(fonctions, str):
        fonctions = [fonctions]
    fonctions = [f for f in (fonctions or []) if str(f).strip()][:MAX_PLOT_FUNCTIONS]
    if not fonctions:
        return {"erreur": "aucune fonction à tracer"}, None

    try:
        x_min = float(x_min)
        x_max = float(x_max)
    except (TypeError, ValueError):
        x_min, x_max = -10.0, 10.0
    if not (x_max > x_min) or (x_max - x_min) > 1e6:
        x_min, x_max = -10.0, 10.0

    var = sympy.Symbol("x")
    xs = np.linspace(x_min, x_max, 800)
    fig, ax = plt.subplots(figsize=(7.2, 4.6), dpi=140)
    tracees, erreurs = [], []

    for raw in fonctions:
        texte = str(raw)
        if "=" in texte:                      # « f(x) = ... » -> « ... »
            texte = texte.split("=", 1)[1]
        try:
            expr = _parse(texte)
            func = sympy.lambdify(var, expr, modules=["numpy"])
            with np.errstate(all="ignore"):
                ys = np.asarray(func(xs), dtype=float)
            ys = np.where(np.isfinite(ys), ys, np.nan)
            if np.all(np.isnan(ys)):
                raise ValueError("valeurs non définies sur cet intervalle")
            ax.plot(xs, ys, linewidth=2, label=f"y = {_pretty(expr)}")
            tracees.append(_pretty(expr))
        except Exception as exc:
            erreurs.append(f"{raw} : {type(exc).__name__}")

    if not tracees:
        plt.close(fig)
        return {"erreur": "aucune fonction traçable", "details": erreurs}, None

    finite = [v for v in ax.get_ylim()]
    if finite and abs(finite[1] - finite[0]) > 400:
        ax.set_ylim(-50, 50)
    ax.axhline(0, color="#444", linewidth=1)
    ax.axvline(0, color="#444", linewidth=1)
    ax.grid(True, linestyle=":", alpha=0.6)
    ax.set_xlabel("x")
    ax.set_ylabel("y")
    ax.set_title(titre or "Représentation graphique")
    ax.legend(loc="best", fontsize=9)
    fig.tight_layout()

    import io

    buffer = io.BytesIO()
    fig.savefig(buffer, format="png")
    plt.close(fig)
    png = buffer.getvalue()
    return (
        {
            "courbes_tracees": tracees,
            "intervalle": [x_min, x_max],
            "erreurs": erreurs or None,
            "image_envoyee": True,
            "note": "Le graphique est joint au message : tu peux y faire référence.",
        },
        png,
    )


# --------------------------------------------------------------------------
# Déclarations pour Gemini (function calling)
# --------------------------------------------------------------------------

TOOL_DECLARATIONS = [
    {
        "name": "verifier_egalite",
        "description": (
            "Vérifie avec SymPy si deux expressions mathématiques sont égales. "
            "À utiliser pour contrôler un calcul, une factorisation, une dérivée, "
            "ou l'étape d'un élève avant d'affirmer qu'elle est juste ou fausse."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "membre_gauche": {"type": "STRING", "description": "Ex : (x+1)**2"},
                "membre_droit": {"type": "STRING", "description": "Ex : x**2+2*x+1"},
            },
            "required": ["membre_gauche", "membre_droit"],
        },
    },
    {
        "name": "resoudre_equation",
        "description": (
            "Résout exactement une équation ou une inéquation avec SymPy et "
            "renvoie toutes les solutions."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "equation": {"type": "STRING", "description": "Ex : x**2-4=0 ou 2*x+1<=5"},
                "variable": {"type": "STRING", "description": "Inconnue, par défaut x"},
            },
            "required": ["equation"],
        },
    },
    {
        "name": "simplifier_expression",
        "description": "Simplifie, développe et factorise une expression avec SymPy.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "expression": {"type": "STRING", "description": "Ex : (x**2-1)/(x-1)"},
            },
            "required": ["expression"],
        },
    },
    {
        "name": "deriver_ou_integrer",
        "description": "Calcule une dérivée ou une primitive exacte avec SymPy.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "expression": {"type": "STRING", "description": "Ex : x*exp(-x)"},
                "operation": {
                    "type": "STRING",
                    "description": "'derivee' ou 'primitive'",
                },
                "variable": {"type": "STRING", "description": "Variable, par défaut x"},
            },
            "required": ["expression", "operation"],
        },
    },
    {
        "name": "tracer_courbe",
        "description": (
            "Trace la représentation graphique d'une à quatre fonctions et joint "
            "l'image PNG au message Discord. À n'utiliser que si un graphique aide "
            "vraiment l'élève."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "fonctions": {
                    "type": "ARRAY",
                    "items": {"type": "STRING"},
                    "description": "Ex : ['x**2-4', 'exp(-x)']",
                },
                "x_min": {"type": "NUMBER", "description": "Borne gauche, défaut -10"},
                "x_max": {"type": "NUMBER", "description": "Borne droite, défaut 10"},
                "titre": {"type": "STRING", "description": "Titre du graphique"},
            },
            "required": ["fonctions"],
        },
    },
]

_DISPATCH = {
    "verifier_egalite": verifier_egalite,
    "resoudre_equation": resoudre_equation,
    "simplifier_expression": simplifier_expression,
    "deriver_ou_integrer": deriver_ou_integrer,
}


def call_tool(name: str, arguments: dict) -> tuple[dict, bytes | None]:
    """Exécute réellement l'outil demandé par Gemini.

    Renvoie (résultat JSON-sérialisable, image PNG éventuelle).
    Aucune exception ne remonte : le modèle reçoit toujours une réponse.
    """
    arguments = dict(arguments or {})
    try:
        if name == "tracer_courbe":
            return tracer_courbe(
                fonctions=arguments.get("fonctions") or [],
                x_min=arguments.get("x_min", -10.0),
                x_max=arguments.get("x_max", 10.0),
                titre=arguments.get("titre", ""),
            )
        func = _DISPATCH.get(name)
        if func is None:
            return {"erreur": f"outil inconnu : {name}"}, None
        return func(**arguments), None
    except MathToolError as exc:
        return {"erreur": str(exc)}, None
    except TypeError as exc:
        return {"erreur": f"arguments invalides : {exc}"}, None
    except Exception as exc:
        return {"erreur": f"calcul impossible ({type(exc).__name__})"}, None
