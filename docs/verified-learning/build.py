#!/usr/bin/env python3
"""Compile the Verified Learning explainer into a single playable HTML file.

Source lives in ``src/`` as small, independently-editable pieces:
  - src/template.html  : the page shell with three placeholders
  - src/styles.css     : global theme, animation utilities, player UI
  - src/player.js      : the "video" engine (TTS narration + auto-advance)
  - src/slides/*.html  : one <section class="slide"> per file, sorted by name

Edit any single file, then re-run:

    python build.py

Output: verified-learning.html  (open it in a browser, click Play).
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
OUT = ROOT / "verified-learning.html"


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def main() -> int:
    template = read(SRC / "template.html")
    styles = read(SRC / "styles.css")
    player = read(SRC / "player.js")

    slide_files = sorted((SRC / "slides").glob("*.html"))
    slides_html = "\n\n".join(read(f) for f in slide_files)

    out = (
        template
        .replace("/*__STYLES__*/", styles)
        .replace("<!--__SLIDES__-->", slides_html)
        .replace("/*__PLAYER__*/", player)
    )
    OUT.write_text(out, encoding="utf-8")

    print(f"Built {OUT.name} from {len(slide_files)} slides:")
    for f in slide_files:
        print(f"  - {f.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
