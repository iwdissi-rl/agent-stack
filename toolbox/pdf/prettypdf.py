"""prettypdf — Markdown (or HTML) → an extravagant, print-quality PDF. Runs on the toolbox env's python (WeasyPrint).

  prettypdf report.md                      → report.pdf next to it + PNG previews of the first pages
  prettypdf report.md -o /workspace/outbox/report.pdf --theme luxe --toc
  prettypdf page.html --raw                → your own full HTML/CSS (still gets fonts + previews)

Front matter (optional, YAML-ish `key: value` between --- lines) overrides flags:
  title, subtitle, author, date, kicker, theme (auto|editorial|luxe|tech|academic), cover_image, toc (true/false),
  openers (true = every ## starts a new page), page (A4|Letter)

Markdown extras (`::: name [title]` … `:::`):
  callout [tip|warn|danger|info] Title · pullquote · stats (a list of "**42%** label") · cards (list of "**Title** text")
  cols (two columns) · hero (full-width gradient band) · timeline (ordered list of "**2024** what happened")
  Images: ![caption](path) becomes a figure with caption. Footnotes [^1], tables, task lists, ```code (highlighted).
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
THEMES = HERE / "themes"
os.environ.setdefault("FONTCONFIG_FILE", str(HERE / "fonts.conf"))

from markdown_it import MarkdownIt  # noqa: E402
from mdit_py_plugins.container import container_plugin  # noqa: E402
from mdit_py_plugins.footnote import footnote_plugin  # noqa: E402
from mdit_py_plugins.tasklists import tasklists_plugin  # noqa: E402
from pygments import highlight  # noqa: E402
from pygments.formatters import HtmlFormatter  # noqa: E402
from pygments.lexers import TextLexer, get_lexer_by_name  # noqa: E402

BLOCKS = ["callout", "pullquote", "stats", "cards", "cols", "hero", "timeline"]
THEME_HINTS = {
    "tech": r"\b(api|code|software|benchmark|latency|gpu|model|python|server|deploy|kpi|metrics?|startup|product|saas|data|mod|minecraft|ai|llm)\b",
    "luxe": r"\b(luxury|premium|invest\w*|portfolio|wealth|brand|fashion|hotel|wine|gala|exclusive|pitch|jewel\w*|watch(es)?)\b",
    "academic": r"\b(abstract|hypothesis|study|literature|methodology|thesis|essay|citation|theorem|proof|lab report|research paper|bibliography|homework|assignment)\b",
    "editorial": r"\b(story|travel|guide|history|culture|profile|feature|magazine|recipe|journal|interview|review|itinerary)\b",
}


def front_matter(text: str) -> tuple[dict, str]:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip().lower()] = v.strip().strip('"\'')
    return meta, text[m.end():]


def pick_theme(text: str) -> str:
    low = text.lower()
    scores = {t: len(re.findall(rx, low)) for t, rx in THEME_HINTS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "editorial"


def render_container(name: str):
    def render(self, tokens, idx, options, env):
        tok = tokens[idx]
        if tok.nesting != 1:
            return "</div>\n"
        info = tok.info.strip()[len(name):].strip()
        if name == "callout":
            kind, _, title = info.partition(" ")
            if kind not in ("tip", "warn", "danger", "info"):
                kind, title = "", info
            label = title or {"tip": "Tip", "warn": "Heads up", "danger": "Warning", "info": "Note"}.get(kind, "Note")
            return f'<div class="callout {kind}"><div class="ttl">{html.escape(label)}</div>\n'
        if name == "hero" and info:
            return f'<div class="hero"><h3>{html.escape(info)}</h3>\n'
        return f'<div class="{name}">\n'
    return render


def highlight_code(code: str, lang: str, _attrs: str) -> str:
    try:
        lexer = get_lexer_by_name(lang) if lang else TextLexer()
    except Exception:
        lexer = TextLexer()
    return highlight(code, lexer, HtmlFormatter(nowrap=True))


def md_to_html(body: str) -> str:
    md = MarkdownIt("commonmark", {"html": True, "typographer": True, "highlight": highlight_code}).enable(["table", "strikethrough"])
    md.use(footnote_plugin).use(tasklists_plugin)
    for b in BLOCKS:
        md.use(container_plugin, name=b, render=render_container(b))
    out = md.render(body)
    # ![caption](src) alone in a paragraph -> figure with caption
    out = re.sub(r'<p><img src="([^"]+)" alt="([^"]*)"\s*/?></p>',
                 lambda m: f'<figure><img src="{m.group(1)}" alt="{m.group(2)}"/>' + (f"<figcaption>{m.group(2)}</figcaption>" if m.group(2) else "") + "</figure>", out)
    out = out.replace('class="task-list-item', 'class="task task-list-item')
    return out


def add_ids_and_toc(body_html: str, openers: bool) -> tuple[str, str]:
    items, n = [], 0

    def h(m):
        nonlocal n
        n += 1
        level, text = m.group(1), m.group(2)
        hid = f"s{n}"
        plain = re.sub(r"<[^>]+>", "", text)
        items.append((level, hid, plain))
        cls = ' class="opener"' if (openers and level == "2" and n > 1) else ""
        return f'<h{level} id="{hid}"{cls}>{text}</h{level}>'
    body_html = re.sub(r"<h([23])>(.*?)</h\1>", h, body_html, flags=re.S)
    toc = "".join(f'<li class="{"sub" if lv == "3" else ""}"><a href="#{hid}">{html.escape(t)}</a></li>' for lv, hid, t in items
                  if lv == "2" or len(items) < 14)
    return body_html, toc


def pygments_css(theme: str) -> str:
    style = {"tech": "dracula", "luxe": "monokai", "editorial": "gruvbox-dark", "academic": "friendly"}.get(theme, "monokai")
    try:
        return HtmlFormatter(style=style).get_style_defs("pre")
    except Exception:
        return HtmlFormatter().get_style_defs("pre")


def build(src: Path, args) -> str:
    raw = src.read_text()
    if args.raw or src.suffix.lower() in (".html", ".htm"):
        return raw
    meta, body = front_matter(raw)
    title = meta.get("title") or args.title
    if not title:
        m = re.search(r"^#\s+(.+)$", body, re.M)
        title = m.group(1).strip() if m else src.stem.replace("-", " ").replace("_", " ").title()
        if m:
            body = body[:m.start()] + body[m.end():]
    theme = (meta.get("theme") or args.theme or "auto").lower()
    if theme == "auto":
        theme = pick_theme(title + " " + body)
    if not (THEMES / f"{theme}.css").exists():
        theme = "editorial"
    args.theme_used = theme
    toc_on = str(meta.get("toc", "auto" if args.toc is None else args.toc)).lower()
    openers = str(meta.get("openers", args.openers)).lower() == "true"
    content, toc = add_ids_and_toc(md_to_html(body), openers)
    if toc_on == "auto":
        toc_on = "true" if content.count("<h2") >= 4 else "false"
    subtitle = meta.get("subtitle") or args.subtitle or ""
    author = meta.get("author") or args.author or ""
    date = meta.get("date") or args.date or dt.date.today().strftime("%B %Y")
    kicker = meta.get("kicker") or args.kicker or {"tech": "Report", "luxe": "Private edition", "academic": "Paper",
                                                    "editorial": "Feature"}[theme]
    cover_img = meta.get("cover_image") or args.cover_image or ""
    page = meta.get("page") or args.page
    css = (THEMES / "base.css").read_text() + "\n" + (THEMES / f"{theme}.css").read_text() + "\n" + pygments_css(theme)
    css += f"\n:root {{ --page-size: {page}; }}\n"
    meta_cells = "".join(f"<div><b>{k}</b>{html.escape(v)}</div>" for k, v in (("By", author), ("Date", date)) if v)
    cover = (f'<section class="cover"><div class="art"></div>'
             + (f'<img class="cover-img" src="{html.escape(cover_img)}"/>' if cover_img else "")
             + f'<div class="inner"><div class="rule"></div><div class="kicker">{html.escape(kicker)}</div>'
             f'<h1>{html.escape(title)}</h1>' + (f'<div class="subtitle">{html.escape(subtitle)}</div>' if subtitle else "")
             + f'<div class="meta">{meta_cells}</div></div></section>')
    toc_html = f'<nav class="toc"><h2>Contents</h2><ol>{toc}</ol></nav>' if toc_on == "true" and toc else ""
    dc = "dropcaps" if theme in ("editorial", "academic") else ""
    return (f'<!doctype html><html lang="en" class="{dc}"><head><meta charset="utf-8"><title>{html.escape(title)}</title>'
            f'<style>{css}</style></head><body><h1 class="doctitle-src">{html.escape(title)}</h1>'
            f'<span class="docauthor-src">{html.escape(author or kicker)}</span>{cover}{toc_html}<main>{content}'
            f'<div class="endmark">✦</div></main></body></html>')


def main() -> int:
    ap = argparse.ArgumentParser(prog="prettypdf", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src")
    ap.add_argument("-o", "--out")
    ap.add_argument("--theme", default=None, help="auto|editorial|luxe|tech|academic")
    ap.add_argument("--title"), ap.add_argument("--subtitle"), ap.add_argument("--author"), ap.add_argument("--date")
    ap.add_argument("--kicker"), ap.add_argument("--cover-image")
    ap.add_argument("--toc", default=None, choices=["true", "false", "auto"])
    ap.add_argument("--openers", default="false", choices=["true", "false"])
    ap.add_argument("--page", default="A4")
    ap.add_argument("--raw", action="store_true", help="input is a complete HTML document")
    ap.add_argument("--html", action="store_true", help="also write the intermediate .html")
    ap.add_argument("--no-preview", action="store_true")
    ap.add_argument("--preview-pages", type=int, default=4)
    args = ap.parse_args()
    args.theme_used = "custom"
    src = Path(args.src).resolve()
    out = Path(args.out).resolve() if args.out else src.with_suffix(".pdf")
    out.parent.mkdir(parents=True, exist_ok=True)
    doc = build(src, args)
    if args.html:
        out.with_suffix(".html").write_text(doc)
    from weasyprint import HTML
    HTML(string=doc, base_url=str(src.parent)).write_pdf(str(out))
    print(f"pdf: {out}  theme: {args.theme_used}  size: {out.stat().st_size // 1024} KB")
    if not args.no_preview:
        pdir = out.parent / f".{out.stem}-preview"
        pdir.mkdir(exist_ok=True)
        for old in pdir.glob("*.png"):
            old.unlink()
        subprocess.run(["pdftoppm", "-png", "-r", "60", "-f", "1", "-l", str(args.preview_pages), str(out), str(pdir / "page")],
                       check=False)
        pages = sorted(pdir.glob("*.png"))
        print("previews (LOOK at them with vision_analyze before delivering):")
        for p in pages:
            print(f"  {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
