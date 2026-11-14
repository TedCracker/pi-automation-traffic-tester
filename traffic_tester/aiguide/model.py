"""A tiny document model + two renderers (plain text and semantic HTML) so the page and the .txt are always the same content."""
from __future__ import annotations

import html
from dataclasses import dataclass, field
from typing import List, Tuple, Union

from .. import __version__

# block kinds: ("p", text) ("ul", [text]) ("ol", [text]) ("kv", [(k, v)]) ("table", header, rows) ("h3", text)
Block = Tuple


@dataclass
class Section:
    id: str
    title: str
    blocks: List[Block] = field(default_factory=list)

    def p(self, text: str) -> "Section":
        self.blocks.append(("p", text)); return self

    def h3(self, text: str) -> "Section":
        self.blocks.append(("h3", text)); return self

    def ul(self, items) -> "Section":
        self.blocks.append(("ul", list(items))); return self

    def ol(self, items) -> "Section":
        self.blocks.append(("ol", list(items))); return self

    def kv(self, pairs) -> "Section":
        self.blocks.append(("kv", list(pairs))); return self

    def table(self, header, rows) -> "Section":
        self.blocks.append(("table", list(header), [list(r) for r in rows])); return self


@dataclass
class Doc:
    title: str
    intro: List[str]
    sections: List[Section] = field(default_factory=list)

    def add(self, sec: Section) -> Section:
        self.sections.append(sec); return sec


def to_text(doc: Doc) -> str:
    out = [doc.title, "=" * len(doc.title), ""]
    out += [*doc.intro, ""]
    for s in doc.sections:
        out += [s.title.upper(), "-" * len(s.title), ""]
        for b in s.blocks:
            k = b[0]
            if k == "p":
                out += [b[1], ""]
            elif k == "h3":
                out += [b[1], ""]
            elif k == "ul":
                out += [f"- {i}" for i in b[1]] + [""]
            elif k == "ol":
                out += [f"{n}. {i}" for n, i in enumerate(b[1], 1)] + [""]
            elif k == "kv":
                out += [f"{a}: {v}" for a, v in b[1]] + [""]
            elif k == "table":
                out += [" | ".join(b[1])] + [" | ".join(str(c) for c in r) for r in b[2]] + [""]
    return "\n".join(out).rstrip() + "\n"


def _e(t) -> str:
    return html.escape(str(t), quote=True)


CSS = """
:root{--bg:#f4f6f8;--panel:#fff;--text:#16202a;--muted:#5c6b7a;--line:#e2e8ee;--accent:#0f766e}
@media (prefers-color-scheme:dark){:root{--bg:#0d1319;--panel:#151d26;--text:#e7edf3;--muted:#93a3b3;--line:#263341;--accent:#2dd4bf}}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.55 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:980px;margin:0 auto;padding:20px 16px 60px}
h1{font-size:1.6rem;margin:.2em 0}h2{font-size:1.25rem;margin:1.6em 0 .4em;padding-top:.6em;border-top:1px solid var(--line)}h3{font-size:1.02rem;margin:1.1em 0 .2em}
p,li{max-width:80ch}.muted{color:var(--muted)}a{color:var(--accent)}code{background:var(--panel);border:1px solid var(--line);border-radius:4px;padding:0 4px;font-size:.88em}
table{border-collapse:collapse;width:100%;font-size:.92rem;background:var(--panel)}th,td{border:1px solid var(--line);padding:5px 8px;text-align:left;vertical-align:top}th{color:var(--muted)}
dl{display:grid;grid-template-columns:max-content 1fr;gap:2px 14px}dt{color:var(--muted)}dd{margin:0}
.brandbar{margin:0 0 6px}.bname{display:inline-flex;gap:9px;align-items:center;text-decoration:none;color:inherit}.bname span{display:flex;flex-direction:column;line-height:1.15}.appver{font-size:.7rem;color:var(--muted)}
nav.toc{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:8px 14px}nav.toc a{display:inline-block;margin:2px 14px 2px 0}
"""


def to_html(doc: Doc, text_href: str = "/ai-guide.txt", app_href: str = "/") -> str:
    h = ['<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">',
         f"<title>{_e(doc.title)}</title><style>{CSS}</style></head><body><main>",
         f'<div class="brandbar"><a href="{_e(app_href)}" class="bname"><svg viewBox="0 0 32 32" width="26" height="26" aria-hidden="true"><rect width="32" height="32" rx="8" fill="#2563eb"/>'
         f'<path d="M6 17h5l3-8 4 14 3-6h5" stroke="#fff" stroke-width="2.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>'
         f'<span><b>Pi Automation Traffic Tester</b><small class="appver" data-testid="app-version">v{_e(__version__)}</small></span></a></div>',
         f"<h1>{_e(doc.title)}</h1>"]
    h += [f"<p>{_e(t)}</p>" for t in doc.intro]
    h.append(f'<p><a href="{_e(app_href)}">Open the app</a> · <a href="{_e(text_href)}">Same guide as plain text</a></p>')
    h.append('<nav class="toc" aria-label="Contents">' + " ".join(f'<a href="#{_e(s.id)}">{_e(s.title)}</a>' for s in doc.sections) + "</nav>")
    for s in doc.sections:
        h.append(f'<section id="{_e(s.id)}"><h2>{_e(s.title)}</h2>')
        for b in s.blocks:
            k = b[0]
            if k == "p":
                h.append(f"<p>{_e(b[1])}</p>")
            elif k == "h3":
                h.append(f"<h3>{_e(b[1])}</h3>")
            elif k in ("ul", "ol"):
                h.append(f"<{k}>" + "".join(f"<li>{_e(i)}</li>" for i in b[1]) + f"</{k}>")
            elif k == "kv":
                h.append("<dl>" + "".join(f"<dt>{_e(a)}</dt><dd>{_e(v)}</dd>" for a, v in b[1]) + "</dl>")
            elif k == "table":
                h.append("<table><thead><tr>" + "".join(f"<th>{_e(c)}</th>" for c in b[1]) + "</tr></thead><tbody>"
                         + "".join("<tr>" + "".join(f"<td>{_e(c)}</td>" for c in r) + "</tr>" for r in b[2]) + "</tbody></table>")
        h.append("</section>")
    h.append("</main></body></html>")
    return "".join(h)
