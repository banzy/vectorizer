# Vector Reconstruction

A reusable AI skill for rebuilding existing logos as clean, editable SVGs. It
guides the model to infer the intended design construction: shared stroke widths,
circular arcs, rounded ends, alignment, custom curves and meaningful corners.

Use it with an original logo image, an existing SVG, or a `logo-designer-brief`
point export. The goal is to preserve the existing identity while recovering
useful geometry and removing unnecessary tracing artifacts.

## Install in Codex

With Node.js installed, run this single command:

```sh
npx -y skills add banzy/vectorizer --full-depth -a codex -g -y
```

This uses the [Skills CLI](https://github.com/vercel-labs/skills) to install the
skill for Codex across your projects. Start a new Codex chat after installation.
The command downloads the version published on GitHub.

Attach the source image and any available point export, then invoke the skill:

> Use $vector-reconstruction to rebuild this logo from the attached image and point
> export. Preserve its appearance, infer meaningful geometry and return an editable
> SVG with a comparison to the original.

A point export is optional. The skill also supports reconstruction directly from
an image or refinement of an existing SVG.

<details>
<summary>Manual installation without Node.js</summary>

Download this repository using GitHub's **Code → Download ZIP**, extract it,
and copy the `vector-reconstruction` folder into `~/.codex/skills/`. If you use a
custom `CODEX_HOME`, copy it into that directory's `skills` folder instead.
Create the destination folder if needed, then start a new Codex chat.

</details>

## Use with another AI assistant

Provide these files along with the original image:

- [`vector-reconstruction/SKILL.md`](vector-reconstruction/SKILL.md): the reconstruction instructions.
- Your point export or existing SVG, if available.
- [`vector-reconstruction/references/export-format.md`](vector-reconstruction/references/export-format.md)
  when supplying a `logo-designer-brief` JSON export.

Then ask:

> Follow the attached Vector Reconstruction skill. Examine the original logo and
> any supplied geometry, infer its intended construction, and return a standalone,
> editable SVG. Explain the construction choices and identify any checks you
> could not perform.

An assistant without a skill loader can use the file as task instructions. This
does not install a native skill in that system. Attach the image separately unless
the assistant can extract and view the embedded PNG; reading base64 text alone
does not provide visual inspection.

## What the skill does

1. Inspects the source and separates visual components, colors and negative space.
2. Infers supported constraints such as repeated widths, shared centers, symmetry,
   alignments and tangent transitions.
3. Rebuilds suitable parts with native shapes, centerline strokes or circular arcs,
   and uses fitted curves for custom outlines and lettering.
4. Preserves distinctive details rather than enforcing one geometric model on
   every region.
5. Renders and compares the SVG when tools are available, then refines regions
   that lose fidelity.

The intended output is an SVG file, a preview when possible, and a brief account
of the inferred design constraints, actual validation and remaining uncertainty.

## Optional helpers

Run these commands from the repository root. Helpers do not call an AI service.

### Extract an image and compact point evidence

Requires Python 3's standard library:

```sh
python3 vector-reconstruction/scripts/prepare_evidence.py logo-designer-brief.json --out evidence
```

This produces `evidence/evidence.json` and, when the export contains an embedded
PNG, `evidence/source.png`. It removes redundant raw coordinates and base64 from
the compact evidence while retaining the original export for local measurements.

### Compare a reconstruction

Requires Python 3 and Pillow. Render the SVG separately to a PNG at the original
image dimensions; the helper does not render SVG or resize the inputs.

```sh
python3 -m pip install Pillow
python3 vector-reconstruction/scripts/compare_rasters.py original.png rendered.png --out comparison
```

The output includes `comparison/comparison.png` and `comparison/metrics.json`.
For transparent images, it measures foreground overlap using alpha masks. It also
reports color differences over white and black backgrounds.

For an image with a known opaque background:

```sh
python3 vector-reconstruction/scripts/compare_rasters.py original.png rendered.png --background '#ffffff' --out comparison
```

To examine a small detail independently:

```sh
python3 vector-reconstruction/scripts/compare_rasters.py original.png rendered.png --crop 20 30 80 60 --out detail-comparison
```

Use `--help` for mask thresholds and other arguments. Whole-image overlap can hide
damage to a small letter, counter or hole; inspect those features visually and
with local crops. In a chat without file execution or rendering, numerical and
raster validation remain unverified.

## Repository contents

```text
README.md
vector-reconstruction/
├── SKILL.md
├── agents/openai.yaml
├── references/export-format.md
└── scripts/
    ├── prepare_evidence.py
    └── compare_rasters.py
```

## Sharing

Share this repository or the complete `vector-reconstruction` folder. Preserve the
folder structure so its references and helpers remain accessible. Source logos,
point exports and generated reconstructions are separate inputs and outputs;
they are not bundled with the skill.

The skill contains no API keys, hosted service or project-specific runtime paths.
