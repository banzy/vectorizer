# Production targets

Read this when deciding how far to simplify, whether strokes may stay live, and what to
check before delivery. Pass the matching `--target` to `scripts/inspect_svg.py`; it raises
the severity of the checks that matter for that output.

| Target | Typical use | Geometry rules | Color rules | Validate at |
| --- | --- | --- | --- | --- |
| `general` (default) | Brand master for unknown future uses | Closed filled outlines; live strokes only where they are part of the construction and you also deliver an outlined copy; true primitives where they fit | Flat brand colors; holes are real holes | Source size, 64, 32 and 16 px |
| `web` | Site header, app UI, favicon, social avatar | Live strokes acceptable; small file; 2 decimals; `viewBox` without fixed size is fine | Flat colors, `currentColor` acceptable for one-color icons | 16, 24, 32, 48 px and the actual header height |
| `print` | Stationery, packaging, signage | Closed outlines, no hairlines below ~0.25 pt at the smallest print size; no clipping tricks the printer must interpret | Note that SVG is RGB: list intended Pantone/CMYK values separately when known | Smallest printed size (e.g. 15 mm wide) |
| `cut` | Vinyl, laser, CNC, stencils | Every shape closed; no strokes (outline them); no overlapping or duplicate fills (union them); no clipping, masks or gradients; no islands smaller than the cutter can weed; stencil bridges for floating counters if requested | One color per layer/pass | 1:1 at physical size; inspect the wireframe |
| `embroidery` | Apparel, patches | Simplify more: minimum feature ~1 mm and minimum gap ~1 mm at stitched size; no hairlines or tiny text; closed fills only | Few thread colors (usually 1–6); no gradients | Stitched size (often 60–100 mm wide) |
| `animation` | Motion logo, Lottie, CSS/SVG animation | Separate meaningful parts with stable ids; keep line-like parts as live centerline strokes when a draw-on effect may be wanted; consistent path direction and start points | Colors as reusable classes | Each part isolated plus the composed mark |

## Simplification by target

Simplify less for a brand master and print; simplify more for embroidery, very small web
icons and cutting. The aim is always the fewest anchors that keep the design's character,
never the lowest possible count. Reduce detail deliberately (e.g. a separate small-size
variant) rather than letting the master lose it.

## Variants a senior designer delivers when asked for a production set

- **Full color**: the master.
- **One color**: every visible region in one ink; works only if knockouts are real holes.
- **Reversed**: light artwork for dark backgrounds; often needs knockouts and internal
  contrast reconsidered, not just a color swap.
- **Small-size / favicon**: simplified drawing that survives 16–32 px (drop taglines,
  thicken hairlines, open tight counters); deliver only when small-size tests fail.

Never produce a variant by changing the master's geometry silently. Name the files and
state what changed.
