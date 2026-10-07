#!/usr/bin/env python3
"""Build a logo-designer-brief package and compact evidence directly from a logo image. Requires Pillow."""
import argparse
import base64
import io
import json
import math
import re
from pathlib import Path

from PIL import Image, ImageChops, ImageColor, ImageFilter, ImageOps

from prepare_evidence import prepare

# Marching-squares segments keyed by corner bits TL=8, TR=4, BR=2, BL=1. Each segment runs
# between two cell edges with the foreground on its right (y-down), so outer contours are
# clockwise on screen (positive signed area) and holes counter-clockwise (negative).
SEGMENTS = {1: [("L", "B")], 2: [("B", "R")], 4: [("R", "T")], 8: [("T", "L")],
            14: [("B", "L")], 13: [("R", "B")], 11: [("T", "R")], 7: [("L", "T")],
            3: [("L", "R")], 6: [("B", "T")], 12: [("R", "L")], 9: [("T", "B")]}
SADDLES = {5: ([("L", "T"), ("R", "B")], [("L", "B"), ("R", "T")]),   # (center inside, outside)
           10: ([("T", "R"), ("B", "L")], [("T", "L"), ("B", "R")])}
COLOR_RADIUS = 40      # RGB distance within which flat colors count as one palette entry
FLAT_TOLERANCE = 12    # max channel spread in a 3x3 window for a pixel to count as flat color


def hex_color(rgb):
    return "#%02x%02x%02x" % tuple(rgb)


def border_color(rgb):
    width, height = rgb.size
    counts = {}
    for strip in (rgb.crop((0, 0, width, 1)), rgb.crop((0, height - 1, width, height)),
                  rgb.crop((0, 0, 1, height)), rgb.crop((width - 1, 0, width, height))):
        for count, color in strip.getcolors(strip.width * strip.height):
            counts[color] = counts.get(color, 0) + count
    return max(counts, key=counts.get)


def flat_mask(image):
    """255 where every channel varies by at most FLAT_TOLERANCE within the 3x3 neighbourhood."""
    spread = None
    for band in image.split():
        band_spread = ImageChops.subtract(band.filter(ImageFilter.MaxFilter(3)),
                                          band.filter(ImageFilter.MinFilter(3)))
        spread = band_spread if spread is None else ImageChops.lighter(spread, band_spread)
    return spread.point(lambda value: 255 if value <= FLAT_TOLERANCE else 0)


def cluster(colors):
    """Greedy weighted clustering of (count, rgb) pairs, most frequent colors first."""
    clusters = []
    for count, color in sorted(colors, reverse=True):
        for entry in clusters:
            if math.dist([value / entry[3] for value in entry[:3]], color) <= COLOR_RADIUS:
                entry[0] += color[0] * count
                entry[1] += color[1] * count
                entry[2] += color[2] * count
                entry[3] += count
                break
        else:
            clusters.append([color[0] * count, color[1] * count, color[2] * count, count])
    return [(tuple(round(value / entry[3]) for value in entry[:3]), entry[3]) for entry in clusters]


def blend(p, a, b):
    """Express p as the mix weight * a + (1 - weight) * b; return (weight, distance from that mix)."""
    span = [a[k] - b[k] for k in range(3)]
    length = sum(value * value for value in span) or 1
    weight = min(1.0, max(0.0, sum((p[k] - b[k]) * span[k] for k in range(3)) / length))
    return weight, math.dist(p, [b[k] + weight * span[k] for k in range(3)])


def core_palette(image, rgb, alpha, transparent, background):
    """Intended colors: flat interiors plus solid cores of thin strokes, without anti-aliasing shades."""
    if transparent:
        candidates = alpha.point(lambda value: 255 if value >= 250 else 0)
    else:
        candidates = flat_mask(rgb)
        distance = ImageChops.difference(rgb, Image.new("RGB", rgb.size, background))
        r, g, b = distance.split()
        distance = ImageChops.lighter(ImageChops.lighter(r, g), b)
        strongest = distance.getextrema()[1]
        if strongest >= 16:  # thin strokes have no flat pixels, but their cores reach full ink
            cores = distance.point(lambda value: 255 if value >= strongest * 0.9 else 0)
            candidates = ImageChops.lighter(candidates, cores)
    sample = Image.new("RGBA", rgb.size, (0, 0, 0, 0))
    sample.paste(rgb.convert("RGBA"), mask=candidates)
    colors = [(count, color[:3]) for count, color in sample.getcolors(rgb.width * rgb.height) if color[3]]
    if len(colors) > 20000:  # noisy (e.g. JPEG) sources: merge the lowest bits before clustering
        merged = {}
        for count, color in colors:
            key = tuple((value >> 2) << 2 | 2 for value in color)
            merged[key] = merged.get(key, 0) + count
        colors = [(count, color) for color, count in merged.items()]
    total = sum(count for count, _ in colors) or 1
    clusters = [(color, count) for color, count in cluster(colors)
                if count >= max(12, total * 0.0005)]
    # A small cluster lying on the line between two larger colors is an edge blend, not a brand color.
    clusters = [(color, count) for color, count in clusters
                if count >= total * 0.02 or not any(
                    a_count > count and b_count > count and blend(color, a, b)[1] <= 12 and
                    0.05 < blend(color, a, b)[0] < 0.95
                    for a, a_count in clusters for b, b_count in clusters if a != b)]
    inks, background_entry = [], None
    for color, count in clusters:
        if not transparent and math.dist(color, background) <= COLOR_RADIUS and background_entry is None:
            background_entry = (color, count)
        else:
            inks.append((color, count))
    if not transparent and background_entry is None:
        background_entry = (tuple(background), 0)
    return inks, background_entry, total


def membership_fields(rgb, alpha, transparent, palette_colors):
    """One 'L' field per palette color: 255 inside it, fractional across anti-aliased edges."""
    width, height = rgb.size
    count = len(palette_colors)
    palette_image = Image.new("P", (1, 1))
    flat = [value for color in palette_colors for value in color]
    palette_image.putpalette(flat + flat[:3] * (256 - count))
    index = rgb.quantize(palette=palette_image, dither=Image.Dither.NONE).tobytes()
    index = index.translate(bytes(value if value < count else 0 for value in range(256)))
    fields = [bytearray(index.translate(bytes(255 if value == c else 0 for value in range(256))))
              for c in range(count)]
    pixels = rgb.tobytes()
    opaque = alpha.tobytes()
    unexplained = edges = 0
    pairs = [(a, b) for a in range(count) for b in range(a + 1, count)]
    if count >= 2:
        flat_bytes = flat_mask(rgb).tobytes()
        for match in re.finditer(b"\x00", flat_bytes):  # only blended (non-flat) pixels need refining
            i = match.start()
            if transparent and opaque[i] < 128:
                continue
            p = pixels[3 * i:3 * i + 3]
            # The pair of palette colors whose blend best explains this edge pixel.
            residual, weight, c1, c2 = min((blend(p, palette_colors[a], palette_colors[b])[::-1] + (a, b)
                                            for a, b in pairs), key=lambda entry: entry[0])
            edges += 1
            if residual > COLOR_RADIUS:
                unexplained += 1
            for c in range(count):
                fields[c][i] = 0
            fields[c1][i] = round(255 * weight)
            fields[c2][i] = round(255 * (1 - weight))
    images = [Image.frombytes("L", (width, height), bytes(field)) for field in fields]
    if transparent:
        images = [ImageChops.multiply(field, alpha) for field in images]
    return images, (unexplained / edges if edges else 0.0)


def trace(field, threshold):
    """Trace sub-pixel iso-contours of the field at the threshold; return closed point loops."""
    width, height = field.size
    data = field.tobytes()
    pad = bytes(width + 2)
    rows = [pad] + [b"\0" + data[y * width:(y + 1) * width] + b"\0" for y in range(height)] + [pad]
    table = bytes(1 if value >= threshold else 0 for value in range(256))
    bits = [row.translate(table) for row in rows]

    def edge_point(kind, x, y):
        # Position of the threshold crossing on one edge of cell (x, y), in padded coordinates.
        if kind == "T":
            a, b, px, py, dx, dy = rows[y][x], rows[y][x + 1], x, y, 1, 0
        elif kind == "B":
            a, b, px, py, dx, dy = rows[y + 1][x], rows[y + 1][x + 1], x, y + 1, 1, 0
        elif kind == "L":
            a, b, px, py, dx, dy = rows[y][x], rows[y + 1][x], x, y, 0, 1
        else:
            a, b, px, py, dx, dy = rows[y][x + 1], rows[y + 1][x + 1], x + 1, y, 0, 1
        fraction = (threshold - a) / (b - a) if b != a else 0.5
        return (dx * fraction + px, dy * fraction + py)

    def key(kind, x, y):
        return {"T": ("h", x, y), "B": ("h", x, y + 1), "L": ("v", x, y), "R": ("v", x + 1, y)}[kind]

    following, position = {}, {}
    for y in range(height + 1):
        top, bottom = bits[y], bits[y + 1]
        if not (top.count(1) or bottom.count(1)) or (top.count(1) == len(top) and bottom.count(1) == len(bottom)):
            continue
        for x in range(width + 1):
            case = (top[x] << 3) | (top[x + 1] << 2) | (bottom[x + 1] << 1) | bottom[x]
            if case in (0, 15):
                continue
            if case in SADDLES:
                center = (rows[y][x] + rows[y][x + 1] + rows[y + 1][x] + rows[y + 1][x + 1]) / 4
                pairs = SADDLES[case][0 if center >= threshold else 1]
            else:
                pairs = SEGMENTS[case]
            for start, end in pairs:
                a, b = key(start, x, y), key(end, x, y)
                following[a] = b
                position[a] = edge_point(start, x, y)
                position[b] = edge_point(end, x, y)

    loops, visited = [], set()
    for first in following:
        if first in visited:
            continue
        loop, current = [], first
        while current not in visited:
            visited.add(current)
            loop.append(position[current])
            current = following[current]
        if len(loop) >= 3:
            loops.append([(x - 0.5, y - 0.5) for x, y in loop])  # padded grid -> pixel-boundary coords
    return loops


def signed_area(points):
    return sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1])) / 2


def inside(point, polygon):
    x, y, hit = point[0], point[1], False
    for (x1, y1), (x2, y2) in zip(polygon, polygon[1:] + polygon[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            hit = not hit
    return hit


def simplify(points, tolerance):
    """Ramer-Douglas-Peucker for a closed loop; every kept point is within `tolerance` of the original."""
    if len(points) < 4:
        return points
    first = max(range(len(points)), key=lambda i: math.dist(points[0], points[i]))
    runs = [points[0:first + 1], points[first:] + [points[0]]]
    kept = []
    for run in runs:
        flags, stack = [False] * len(run), [(0, len(run) - 1)]
        flags[0] = flags[-1] = True
        while stack:
            lo, hi = stack.pop()
            (x1, y1), (x2, y2) = run[lo], run[hi]
            length = math.dist(run[lo], run[hi]) or 1e-12
            worst, index = 0.0, None
            for i in range(lo + 1, hi):
                distance = abs((x2 - x1) * (y1 - run[i][1]) - (x1 - run[i][0]) * (y2 - y1)) / length
                if distance > worst:
                    worst, index = distance, i
            if index is not None and worst > tolerance:
                flags[index] = True
                stack += [(lo, index), (index, hi)]
        kept += [p for p, flag in zip(run[:-1], flags[:-1]) if flag]
    return kept


def number(value):
    return f"{value:.3f}".rstrip("0").rstrip(".")


def path_data(points):
    return "M" + " L".join(f"{number(x)} {number(y)}" for x, y in points) + " Z"


def binned_palette(image):
    bands = image.split()
    binned = Image.merge("RGBA", [band.point(lambda v: (v >> 5) * 32 + 16) for band in bands[:3]] +
                         [bands[3].point(lambda v: 255 if v >= 128 else 0)])
    colors = [(count, color) for count, color in binned.getcolors(1 << 24) if color[3]]
    total = sum(count for count, _ in colors) or 1
    return [{"hex": hex_color(color[:3]), "pixels": count, "share": round(count / total, 4)}
            for count, color in sorted(colors, reverse=True)[:12]]


def contour_records(field, threshold, tolerance, min_area, first_id, color):
    """Trace one field and return its contour records (ids from first_id), SVG path data and speck count."""
    contours, specks = [], 0
    for points in trace(field, threshold):
        area = signed_area(points)
        if abs(area) >= min_area:
            contours.append({"points": points, "area": area})
        else:
            specks += 1
    contours.sort(key=lambda c: -abs(c["area"]))
    boxes = [(min(x for x, _ in c["points"]), min(y for _, y in c["points"]),
              max(x for x, _ in c["points"]), max(y for _, y in c["points"])) for c in contours]
    for index, contour in enumerate(contours):
        px, py = contour["points"][0]
        parents = [j for j in range(index - 1, -1, -1)
                   if boxes[j][0] <= px <= boxes[j][2] and boxes[j][1] <= py <= boxes[j][3]
                   and inside((px, py), contours[j]["points"])]
        contour["parent"] = parents[0] if parents else None  # nearest enclosing = smallest, listed last
    records, paths, kept = [], [], set()
    for index, contour in enumerate(contours):
        simple = simplify(contour["points"], tolerance)
        if len(simple) < 3:
            continue
        kept.add(index)
        paths.append(path_data(simple))
        rounded = [[round(x, 3), round(y, 3)] for x, y in simple]
        records.append({
            "id": first_id + index, "local": index, "color_hex": color,
            "role": "outer" if contour["area"] > 0 else "hole",
            "observed_area_px2": round(abs(contour["area"]), 2),
            "boundary_points_px": rounded,
            "boundary_simplification_max_error_px": tolerance,
            "current_geometry": {
                "closed": True, "anchors": rounded,
                "segments": [{"type": "line", "from": a, "to": b}
                             for a, b in zip(rounded, rounded[1:] + rounded[:1])],
                "svg_path": path_data(simple)}})
    for record in records:  # a parent dropped by simplification would leave a dangling reference
        parent = contours[record.pop("local")]["parent"]
        while parent is not None and parent not in kept:
            parent = contours[parent]["parent"]
        record["parent_id"] = None if parent is None else first_id + parent
    return records, " ".join(paths), specks, first_id + len(contours)


def build(image_path, output_dir, threshold, tolerance, min_area, background, mode):
    image = ImageOps.exif_transpose(Image.open(image_path)).convert("RGBA")
    width, height = image.size
    alpha = image.getchannel("A")
    transparent = background is None and alpha.getextrema()[0] < 250
    rgb = Image.new("RGB", image.size, "white")
    rgb.paste(image.convert("RGB"), mask=None if transparent else alpha)
    if not transparent:
        background = tuple(background) if background else border_color(rgb)
    inks, background_entry, sampled = core_palette(image, rgb, alpha, transparent, background)
    if not inks:
        raise ValueError("No flat foreground colors were found. If this is a pencil, ink, blueprint or other "
                         "detailed line drawing or illustration, run scripts/trace_lineart.py instead. "
                         "Otherwise use --background if the background color was misdetected.")
    palette_colors = [color for color, _ in inks] + ([] if transparent else [background_entry[0]])
    fields, unexplained = membership_fields(rgb, alpha, transparent, palette_colors)
    if mode == "auto":
        mode = "color" if len(inks) > 1 else "mono"
    if mode == "mono":
        silhouette = alpha if transparent else fields[-1].point(lambda value: 255 - value)
        layers = [(hex_color(inks[0][0]), silhouette)]
    else:
        layers = [(hex_color(color), fields[index]) for index, (color, _) in enumerate(inks)]
    records, svg_paths, specks, next_id = [], [], 0, 0
    for color, field in layers:
        layer_records, d, layer_specks, next_id = contour_records(
            field, threshold, tolerance, min_area, next_id, color)
        records += layer_records
        specks += layer_specks
        if d:
            svg_paths.append(f'<path fill="{color}" fill-rule="evenodd" d="{d}"/>')
    if not records:
        raise ValueError("No contours were found. Lower --threshold or --min-area, "
                         "or set --background if the background color was misdetected.")
    core = [{"hex": hex_color(color), "role": "ink", "sampled_pixels": count,
             "share": round(count / sampled, 4)} for color, count in inks]
    if background_entry:
        core.append({"hex": hex_color(background_entry[0]), "role": "background",
                     "sampled_pixels": background_entry[1], "share": round(background_entry[1] / sampled, 4)})
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    package = {
        "schema": "logo-designer-brief", "version": 1, "producer": "extract_contours.py",
        "image": {"width": width, "height": height, "transparent_background": transparent,
                  "png_data_url": "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()},
        "coordinates": {"origin": "top-left", "x": "right", "y": "down", "units": "source pixels",
                        "boundary_estimate": "Sub-pixel iso-contour of each color's membership field at "
                                             "the threshold, in pixel-boundary coordinates (a pixel "
                                             "spans [i, i+1]); no further offset is needed."},
        "core_palette": core,
        "source_palette": binned_palette(image),
        "trace_settings": {"method": "marching squares", "mode": mode,
                           "foreground_field": "alpha" if transparent else "color membership",
                           "threshold_0_to_255": threshold,
                           "background_rgb": None if transparent else list(background_entry[0]),
                           "simplification_tolerance_px": tolerance, "minimum_area_px2": min_area,
                           "dropped_specks": specks,
                           "unexplained_edge_share": round(unexplained, 4)},
        "contours": records,
        "current_svg": (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
                        f'viewBox="0 0 {width} {height}">' + "".join(svg_paths) + "</svg>")}
    output_dir.mkdir(parents=True, exist_ok=True)
    export = output_dir / "logo-designer-brief.json"
    export.write_text(json.dumps(package) + "\n", encoding="utf-8")
    (output_dir / "trace.svg").write_text(package["current_svg"] + "\n", encoding="utf-8")
    summary = prepare(export, output_dir)
    evidence_bytes = (output_dir / "evidence.json").stat().st_size
    complex_image = len(records) > 300 or evidence_bytes > 200_000
    summary.update(export_file=str(export), trace_svg=str(output_dir / "trace.svg"), mode=mode,
                   evidence_bytes=evidence_bytes,
                   **({"warning": "Too detailed to be a logo. Do not read evidence.json or rebuild by hand: "
                                  "run scripts/trace_lineart.py on the image instead."} if complex_image else {}),
                   core_palette=[f'{entry["hex"]} {entry["role"]} {entry["share"]:.1%}' for entry in core],
                   outer_contours=sum(r["role"] == "outer" for r in records),
                   hole_contours=sum(r["role"] == "hole" for r in records),
                   boundary_points=sum(len(r["boundary_points_px"]) for r in records),
                   dropped_specks=specks, unexplained_edge_share=round(unexplained, 4))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path, help="PNG, JPEG, WebP or another raster Pillow can open")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--mode", choices=("auto", "color", "mono"), default="auto",
                        help="color: one layer per palette color; mono: one silhouette of everything "
                             "that is not background; auto (default): color when 2+ inks are found")
    parser.add_argument("--threshold", type=int, choices=range(1, 256), metavar="1..255", default=128,
                        help="membership level (0-255) where boundaries are placed; default 128")
    parser.add_argument("--tolerance", type=float, default=0.35, help="simplification error in pixels")
    parser.add_argument("--min-area", type=float, default=4.0,
                        help="drop specks smaller than this (px^2); the count is reported")
    parser.add_argument("--background", help="opaque background color, e.g. '#ffffff'; detected from the border otherwise")
    args = parser.parse_args()
    try:
        fixed = list(ImageColor.getrgb(args.background)[:3]) if args.background else None
        print(json.dumps(build(args.image, args.out, args.threshold, args.tolerance,
                               args.min_area, fixed, args.mode), indent=2))
    except (OSError, ValueError) as error:
        parser.exit(1, f"Cannot extract contours: {error}\n")
