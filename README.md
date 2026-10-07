# Vector Reconstruction

An AI skill that rebuilds an existing logo as a clean, editable SVG, the way a senior
designer would: it treats auto-tracing as a rough draft, removes debris and duplicate
shapes, recovers the real geometry (circles, arcs, true curves, redrawn letters), snaps
colors to a clean palette and checks the result at real sizes.

**All it needs is the logo image.** It creates its own contour evidence from the image.
You can also give it an existing SVG to clean up.

## Install

**Codex or Claude Code** (needs Node.js; run it once, then start a new chat or session):

```sh
npx -y skills add banzy/vectorizer --full-depth -a codex -g -y
npx -y skills add banzy/vectorizer --full-depth -a claude-code -g -y
```

<details>
<summary>Manual install</summary>

Download this repo (**Code → Download ZIP**) and copy the `vector-reconstruction` folder to:

- Claude Code: `~/.claude/skills/` (all projects) or `.claude/skills/` (one project)
- Codex: `~/.codex/skills/`
- Claude.ai / Claude desktop: zip the folder so `vector-reconstruction/` is at the top
  level, then upload it in **Settings → Capabilities → Skills**.

</details>

## Use it

Attach the logo and ask:

> Use the vector-reconstruction skill to vectorize this logo.

That is all it needs. You get a clean, editable SVG that works for any use, a preview
and a short account of what was rebuilt and checked. It asks no questions unless the
image is unusable.

Optional, only if it matters to you: name the use (web, print, cutting, embroidery,
animation) so it can tune the simplification, or name anything that must stay exactly
as is, such as a color value or a letter shape.

## Use it on any other platform

Any AI chat that accepts image uploads can follow the skill, even without a skill loader.

1. Upload the logo image.
2. Upload or paste [`vector-reconstruction/SKILL.md`](vector-reconstruction/SKILL.md) as
   the instructions (add [`production-targets.md`](vector-reconstruction/references/production-targets.md)
   if you can attach a second file).
3. Send:

> Follow the attached Vector Reconstruction instructions and vectorize the uploaded logo.
> Return a standalone SVG. Then say which checks you actually ran and which you could
> not run.

What to expect:

- **With code execution** (e.g. ChatGPT with data analysis, Claude with code execution):
  also upload the `scripts` folder. The assistant can then trace the image, render and
  compare the result and audit the SVG, as described below.
- **Without code execution**: the assistant works from looking at the image. It can
  still follow the design method, but it cannot measure overlap or render checks, so
  open the SVG yourself at several sizes (including 16–32 px) and compare it with the
  original.
- Chat assistants can produce a plausible but imperfect result. Review the output, and
  ask for another pass on any part that drifted.

## What it does

1. Decides what must be preserved and the output target.
2. Traces the image into per-color contours and a core palette.
3. Removes debris: background rectangles, specks, fake white knockouts, stacked
   duplicates and hidden shapes.
4. Infers construction: repeated widths, radii, alignment, symmetry.
5. Rebuilds with true primitives, arcs and deliberate Béziers; redraws lettering.
6. Simplifies without changing the character, then organizes colors, holes and groups.
7. Validates against the source, in outline mode and at small sizes.

## Optional helper scripts

Python 3. `extract_contours.py`, `compare_rasters.py` and `render_svg.py --sizes` also
need Pillow (`python3 -m pip install Pillow`). `render_svg.py` needs one SVG renderer
(`rsvg-convert`, cairosvg, Inkscape or Chrome). `inspect_svg.py` needs nothing extra.
The skill runs these itself when it can:

```sh
python3 vector-reconstruction/scripts/extract_contours.py logo.png --out evidence
python3 vector-reconstruction/scripts/render_svg.py candidate.svg rendered.png --width W --height H
python3 vector-reconstruction/scripts/compare_rasters.py evidence/source.png rendered.png --out comparison
python3 vector-reconstruction/scripts/inspect_svg.py candidate.svg --out inspection --target web
python3 vector-reconstruction/scripts/render_svg.py candidate.svg sizes.png --sizes 16 24 32 48 64
```

They trace the image, render the SVG, measure overlap, audit it (duplicates, hidden
shapes, bad joins, open paths, live strokes; wireframe included) and make a small-size
legibility sheet. Use `--help` on any script. `prepare_evidence.py` is only needed if
you already have a `logo-designer-brief` JSON export from the web app.

## Repository

```text
vector-reconstruction/
├── SKILL.md                      instructions
├── agents/openai.yaml            Codex metadata
├── references/                   export format, production targets
└── scripts/                      the helpers above
```

No API keys, hosted service or logos are bundled. Source images and generated SVGs are
your inputs and outputs.
