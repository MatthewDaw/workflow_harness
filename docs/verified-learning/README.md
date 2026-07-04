# Verified Learning — explainer "video" (HTML)

A self-contained, narrated, auto-playing HTML explainer of the Verified Learning
algorithm. Open `verified-learning.html` in any modern browser and click **Play** —
it speaks each slide (Web Speech API), shows captions, animates the diagram, and
advances on its own. Controls: play/pause (Space), ← →, restart (R), mute (M).

## It's compiled — edit small pieces, don't touch the whole

The playable file is **generated**. Source lives in `src/`:

```
src/template.html      page shell (3 placeholders: styles, slides, player)
src/styles.css         global theme + animation utilities + player UI
src/player.js          the "video" engine (TTS narration + auto-advance + controls)
src/slides/NN-*.html   ONE <section class="slide"> per file, played in filename order
build.py               concatenates everything -> verified-learning.html
```

Each slide is independent: it carries its own diagram and, optionally, its own
scoped `<style>` (selectors prefixed with `#slide-NN`). The narration for a slide
is the `data-narration` attribute on its `<section>`; the on-screen caption is
generated from it automatically.

### To change something

- **Reword narration / a slide's visual** → edit just `src/slides/NN-*.html`.
- **Colors, fonts, transitions, controls** → edit `src/styles.css`.
- **Playback behavior** (timing, voice, keys) → edit `src/player.js`.
- **Add/remove/reorder slides** → add or rename files in `src/slides/`
  (they sort by filename, e.g. `09b-...` slots between 09 and 10).

Then rebuild:

```bash
python build.py
```

No dependencies — standard-library Python only.

## Animation helpers (usable in any slide)

Add a class and an optional `--d` delay; they run when the slide becomes active:

- `reveal` — fade + rise · `pop` — scale-in · `fade` — fade-in
- `draw` — draws an SVG stroke (give the path `pathLength="1"`)
- `pulse` — soft glowing loop

Example: `<g class="pop" style="--d:.6s"> … </g>`
