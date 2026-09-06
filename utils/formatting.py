"""Mise en forme des réponses pour Discord.

Discord limite chaque message à 2000 caractères : on découpe proprement,
sans casser les blocs de code ni les formules.
"""

from __future__ import annotations

import re

DISCORD_LIMIT = 1900

_LIGATURES = {
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl",
    " ": " ", " ": " ",
}


def clean_pdf_text(text: str) -> str:
    """Nettoie le texte extrait d'un PDF (espaces, ligatures, mots coupés)."""
    if not text:
        return ""
    for bad, good in _LIGATURES.items():
        text = text.replace(bad, good)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", text)
    # mots coupés en fin de ligne : "fonc-\ntion" -> "fonction"
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    lines = []
    for line in text.split("\n"):
        line = re.sub(r"[ \t]+", " ", line).strip()
        if not line:
            continue
        if re.fullmatch(r"[-–—•.\s\d]{0,6}", line):  # numéros de page isolés
            continue
        lines.append(line)
    out = "\n".join(lines)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


_EXPOSANTS = str.maketrans("0123456789+-=()n x", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿ ˣ")
_INDICES = str.maketrans("0123456789+-=()n", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₙ")

#: Commandes LaTeX les plus courantes en Terminale -> Unicode.
_LATEX_UNICODE = [
    (r"\\mathbb\{R\}", "ℝ"), (r"\\mathbb\{N\}", "ℕ"), (r"\\mathbb\{Z\}", "ℤ"),
    (r"\\mathbb\{Q\}", "ℚ"), (r"\\mathbb\{C\}", "ℂ"),
    (r"\\R\b", "ℝ"), (r"\\N\b", "ℕ"), (r"\\Z\b", "ℤ"), (r"\\C\b", "ℂ"),
    (r"\\times", "×"), (r"\\div", "÷"), (r"\\cdot", "·"), (r"\\pm", "±"),
    (r"\\leq?\b", "≤"), (r"\\geq?\b", "≥"), (r"\\neq?\b", "≠"),
    (r"\\approx", "≈"), (r"\\equiv", "≡"), (r"\\sim", "∼"),
    (r"\\infty", "∞"), (r"\\partial", "∂"), (r"\\emptyset|\\varnothing", "∅"),
    (r"\\in\b", "∈"), (r"\\notin\b", "∉"), (r"\\subset", "⊂"),
    (r"\\cup", "∪"), (r"\\cap", "∩"), (r"\\forall", "∀"), (r"\\exists", "∃"),
    (r"\\Rightarrow|\\implies", "⇒"), (r"\\Leftrightarrow|\\iff", "⇔"),
    (r"\\rightarrow|\\to\b", "→"), (r"\\mapsto", "↦"),
    (r"\\sum", "∑"), (r"\\prod", "∏"), (r"\\int", "∫"),
    (r"\\alpha", "α"), (r"\\beta", "β"), (r"\\gamma", "γ"), (r"\\delta", "δ"),
    (r"\\Delta", "∆"), (r"\\theta", "θ"), (r"\\lambda", "λ"), (r"\\mu", "μ"),
    (r"\\pi\b", "π"), (r"\\sigma", "σ"), (r"\\varphi|\\phi", "φ"),
    (r"\\ldots|\\dots|\\cdots", "…"),
    (r"\\left|\\right", ""), (r"\\!|\\,|\\;|\\:|\\quad|\\qquad", " "),
    (r"\\displaystyle|\\limits", ""), (r"\\text\{([^{}]*)\}", r"\1"),
    (r"\\mathrm\{([^{}]*)\}", r"\1"), (r"\\operatorname\{([^{}]*)\}", r"\1"),
    (r"\\lim", "lim"), (r"\\log", "log"), (r"\\ln", "ln"), (r"\\exp", "exp"),
    (r"\\sin", "sin"), (r"\\cos", "cos"), (r"\\tan", "tan"),
]


def latex_vers_unicode(text: str) -> str:
    """Traduit le LaTeX résiduel en Unicode : Discord ne rend pas le LaTeX.

    Le prompt demande déjà de l'Unicode ; ceci rattrape les rechutes du modèle
    pour que l'élève ne voie jamais de `$\\frac{a}{b}$` en clair.
    """
    if not text or ("\\" not in text and "$" not in text):
        return text

    # \frac{a}{b} -> (a)/(b), en traitant les imbrications simples
    for _ in range(3):
        nouveau = re.sub(r"\\[dt]?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}",
                         r"(\1)/(\2)", text)
        if nouveau == text:
            break
        text = nouveau
    text = re.sub(r"\\sqrt\s*\[3\]\s*\{([^{}]*)\}", r"∛(\1)", text)
    text = re.sub(r"\\sqrt\s*\{([^{}]*)\}", r"√(\1)", text)
    text = re.sub(r"\\vec\s*\{([^{}]*)\}", r"\1→", text)
    text = re.sub(r"\\overline\s*\{([^{}]*)\}", r"\1̄", text)

    for motif, remplacement in _LATEX_UNICODE:
        text = re.sub(motif, remplacement, text)

    # exposants et indices : x^{2} / x^2  et  u_{n+1} / u_n
    def _haut(m):
        contenu = m.group(1) or m.group(2)
        return contenu.translate(_EXPOSANTS) if contenu else m.group(0)

    def _bas(m):
        contenu = m.group(1) or m.group(2)
        return contenu.translate(_INDICES) if contenu else m.group(0)

    text = re.sub(r"\^\{([0-9n+\-=()\sx]*)\}|\^([0-9n])", _haut, text)
    text = re.sub(r"_\{([0-9n+\-=()\s]*)\}|_([0-9n])", _bas, text)

    # lim_{x → +∞} f(x)  ->  lim(x → +∞) f(x)  (idem pour ∑ et ∏)
    text = re.sub(r"(lim|∑|∏|max|min)\s*_\{([^{}]*)\}", r"\1(\2)", text)
    text = re.sub(r"_\{([^{}]+)\}", r"_\1", text)      # indices restants

    # (1)/(3) -> 1/3 quand les parenthèses n'apportent rien
    text = re.sub(r"\((\w+)\)\s*/\s*\((\w+)\)", r"\1/\2", text)

    # délimiteurs de formules, devenus inutiles
    text = text.replace("$$", "").replace("$", "")
    text = re.sub(r"\\[a-zA-Z]+", "", text)          # commandes restantes
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text


def polish(text: str) -> str:
    """Normalise la sortie du modèle avant envoi sur Discord."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # \( \) et \[ \] : simples délimiteurs, on les efface
    text = text.replace("\\(", " ").replace("\\)", " ")
    text = text.replace("\\[", "\n").replace("\\]", "\n")
    text = latex_vers_unicode(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # neutralise les mentions de masse (ceinture + bretelles avec allowed_mentions)
    text = text.replace("@everyone", "@​everyone").replace("@here", "@​here")
    return text.strip()


def _fence_state(chunk: str) -> str | None:
    """Retourne le langage du bloc ``` laissé ouvert, sinon None."""
    fences = re.findall(r"^```(\w*)", chunk, flags=re.MULTILINE)
    if len(fences) % 2 == 0:
        return None
    return fences[-1] or ""


def _hard_split(block: str, limit: int) -> list[str]:
    """Découpe brutale d'une ligne trop longue, en préférant les espaces."""
    pieces = []
    while len(block) > limit:
        cut = block.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        pieces.append(block[:cut].rstrip())
        block = block[cut:].lstrip()
    if block:
        pieces.append(block)
    return pieces


def split_message(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    """Découpe un texte en messages Discord valides (<= `limit` caractères).

    On coupe en priorité entre les paragraphes, puis entre les lignes ; les
    blocs de code coupés en deux sont refermés puis rouverts.
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    blocks: list[str] = []
    for para in text.split("\n\n"):
        if len(para) <= limit:
            blocks.append(para)
            continue
        current = ""
        for line in para.split("\n"):
            for piece in ([line] if len(line) <= limit else _hard_split(line, limit)):
                candidate = f"{current}\n{piece}" if current else piece
                if len(candidate) <= limit:
                    current = candidate
                else:
                    if current:
                        blocks.append(current)
                    current = piece
        if current:
            blocks.append(current)

    chunks: list[str] = []
    current = ""
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = block
    if current:
        chunks.append(current)

    # rééquilibrage des blocs de code coupés entre deux messages
    fixed: list[str] = []
    carry: str | None = None
    for chunk in chunks:
        if carry is not None:
            chunk = "```" + carry + "\n" + chunk
        open_lang = _fence_state(chunk)
        if open_lang is not None:
            chunk = chunk + "\n```"
        carry = open_lang
        fixed.append(chunk[:2000])
    return [c for c in fixed if c.strip()]


def truncate(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + " [...]"
