"""Markdown with LaTeX left intact for KaTeX to render in the browser.

Markdown would otherwise eat the underscores and asterisks inside $...$, so math is
swapped out for placeholders before rendering and put back afterwards. Input is
HTML-escaped first: extracted text is data, never markup.
"""

import html
import re

import markdown as md
from markupsafe import Markup

_MATH = re.compile(r"\$\$.+?\$\$|\\\[.+?\\\]|\\\(.+?\\\)|\$[^$\n]+?\$", re.S)


def render(text: str | None) -> Markup:
    if not text:
        return Markup("")
    stash: list[str] = []

    def keep(m: re.Match) -> str:
        stash.append(m.group(0))
        return f"\x00{len(stash) - 1}\x00"

    protected = _MATH.sub(keep, html.escape(text, quote=False))
    out = md.markdown(protected, extensions=["tables", "sane_lists", "nl2br"])
    out = re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], out)
    return Markup(out)
