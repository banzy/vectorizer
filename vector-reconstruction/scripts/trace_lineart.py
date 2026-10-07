#!/usr/bin/env python3
"""Vectorize detailed line art or illustrations (pencil, ink, engraving, blueprint, sketch) into an
SVG automatically: removes paper tone and texture, then traces tonal layers of one ink color as smooth
curves. Use it when a picture has too much detail to rebuild by hand. Requires Pillow."""
import argparse
import json
import math
import statistics
from pathlib import Path

from PIL import Image, ImageChops, ImageColor, ImageFilter, ImageOps

from extract_contours import number, signed_area, simplify, trace


def hex_color(rgb):
    return "#%02x%02x%02x" % tuple(round(v) for v in rgb)


def local_background(rgb, light_paper):
    """Paper tone per pixel: a large max (or min) filter on a reduced copy keeps paper and drops ink."""
    scale = max(1, min(rgb.size) // 120)
    small = rgb.resize((max(1, rgb.width // scale), max(1, rgb.height // scale)), Image.Resampling.BOX)
    size = 9
    window = ImageFilter.MaxFilter(size) if light_paper else ImageFilter.MinFilter(size)
    small = small.filter(window).filter(ImageFilter.GaussianBlur(size / 2))
    return small.resize(rgb.size, Image.Resampling.BICUBIC)


def coverage_field(image, ink_override):
    """Return (coverage 'L' image: 0 paper .. 255 full ink, ink rgb, paper rgb, transparent)."""
    alpha = image.getchannel("A")
    if alpha.getextrema()[0] < 250:
        solid = [p for p, a in zip(image.convert("RGB").getdata(), alpha.getdata()) if a >= 200]
        ink = tuple(statistics.median(c[k] for c in solid) for k in range(3)) if solid else (0, 0, 0)
        return alpha, tuple(ink_override or ink), None, True
    rgb = image.convert("RGB")
    gray = rgb.convert("L")
    light_paper = statistics.fmean(gray.resize((64, 64)).getdata()) >= 128
    paper_map = local_background(rgb, light_paper)
    histogram, total = gray.histogram(), rgb.width * rgb.height
    # Ink color: median of the darkest 0.2% of pixels furthest from paper in luminance.
    cutoff, seen = 0, 0
    for value in (range(256) if light_paper else range(255, -1, -1)):
        seen += histogram[value]
        if seen >= total * 0.002:
            cutoff = value
            break
    chosen = [p for p, g in zip(rgb.getdata(), gray.getdata()) if (g <= cutoff if light_paper else g >= cutoff)]
    ink = tuple(statistics.median(c[k] for c in chosen) for k in range(3)) if chosen else (0, 0, 0)
    ink = tuple(ink_override) if ink_override else ink
    paper = tuple(round(statistics.fmean(band.resize((32, 32)).getdata())) for band in paper_map.split())
    direction = [(paper[k] - ink[k]) if light_paper else (ink[k] - paper[k]) for k in range(3)]
    norm = sum(d * d for d in direction if d > 0) or 1
    bands = []
    for index, (band, paper_band) in enumerate(zip(rgb.split(), paper_map.split())):
        weight = max(0.0, direction[index]) / norm * 255
        difference = ImageChops.subtract(paper_band, band) if light_paper else ImageChops.subtract(band, paper_band)
        bands.append(difference.point(lambda v, w=weight: min(255, v * w)))
    field = ImageChops.add(ImageChops.add(bands[0], bands[1]), bands[2])
    return field, ink, paper, False


def smooth_path(points, corner_degrees):
    """Closed Catmull-Rom style cubic path through the points; sharp turns stay corners."""
    count = len(points)
    tangents = []
    for i in range(count):
        before, here, after = points[i - 1], points[i], points[(i + 1) % count]
        u = (here[0] - before[0], here[1] - before[1])
        v = (after[0] - here[0], after[1] - here[1])
        lu, lv = math.hypot(*u), math.hypot(*v)
        if lu < 1e-9 or lv < 1e-9:
            tangents.append(None)
            continue
        turn = math.degrees(math.acos(max(-1.0, min(1.0, (u[0] * v[0] + u[1] * v[1]) / (lu * lv)))))
        if turn > corner_degrees:
            tangents.append(None)
            continue
        t = (after[0] - before[0], after[1] - before[1])
        length = math.hypot(*t) or 1.0
        tangents.append((t[0] / length, t[1] / length))
    parts = [f"M{number(points[0][0])} {number(points[0][1])}"]
    for i in range(count):
        a, b = points[i], points[(i + 1) % count]
        reach = math.dist(a, b) / 3
        ta, tb = tangents[i], tangents[(i + 1) % count]
        if ta is None and tb is None:
            parts.append(f"L{number(b[0])} {number(b[1])}")
            continue
        c1 = a if ta is None else (a[0] + ta[0] * reach, a[1] + ta[1] * reach)
        c2 = b if tb is None else (b[0] - tb[0] * reach, b[1] - tb[1] * reach)
        parts.append(f"C{number(c1[0])} {number(c1[1])} {number(c2[0])} {number(c2[1])} "
                     f"{number(b[0])} {number(b[1])}")
    return "".join(parts) + "Z"


def tier_opacities(field, thresholds):
    """Opacity per nested tier so overlapping tiers reproduce each band's mean darkness."""
    histogram = field.histogram()
    edges = [round(t * 255) for t in thresholds] + [256]
    opacities, remaining = [], 1.0
    for index in range(len(thresholds)):
        lo, hi = edges[index], edges[index + 1]
        count = sum(histogram[lo:hi])
        mean = (sum(v * histogram[v] for v in range(lo, hi)) / count / 255) if count else 0
        mean = min(0.995, max(mean, 1 - remaining + 0.001))
        opacity = 1 - (1 - mean) / remaining
        opacities.append(min(1.0, max(0.02, opacity)))
        remaining *= 1 - opacities[-1]
    return opacities


def build(image_path, out_path, tiers, tolerance, min_area, ink_override, paper_fill, blur, corner, boost):
    image = ImageOps.exif_transpose(Image.open(image_path)).convert("RGBA")
    width, height = image.size
    field, ink, paper, transparent = coverage_field(image, ink_override)
    if boost != 1.0:
        field = field.point(lambda v: min(255, round(v * boost)))
    if blur > 0:
        field = field.filter(ImageFilter.GaussianBlur(blur))
    thresholds = {1: [0.35], 2: [0.2, 0.55], 3: [0.14, 0.38, 0.68], 4: [0.1, 0.27, 0.5, 0.75]}[tiers]
    opacities = tier_opacities(field, thresholds)
    layers, nodes, specks = [], 0, 0
    for index, (level, opacity) in enumerate(zip(thresholds, opacities), start=1):
        data = []
        for loop in trace(field, level * 255):
            if abs(signed_area(loop)) < min_area:
                specks += 1
                continue
            simple = simplify(loop, tolerance)
            if len(simple) < 3:
                continue
            nodes += len(simple)
            data.append(smooth_path(simple, corner))
        if data:
            layers.append(f'<path id="tier-{index}" opacity="{opacity:.3f}" d="{"".join(data)}"/>')
    if not layers:
        raise ValueError("No ink was found. Lower --min-area or raise --boost, or give --ink '#rrggbb'.")
    body = "".join(layers)
    paper_rect = f'<rect id="paper" width="{width}" height="{height}" fill="{hex_color(paper)}"/>' \
        if (paper_fill and paper) else ""
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
           f'viewBox="0 0 {width} {height}">{paper_rect}'
           f'<g id="ink" fill="{hex_color(ink)}" fill-rule="evenodd">{body}</g></svg>\n')
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(svg, encoding="utf-8")
    return {"svg": str(out_path), "size_px": [width, height], "ink": hex_color(ink),
            "paper": hex_color(paper) if paper else None, "tiers": len(layers),
            "tier_opacities": [round(o, 3) for o in opacities], "nodes": nodes,
            "dropped_specks": specks, "bytes": len(svg.encode("utf-8"))}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--out", required=True, type=Path, help="SVG file to write")
    parser.add_argument("--tiers", type=int, choices=(1, 2, 3, 4), default=4,
                        help="tonal layers: 1 = flat silhouette, 2-3 = lighter file, 4 = default, most faithful")
    parser.add_argument("--tolerance", type=float, default=0.45, help="simplification error in pixels")
    parser.add_argument("--min-area", type=float, default=5.0, help="drop specks smaller than this (px^2)")
    parser.add_argument("--blur", type=float, default=0.3, help="denoise radius before tracing (px)")
    parser.add_argument("--boost", type=float, default=1.25,
                        help="multiply ink strength before tiering; above 1 reveals fainter lines")
    parser.add_argument("--corner", type=float, default=55.0, help="turns sharper than this stay corners (deg)")
    parser.add_argument("--ink", help="force the ink color, e.g. '#2a5db0'; detected otherwise")
    parser.add_argument("--paper", action="store_true", help="add a paper-colored background rectangle")
    args = parser.parse_args()
    try:
        ink = list(ImageColor.getrgb(args.ink)[:3]) if args.ink else None
        print(json.dumps(build(args.image, args.out, args.tiers, args.tolerance, args.min_area, ink,
                               args.paper, args.blur, args.corner, args.boost), indent=2))
    except (OSError, ValueError) as error:
        parser.exit(1, f"Cannot trace line art: {error}\n")
