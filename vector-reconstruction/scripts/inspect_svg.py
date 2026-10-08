#!/usr/bin/env python3
"""Audit an SVG like a designer in outline mode: tracing debris, path quality, color and production
issues. Writes report.json and wireframe.svg (anchors, handles and flagged points, optionally over the
source image). Uses only the Python standard library."""
import argparse
import base64
import json
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path

SVG = "{http://www.w3.org/2000/svg}"
XLINK = "{http://www.w3.org/1999/xlink}href"
NUMBER = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
ARITY = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2, "A": 7, "Z": 0}
SHAPES = {"path", "rect", "circle", "ellipse", "line", "polyline", "polygon"}
SKIP = {"defs", "clipPath", "mask", "symbol", "marker", "pattern", "linearGradient", "radialGradient",
        "title", "desc", "metadata", "style", "filter"}
INHERITED = {"fill", "stroke", "stroke-width", "fill-rule", "fill-opacity", "stroke-opacity", "visibility",
             "stroke-linecap", "stroke-linejoin"}
PROPERTIES = INHERITED | {"display", "opacity", "clip-path", "mask", "filter", "transform"}
NAMED = {"white": "#ffffff", "black": "#000000", "red": "#ff0000", "blue": "#0000ff", "green": "#008000",
         "gray": "#808080", "grey": "#808080", "transparent": "none"}
# Severity of production checks by output target; see references/production-targets.md.
SEVERITY = {
    "stroke": {"cut": "error", "embroidery": "error", "print": "info"},
    "open-filled-path": {"cut": "error", "embroidery": "error"},
    "clip-mask-filter": {"cut": "error", "embroidery": "error"},
    "gradient": {"cut": "error", "embroidery": "error", "print": "info", "web": "info", "animation": "info"},
    "text": {"web": "warn", "animation": "warn"},
}
DEFAULT_SEVERITY = {"stroke": "info", "open-filled-path": "warn", "clip-mask-filter": "warn",
                    "gradient": "warn", "text": "error"}


def local(tag):
    return tag.split("}")[-1] if isinstance(tag, str) else ""


def length(value, default=0.0):
    match = NUMBER.match((value or "").strip())
    return float(match.group()) if match else default


# ---- transforms -------------------------------------------------------------------------------

def multiply(m, n):
    a, b, c, d, e, f = m
    a2, b2, c2, d2, e2, f2 = n
    return (a * a2 + c * b2, b * a2 + d * b2, a * c2 + c * d2, b * c2 + d * d2,
            a * e2 + c * f2 + e, b * e2 + d * f2 + f)


def parse_transform(text):
    matrix = (1, 0, 0, 1, 0, 0)
    for name, args in re.findall(r"(\w+)\s*\(([^)]*)\)", text or ""):
        v = [float(x) for x in NUMBER.findall(args)]
        if name == "matrix" and len(v) == 6:
            step = tuple(v)
        elif name == "translate":
            step = (1, 0, 0, 1, v[0], v[1] if len(v) > 1 else 0)
        elif name == "scale":
            step = (v[0], 0, 0, v[1] if len(v) > 1 else v[0], 0, 0)
        elif name == "rotate":
            r = math.radians(v[0])
            step = (math.cos(r), math.sin(r), -math.sin(r), math.cos(r), 0, 0)
            if len(v) == 3:
                step = multiply(multiply((1, 0, 0, 1, v[1], v[2]), step), (1, 0, 0, 1, -v[1], -v[2]))
        elif name == "skewX":
            step = (1, 0, math.tan(math.radians(v[0])), 1, 0, 0)
        elif name == "skewY":
            step = (1, math.tan(math.radians(v[0])), 0, 1, 0, 0)
        else:
            continue
        matrix = multiply(matrix, step)
    return matrix


def apply(m, p):
    return (m[0] * p[0] + m[2] * p[1] + m[4], m[1] * p[0] + m[3] * p[1] + m[5])


def apply_vector(m, v):
    return (m[0] * v[0] + m[2] * v[1], m[1] * v[0] + m[3] * v[1])


# ---- path parsing and geometry --------------------------------------------------------------

def tokenize(d):
    """Yield (command, args) groups, splitting implicit repeats; arc flags may be written unspaced."""
    i, n, command = 0, len(d), None
    while True:
        while i < n and d[i] in " \t\r\n,":
            i += 1
        if i >= n:
            return
        if d[i].isalpha():
            command, i = d[i], i + 1
            if command in "Zz":
                yield command, []
                continue
        elif command is None or command in "Zz":
            raise ValueError(f"Unexpected path data near {d[i:i + 12]!r}")
        args = []
        for index in range(ARITY[command.upper()]):
            while i < n and d[i] in " \t\r\n,":
                i += 1
            if command in "Aa" and index in (3, 4):
                if i >= n or d[i] not in "01":
                    raise ValueError("Invalid arc flag in path data")
                args.append(float(d[i]))
                i += 1
                continue
            match = NUMBER.match(d, i)
            if not match:
                raise ValueError(f"Incomplete path data near {d[i:i + 12]!r}")
            args.append(float(match.group()))
            i = match.end()
        yield command, args
        if command in "Mm":
            command = "l" if command == "m" else "L"


def arc_samples(p0, p1, rx, ry, phi, large, sweep, steps=24):
    """Points along an SVG endpoint arc plus its start and end tangents (spec F.6.5)."""
    rx, ry = abs(rx), abs(ry)
    if rx == 0 or ry == 0 or p0 == p1:
        v = (p1[0] - p0[0], p1[1] - p0[1])
        return [p0, p1], v, v
    cos, sin = math.cos(math.radians(phi)), math.sin(math.radians(phi))
    dx, dy = (p0[0] - p1[0]) / 2, (p0[1] - p1[1]) / 2
    x1, y1 = cos * dx + sin * dy, -sin * dx + cos * dy
    scale = x1 * x1 / (rx * rx) + y1 * y1 / (ry * ry)
    if scale > 1:
        rx, ry = rx * math.sqrt(scale), ry * math.sqrt(scale)
    numerator = rx * rx * ry * ry - rx * rx * y1 * y1 - ry * ry * x1 * x1
    denominator = rx * rx * y1 * y1 + ry * ry * x1 * x1
    factor = math.sqrt(max(0.0, numerator / denominator)) * (-1 if large == sweep else 1)
    cx1, cy1 = factor * rx * y1 / ry, -factor * ry * x1 / rx
    cx = cos * cx1 - sin * cy1 + (p0[0] + p1[0]) / 2
    cy = sin * cx1 + cos * cy1 + (p0[1] + p1[1]) / 2
    start = math.atan2((y1 - cy1) / ry, (x1 - cx1) / rx)
    delta = math.atan2((-y1 - cy1) / ry, (-x1 - cx1) / rx) - start
    if sweep and delta < 0:
        delta += 2 * math.pi
    elif not sweep and delta > 0:
        delta -= 2 * math.pi

    def at(t):
        return (cx + rx * cos * math.cos(t) - ry * sin * math.sin(t),
                cy + rx * sin * math.cos(t) + ry * cos * math.sin(t))

    def tangent(t):
        s = 1 if delta >= 0 else -1
        return (s * (-rx * cos * math.sin(t) - ry * sin * math.cos(t)),
                s * (-rx * sin * math.sin(t) + ry * cos * math.cos(t)))

    count = max(4, int(abs(delta) / (2 * math.pi) * steps * 2))
    points = [at(start + delta * k / count) for k in range(count + 1)]
    points[0], points[-1] = p0, p1
    return points, tangent(start), tangent(start + delta)


def bezier(points, t):
    while len(points) > 1:
        points = [((1 - t) * a[0] + t * b[0], (1 - t) * a[1] + t * b[1]) for a, b in zip(points, points[1:])]
    return points[0]


def nonzero(*vectors):
    return next((v for v in vectors if math.hypot(*v) > 1e-9), (0.0, 0.0))


def segment(kind, p0, p1, controls=(), arc=None):
    """A segment in local coordinates; finalized (sampled, tangents) after transforming."""
    return {"type": kind, "p0": p0, "p1": p1, "controls": list(controls), "arc": arc}


def path_subpaths(d):
    subpaths, current, cursor, start, last_control, last = [], None, (0.0, 0.0), (0.0, 0.0), None, ""
    for command, args in tokenize(d):
        upper, relative = command.upper(), command.islower()

        def point(x, y):
            return (cursor[0] + x, cursor[1] + y) if relative else (x, y)

        if upper == "M":
            cursor = start = point(*args)
            current = {"segments": [], "closed": False, "start": cursor}
            subpaths.append(current)
            last_control, last = None, "M"
            continue
        if current is None or current["closed"]:
            current = {"segments": [], "closed": False, "start": start}
            subpaths.append(current)
        if upper == "Z":
            if math.dist(cursor, start) > 1e-9:
                current["segments"].append(segment("L", cursor, start))
            current["closed"] = True
            cursor, last_control, last = start, None, "Z"
            continue
        if upper == "L":
            end = point(*args)
            current["segments"].append(segment("L", cursor, end))
        elif upper == "H":
            end = (cursor[0] + args[0] if relative else args[0], cursor[1])
            current["segments"].append(segment("L", cursor, end))
        elif upper == "V":
            end = (cursor[0], cursor[1] + args[0] if relative else args[0])
            current["segments"].append(segment("L", cursor, end))
        elif upper in "CS":
            if upper == "C":
                c1, c2, end = point(*args[0:2]), point(*args[2:4]), point(*args[4:6])
            else:
                c1 = (2 * cursor[0] - last_control[0], 2 * cursor[1] - last_control[1]) \
                    if last in "CS" and last_control else cursor
                c2, end = point(*args[0:2]), point(*args[2:4])
            current["segments"].append(segment("C", cursor, end, (c1, c2)))
            last_control = c2
        elif upper in "QT":
            if upper == "Q":
                c, end = point(*args[0:2]), point(*args[2:4])
            else:
                c = (2 * cursor[0] - last_control[0], 2 * cursor[1] - last_control[1]) \
                    if last in "QT" and last_control else cursor
                end = point(*args[0:2])
            current["segments"].append(segment("Q", cursor, end, (c,)))
            last_control = c
        elif upper == "A":
            end = point(*args[5:7])
            current["segments"].append(segment("A", cursor, end, arc=tuple(args[0:5])))
        cursor, last = end, upper
        if upper not in "CSQT":
            last_control = None
    return [s for s in subpaths if s["segments"]]


def shape_subpaths(element, tag):
    g = lambda name, default=0.0: length(element.get(name), default)
    if tag == "path":
        return path_subpaths(element.get("d", ""))
    if tag in ("circle", "ellipse"):
        cx, cy = g("cx"), g("cy")
        rx, ry = (g("r"), g("r")) if tag == "circle" else (g("rx"), g("ry"))
        if rx <= 0 or ry <= 0:
            return []
        a, b = (cx + rx, cy), (cx - rx, cy)
        return [{"closed": True, "start": a, "segments": [segment("A", a, b, arc=(rx, ry, 0, 0, 1)),
                                                          segment("A", b, a, arc=(rx, ry, 0, 0, 1))]}]
    if tag == "rect":
        x, y, w, h = g("x"), g("y"), g("width"), g("height")
        if w <= 0 or h <= 0:
            return []
        rx = element.get("rx")
        ry = element.get("ry")
        rx, ry = length(rx if rx is not None else ry), length(ry if ry is not None else rx)
        rx, ry = min(rx, w / 2), min(ry, h / 2)
        if rx <= 0 or ry <= 0:
            corners = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
            return [{"closed": True, "start": corners[0],
                     "segments": [segment("L", a, b) for a, b in zip(corners, corners[1:] + corners[:1])]}]
        p = [(x + rx, y), (x + w - rx, y), (x + w, y + ry), (x + w, y + h - ry),
             (x + w - rx, y + h), (x + rx, y + h), (x, y + h - ry), (x, y + ry)]
        segments = []
        for i in range(0, 8, 2):
            segments.append(segment("L", p[i], p[i + 1]))
            segments.append(segment("A", p[i + 1], p[(i + 2) % 8], arc=(rx, ry, 0, 0, 1)))
        return [{"closed": True, "start": p[0], "segments": [s for s in segments if s["p0"] != s["p1"]]}]
    if tag == "line":
        a, b = (g("x1"), g("y1")), (g("x2"), g("y2"))
        return [{"closed": False, "start": a, "segments": [segment("L", a, b)]}]
    values = [float(v) for v in NUMBER.findall(element.get("points", ""))]
    points = list(zip(values[0::2], values[1::2]))
    if len(points) < 2:
        return []
    closed = tag == "polygon"
    pairs = list(zip(points, points[1:] + (points[:1] if closed else [])))
    return [{"closed": closed, "start": points[0], "segments": [segment("L", a, b) for a, b in pairs]}]


def finalize(subpaths, matrix):
    """Transform to root coordinates and attach samples and tangents."""
    for subpath in subpaths:
        for s in subpath["segments"]:
            if s["type"] == "A":
                points, t0, t1 = arc_samples(s["p0"], s["p1"], *s["arc"])
                s["samples"] = [apply(matrix, p) for p in points]
                s["t0"], s["t1"] = apply_vector(matrix, t0), apply_vector(matrix, t1)
                s["controls"] = []
            else:
                local_points = [s["p0"], *s["controls"], s["p1"]]
                if s["type"] == "L":
                    samples = local_points
                else:
                    samples = [bezier(local_points, k / 16) for k in range(17)]
                s["samples"] = [apply(matrix, p) for p in samples]
                s["controls"] = [apply(matrix, c) for c in s["controls"]]
            s["p0"], s["p1"] = apply(matrix, s["p0"]), apply(matrix, s["p1"])
            if s["type"] != "A":
                chain = [s["p0"], *s["controls"], s["p1"]]
                s["t0"] = nonzero(*[(q[0] - s["p0"][0], q[1] - s["p0"][1]) for q in chain[1:]])
                s["t1"] = nonzero(*[(s["p1"][0] - q[0], s["p1"][1] - q[1]) for q in reversed(chain[:-1])])
        samples = [p for s in subpath["segments"] for p in s["samples"]]
        subpath["polygon"] = samples
        subpath["area"] = sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(samples, samples[1:] + samples[:1])) / 2
        xs, ys = [p[0] for p in samples], [p[1] for p in samples]
        subpath["bbox"] = (min(xs), min(ys), max(xs), max(ys))
    return subpaths


def angle(u, v):
    return abs(math.degrees(math.atan2(u[0] * v[1] - u[1] * v[0], u[0] * v[0] + u[1] * v[1])))


def inside(point, polygon):
    x, y, hit = point[0], point[1], False
    for (x1, y1), (x2, y2) in zip(polygon, polygon[1:] + polygon[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            hit = not hit
    return hit


def rasterize(subpaths, rule, scale, origin, rows, columns):
    """Coverage mask of a filled shape on a coarse grid: {row: bitmask of covered pixel centers}."""
    crossings = {}
    for subpath in subpaths:
        points = [((x - origin[0]) * scale, (y - origin[1]) * scale) for x, y in subpath["polygon"]]
        for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1]):
            if y1 == y2:
                continue
            direction = 1 if y2 > y1 else -1
            low, high = min(y1, y2), max(y1, y2)
            for row in range(max(0, math.ceil(low - 0.5)), min(rows - 1, math.ceil(high - 0.5) - 1) + 1):
                x = x1 + (row + 0.5 - y1) * (x2 - x1) / (y2 - y1)
                crossings.setdefault(row, []).append((x, direction))
    mask = {}
    for row, hits in crossings.items():
        hits.sort()
        bits = winding = 0
        for count, ((x, direction), following) in enumerate(zip(hits, hits[1:])):
            winding += direction
            if (winding != 0) if rule == "nonzero" else (count % 2 == 0):
                start, end = max(0, math.ceil(x - 0.5)), min(columns, math.ceil(following[0] - 0.5))
                if end > start:
                    bits |= (1 << end) - (1 << start)
        if bits:
            mask[row] = bits
    return mask


def coverage(mask):
    return sum(bits.bit_count() for bits in mask.values())


def stacking(records, view, add):
    """Find stacked duplicates (same object drawn twice, any color or offset) and shapes hidden by later ones."""
    scale = 640 / max(view[2], view[3])
    rows, columns = math.ceil(view[3] * scale), math.ceil(view[2] * scale)
    masks = {}
    for index, record in enumerate(records):
        if record["_fill"] and record["_opaque"]:
            mask = rasterize(record["_subpaths"], record["_rule"], scale, view[:2], rows, columns)
            if coverage(mask) >= 6:
                masks[index] = (mask, coverage(mask))
    order = sorted(masks)
    reported = set()
    for position, upper in enumerate(order):
        mask_upper, area_upper = masks[upper]
        for lower in reversed(order[:position]):
            mask_lower, area_lower = masks[lower]
            if min(area_lower, area_upper) / max(area_lower, area_upper) < 0.8:
                continue
            shared = sum((bits & mask_lower.get(row, 0)).bit_count() for row, bits in mask_upper.items())
            overlap = shared / (area_lower + area_upper - shared)
            if overlap < 0.9:
                continue
            reported.add(lower)
            records[upper]["_issue"] = records[lower]["_issue"] = True
            if records[upper].get("_exact_of") == lower:
                break  # already reported as an exact duplicate
            a, b = records[lower], records[upper]
            if a["fill"] == b["fill"]:
                detail = "same color: delete one"
            else:
                detail = (f"{a['fill']} under {b['fill']}: keep the one that matches the source; the lower copy "
                          "is hidden or shows only as a halo")
            add("stacked-duplicate", f"Same object as {a['id']} drawn again on top ({overlap:.0%} overlap, "
                                     f"{detail}).", b["id"])
            break
    above = {}
    for index in reversed(order):
        mask, area = masks[index]
        record = records[index]
        if index not in reported and not record["_stroked"]:
            visible = sum((bits & ~above.get(row, 0)).bit_count() for row, bits in mask.items()) / area
            if visible < 0.01:
                record["_issue"] = True
                add("hidden-under", "Completely covered by shapes painted above it; nothing of it shows. "
                                    "Delete it (or merge it if it is a stacked trace layer).", record["id"])
            elif visible < 0.1:
                record["_issue"] = True
                add("mostly-hidden", f"Only {visible:.0%} of it shows past the shapes above; likely a stacked "
                                     "copy or trace layer. Trim it to the visible part or delete it.", record["id"])
        for row, bits in mask.items():
            above[row] = above.get(row, 0) | bits


# ---- styles -----------------------------------------------------------------------------------

def parse_css(root):
    rules = []
    text = " ".join(e.text or "" for e in root.iter() if local(e.tag) == "style")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    for selectors, body in re.findall(r"([^{}]+)\{([^}]*)\}", text):
        declarations = dict((k.strip(), v.strip().replace("!important", "").strip())
                            for k, _, v in (item.partition(":") for item in body.split(";")) if v.strip())
        for selector in selectors.split(","):
            rules.append((selector.strip(), declarations))
    return rules


def own_style(element, rules):
    style = {k: v for k, v in element.attrib.items() if k in PROPERTIES}
    classes = set(element.get("class", "").split())
    for selector, declarations in rules:
        if (selector == "*" or selector == local(element.tag) or
                (selector.startswith(".") and selector[1:] in classes) or
                (selector.startswith("#") and selector[1:] == element.get("id"))):
            style.update(declarations)
    for item in element.get("style", "").split(";"):
        key, _, value = item.partition(":")
        if value.strip():
            style[key.strip()] = value.strip()
    return style


def normalize_color(value):
    value = (value or "").strip().lower()
    value = NAMED.get(value, value)
    if re.fullmatch(r"#[0-9a-f]{3}", value):
        return "#" + "".join(c * 2 for c in value[1:])
    match = re.fullmatch(r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+).*\)", value)
    if match:
        return "#%02x%02x%02x" % tuple(round(float(v)) for v in match.groups())
    return value


def rgb(color):
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5)) if re.fullmatch(r"#[0-9a-f]{6}", color) else None


# ---- inspection -------------------------------------------------------------------------------

def collect(root, rules):
    """Walk rendered geometry (following <use>) with computed style and transform."""
    ids = {e.get("id"): e for e in root.iter() if e.get("id")}
    shapes, notes = [], []

    def walk(element, matrix, inherited, depth, hidden, via_use):
        tag = local(element.tag)
        if tag in SKIP or depth > 32:
            return
        style = own_style(element, rules)
        computed = {**inherited, **{k: v for k, v in style.items() if k in INHERITED}}
        matrix = multiply(matrix, parse_transform(style.get("transform", element.get("transform"))))
        hidden = hidden or style.get("display") == "none" or computed.get("visibility") == "hidden"
        if tag in ("image", "text", "foreignObject", "script"):
            notes.append((tag, element))
        if tag == "use":
            target = ids.get((element.get("href") or element.get(XLINK) or "").lstrip("#"))
            if target is not None:
                offset = (1, 0, 0, 1, length(element.get("x")), length(element.get("y")))
                children = list(target) if local(target.tag) in ("symbol", "g") else [target]
                for child in children:
                    walk(child, multiply(matrix, offset), computed, depth + 1, hidden, True)
            return
        if tag in SHAPES:
            shapes.append({"element": element, "tag": tag, "matrix": matrix, "style": computed,
                           "own": style, "hidden": hidden, "via_use": via_use})
        for child in element:
            walk(child, matrix, computed, depth + 1, hidden, via_use)

    walk(root, (1, 0, 0, 1, 0, 0), {"fill": "#000000", "stroke": "none"}, 0, False, False)
    return shapes, notes


def circle_through(points):
    """Least-squares circle (centre, radius, worst residual) through sample points, or None."""
    n = len(points)
    sx = sum(x for x, _ in points); sy = sum(y for _, y in points)
    mx, my = sx / n, sy / n
    u = [(x - mx, y - my) for x, y in points]
    suu = sum(a * a for a, _ in u); svv = sum(b * b for _, b in u); suv = sum(a * b for a, b in u)
    suuu = sum(a ** 3 for a, _ in u); svvv = sum(b ** 3 for _, b in u)
    suvv = sum(a * b * b for a, b in u); svuu = sum(b * a * a for a, b in u)
    det = suu * svv - suv * suv
    if abs(det) < 1e-12:
        return None
    rhs1, rhs2 = (suuu + suvv) / 2, (svvv + svuu) / 2
    uc, vc = (rhs1 * svv - rhs2 * suv) / det, (suu * rhs2 - suv * rhs1) / det
    cx, cy = uc + mx, vc + my
    radius = math.sqrt(uc * uc + vc * vc + (suu + svv) / n)
    worst = max(abs(math.dist(p, (cx, cy)) - radius) for p in points)
    return (cx, cy), radius, worst



def edge_distance(point, polygon):
    """Distance from `point` to the nearest edge of the closed polyline `polygon`."""
    best = math.inf
    for i in range(len(polygon)):
        a, b = polygon[i], polygon[(i + 1) % len(polygon)]
        edge = (b[0] - a[0], b[1] - a[1])
        length2 = edge[0] * edge[0] + edge[1] * edge[1]
        t = 0.0 if length2 < 1e-12 else max(0.0, min(1.0, ((point[0] - a[0]) * edge[0] +
                                                            (point[1] - a[1]) * edge[1]) / length2))
        foot = (a[0] + t * edge[0], a[1] + t * edge[1])
        best = min(best, math.dist(point, foot))
    return best

def construction(records, size, add):
    """Arcs that almost, but not exactly, share a centre or a radius: a designer gives them one exact value."""
    arcs = []
    for record in records:
        for subpath in record["_subpaths"]:
            for seg in subpath["segments"]:
                samples = seg.get("samples") or []
                if seg["type"] not in ("A", "C") or len(samples) < 5:
                    continue
                fit = circle_through(samples)
                if fit is None:
                    continue
                center, radius, worst = fit
                if radius < size * 0.005 or radius > size * 4 or worst > max(0.05, 0.002 * radius):
                    continue                      # not a circular arc (or too small to matter)
                a0 = math.atan2(samples[0][1] - center[1], samples[0][0] - center[0])
                a1 = math.atan2(samples[-1][1] - center[1], samples[-1][0] - center[0])
                sweep = abs((a1 - a0 + math.pi) % (2 * math.pi) - math.pi)
                if seg["type"] == "C" and sweep < math.radians(20):
                    continue
                arcs.append((record["id"], center, radius, seg["p0"]))
    reported = set()
    for i, (name, c1, r1, at) in enumerate(arcs):
        for j in range(i + 1, len(arcs)):
            _, c2, r2, _ = arcs[j]
            gap = math.dist(c1, c2)
            if 0.1 < gap <= max(1.0, 0.02 * min(r1, r2)):      # below 0.1 px is coordinate rounding
                key = ("centre", round(c1[0]), round(c1[1]))
                if key not in reported:
                    reported.add(key)
                    add("near-concentric", f"Arcs almost share a centre ({gap:.2f} px apart): give them one "
                                           "exact centre.", name, at)
            elif gap <= 0.1 and 0.05 < abs(r1 - r2) <= max(0.5, 0.004 * max(r1, r2)):
                key = ("radius", round(min(r1, r2), 1))
                if key not in reported:
                    reported.add(key)
                    add("near-equal-radius", f"Concentric arcs with radii {r1:.2f} and {r2:.2f}: make them one "
                                             "radius, or separate them clearly.", name, at)



def seams(records, size, add):
    """Separate shapes of the same fill color that touch or nearly touch: anti-aliasing can leave a hairline
    gap there even when the coordinates match. A designer unions touching same-color shapes into one path."""
    tol = max(0.5, size * 0.001)
    reported = set()
    for i, a in enumerate(records):
        if not a["_fill"] or a["fill"] is None:
            continue
        for b in records[i + 1:]:
            if not b["_fill"] or b["fill"] != a["fill"]:
                continue
            abox, bbox = a["bbox"], b["bbox"]
            if (abox[2] + tol < bbox[0] or bbox[2] + tol < abox[0] or
                    abox[3] + tol < bbox[1] or bbox[3] + tol < abox[1]):
                continue
            thin = lambda points: points[::max(1, len(points) // 150)]  # noqa: E731
            pa = thin([p for s in a["_subpaths"] for p in s["polygon"]])
            pb = thin([p for s in b["_subpaths"] for p in s["polygon"]])
            if not pa or not pb:
                continue
            # Point-to-point distance misses a tangent that lands on a straight edge rather than a
            # sampled vertex (a rectangle has only 4), so check each shape's points against the other's
            # edges. Each subpath's own ring is tested separately: concatenating them as one polygon
            # would draw a bogus edge straight across the canvas, from one subpath's end to the next's start.
            near_b = lambda p: min(edge_distance(p, s["polygon"]) for s in b["_subpaths"])  # noqa: E731
            near_a = lambda q: min(edge_distance(q, s["polygon"]) for s in a["_subpaths"])  # noqa: E731
            at, gap = min([(p, near_b(p)) for p in pa] + [(q, near_a(q)) for q in pb], key=lambda pair: pair[1])
            if gap <= tol:
                key = tuple(sorted((a["id"], b["id"])))
                if key not in reported:
                    reported.add(key)
                    add("touching-same-fill", f"{a['id']} and {b['id']} are separate shapes of the same fill "
                                              f"color ({a['fill']}) that touch or nearly touch ({gap:.2f} px "
                                              "apart): anti-aliasing can leave a hairline seam there. Union them "
                                              "into one path (one <path>, both as subpaths) instead of two "
                                              "elements.", a["id"], at)


def inspect(svg_path, target, background, min_feature):
    tree = ET.parse(svg_path)
    root = tree.getroot()
    rules = parse_css(root)
    view = [float(v) for v in NUMBER.findall(root.get("viewBox", ""))]
    findings = []

    def add(check, message, element=None, at=None, severity=None):
        if severity is None:
            severity = SEVERITY.get(check, {}).get(target, DEFAULT_SEVERITY.get(check, "warn"))
        findings.append({"severity": severity, "check": check, "element": element,
                         "message": message, **({"at": [round(at[0], 2), round(at[1], 2)]} if at else {})})

    if len(view) != 4:
        add("viewbox", "Missing viewBox: the artwork will not scale predictably.", severity="error")
        view = [0, 0, length(root.get("width"), 100), length(root.get("height"), 100)]
    size = max(view[2], view[3])
    min_feature = min_feature if min_feature is not None else size * 0.0075
    shapes, notes = collect(root, rules)

    for tag, element in notes:
        name = element.get("id") or tag
        if tag == "text":
            add("text", "Live <text> depends on installed fonts; convert lettering to outlines for a logo master.", name)
        elif tag == "image":
            add("raster", "Embedded raster image; confirm it is intended and disclosed.", name, severity="warn")
        else:
            add("unsafe", f"<{tag}> in a logo file; remove it.", name, severity="error")
    for element in root.iter():
        href = element.get("href") or element.get(XLINK) or ""
        if href.startswith(("http:", "https:", "//")):
            add("external", f"External resource {href!r}; make the SVG self-contained.", element.get("id"), severity="error")
        if local(element.tag) == "g" and len(element) == 0:
            add("empty-group", "Empty group.", element.get("id"), severity="warn")
        if local(element.tag) in ("linearGradient", "radialGradient"):
            add("gradient", "Gradient fill; confirm it reflects real color transition in the source.", element.get("id"))
    if any(e.get("clip-path") or e.get("mask") or e.get("filter") or
           re.search(r"clip-path|mask|filter", e.get("style", "")) for e in root.iter()):
        add("clip-mask-filter", "Clipping paths, masks or filters are used; they can break cutting, "
                                "embroidery and some converters. Prefer expanded geometry.")

    records, colors, signatures = [], {}, {}
    painted_boxes = []
    for index, shape in enumerate(shapes):
        element, tag, style = shape["element"], shape["tag"], shape["style"]
        name = element.get("id") or f"{tag}[{index}]"
        try:
            subpaths = finalize(shape_subpaths(element, tag), shape["matrix"])
        except ValueError as error:
            add("path-data", f"Unparseable geometry: {error}", name, severity="error")
            continue
        fill, stroke = normalize_color(style.get("fill")), normalize_color(style.get("stroke"))
        has_fill = fill != "none" and length(style.get("fill-opacity"), 1) > 0 and tag != "line"
        has_stroke = stroke != "none" and length(style.get("stroke-width"), 1) > 0
        if not subpaths:
            add("empty-shape", "Shape has no geometry.", name, severity="warn")
            continue
        if shape["hidden"] or length(shape["own"].get("opacity"), 1) == 0 or not (has_fill or has_stroke):
            add("hidden", "Invisible or hidden geometry; delete it unless it is a deliberate guide.", name, severity="warn")
        for paint, used in ((fill, has_fill), (stroke, has_stroke)):
            if used:
                colors[paint] = colors.get(paint, 0) + 1
        if has_stroke:
            add("stroke", f"Live stroke ({stroke}, width {style.get('stroke-width', '1')}). Outline it only if "
                          "the output target requires it.", name)
        box = (min(s["bbox"][0] for s in subpaths), min(s["bbox"][1] for s in subpaths),
               max(s["bbox"][2] for s in subpaths), max(s["bbox"][3] for s in subpaths))
        anchors = sum(len(s["segments"]) + (0 if s["closed"] else 1) for s in subpaths)
        kinds = {}
        flagged, polyline_turns = [], 0
        for s in subpaths:
            for seg in s["segments"]:
                kinds[seg["type"]] = kinds.get(seg["type"], 0) + 1
            if s["closed"] is False and has_fill and not has_stroke:
                add("open-filled-path", "Open subpath with a fill: closes only implicitly.", name, s["start"])
            width, height = s["bbox"][2] - s["bbox"][0], s["bbox"][3] - s["bbox"][1]
            if max(width, height) < min_feature and has_fill:
                flagged.append(s["polygon"][0])
                add("speck", f"Tiny subpath ({width:.2f} x {height:.2f}); likely tracing debris.", name, s["polygon"][0])
            if tag != "path":
                continue
            segments = s["segments"]
            joins = list(zip(segments, segments[1:])) + ([(segments[-1], segments[0])] if s["closed"] else [])
            for before, after in joins:
                turn = angle(before["t1"], after["t0"])
                if 0.5 < turn < 25 and before["type"] == after["type"] == "L":
                    polyline_turns += 1
                    flagged.append(after["p0"])
                elif 0.5 < turn < 12:
                    flagged.append(after["p0"])
                    add("near-smooth", f"Almost-smooth join ({turn:.1f} deg): align the handles or make an "
                                       "intentional corner.", name, after["p0"])
                elif turn <= 0.5 and before["type"] == after["type"] == "L":
                    flagged.append(after["p0"])
                    add("redundant-anchor", "Anchor in the middle of a straight run.", name, after["p0"])
            for seg in segments:
                chord = math.dist(seg["p0"], seg["p1"])
                if 0 < chord < size * 0.002:
                    flagged.append(seg["p0"])
                    add("tiny-segment", f"Near-coincident anchors ({chord:.3f} apart).", name, seg["p0"])
                if seg["type"] == "L" and chord > size * 0.02:
                    tilt = math.degrees(math.atan2(abs(seg["p1"][1] - seg["p0"][1]), abs(seg["p1"][0] - seg["p0"][0])))
                    off = min(tilt, 90 - tilt)
                    if 0.05 < off < 1.5:
                        flagged.append(seg["p0"])
                        add("near-axis", f"Line is {off:.2f} deg off horizontal/vertical.", name, seg["p0"])
                if seg["type"] == "C" and chord > 0:
                    deviation = max(abs((seg["p1"][0] - seg["p0"][0]) * (seg["p0"][1] - c[1]) -
                                        (seg["p0"][0] - c[0]) * (seg["p1"][1] - seg["p0"][1])) / chord
                                    for c in seg["controls"])
                    if deviation < size * 0.0005:
                        add("straight-curve", "Cubic segment is a straight line; use a line segment.", name,
                            seg["p0"], severity="info")
        for i, first in enumerate(subpaths):
            for second in subpaths[i + 1:]:
                if (max(abs(u - v) for u, v in zip(first["bbox"], second["bbox"])) < size * 0.002 and
                        abs(abs(first["area"]) - abs(second["area"])) <= 0.02 * max(abs(first["area"]), 1e-9)):
                    flagged.append(second["polygon"][0])
                    add("duplicate-subpath", "The same outline appears twice inside one path: under evenodd the "
                                             "copies cancel out, under nonzero they double the edge. Keep one.",
                        name, second["polygon"][0], severity="warn")
        if polyline_turns >= 3:
            add("polyline-curve", f"{polyline_turns} shallow line-to-line turns: polylines are standing in for "
                                  "curves. Redraw those runs with a few arcs or Bezier segments.", name)
        if tag == "path" and len(subpaths) > 1 and style.get("fill-rule", "nonzero") == "nonzero" and has_fill:
            for i, inner in enumerate(subpaths):
                for j, outer in enumerate(subpaths):
                    if (i != j and abs(inner["area"]) < abs(outer["area"]) and
                            inside(inner["polygon"][0], outer["polygon"]) and inner["area"] * outer["area"] > 0):
                        add("winding", "Nested subpath winds the same way as its parent under nonzero fill, so "
                                       "it fills instead of making a hole. Reverse it or use evenodd.",
                            name, inner["polygon"][0], severity="warn")
                        break
        signature = (fill, stroke, tuple((round(p[0] / size * 2000), round(p[1] / size * 2000))
                                         for s in subpaths for p in s["polygon"][::4]))
        exact_of = signatures.get(signature)
        if exact_of is not None:
            add("duplicate", f"Exact duplicate of {records[exact_of]['id']} stacked in the same place: delete one.",
                name, severity="warn")
        else:
            signatures[signature] = len(records)
        covers = (box[0] <= view[0] + view[2] * 0.01 and box[1] <= view[1] + view[3] * 0.01 and
                  box[2] >= view[0] + view[2] * 0.99 and box[3] >= view[1] + view[3] * 0.99)
        if covers and has_fill and not painted_boxes:
            add("background", "Full-canvas background shape. Logos normally ship on transparency unless the "
                              "background is part of the mark.", name, severity="warn")
        elif has_fill and fill == background and any(
                b[0] < box[2] and box[0] < b[2] and b[1] < box[3] and box[1] < b[3] for b in painted_boxes):
            add("fake-knockout", f"Shape is painted in the background color ({background}) over other artwork. "
                                 "Make it a real hole so one-color, reversed and non-white placements work.", name)
        if has_fill or has_stroke:
            painted_boxes.append(box)
        records.append({"id": name, "tag": tag, "fill": fill if has_fill else None,
                        "stroke": stroke if has_stroke else None, "anchors": anchors if tag == "path" else None,
                        "native_primitive": tag != "path", "segments": kinds, "subpaths": len(subpaths),
                        "open_subpaths": sum(not s["closed"] for s in subpaths),
                        "bbox": [round(v, 2) for v in box], "via_use": shape["via_use"],
                        "_subpaths": subpaths, "_flagged": flagged, "_fill": has_fill, "_stroked": has_stroke,
                        "_rule": style.get("fill-rule", "nonzero").strip(), "_exact_of": exact_of,
                        "_issue": exact_of is not None,
                        "_opaque": (not shape["hidden"] and length(shape["own"].get("opacity"), 1) >= 1 and
                                    length(style.get("fill-opacity"), 1) >= 1)})

    stacking(records, view, add)
    construction(records, size, add)
    seams(records, size, add)

    palette = sorted(c for c in colors if rgb(c))
    for i, a in enumerate(palette):
        for b in palette[i + 1:]:
            if math.dist(rgb(a), rgb(b)) <= 24:
                add("near-duplicate-color", f"Near-identical colors {a} and {b}: snap them to one swatch.",
                    severity="warn")
    decimals = max((len(m) for m in re.findall(r"\.(\d+)", Path(svg_path).read_text(encoding="utf-8"))), default=0)
    if decimals > 3:
        add("precision", f"Coordinates use up to {decimals} decimals; 2-3 are enough at logo scale.", severity="info")
    top_level = [e for e in root if local(e.tag) in SHAPES | {"g", "use"}]
    if len(top_level) > 1 and not any(e.get("id") for e in top_level):
        add("structure", "No named components; group and id them (e.g. logo-mark, wordmark).", severity="info")
    summary = {"target": target, "view_box": view, "shapes": len(records),
               "path_anchors": sum(r["anchors"] or 0 for r in records),
               "native_primitives": sum(r["native_primitive"] for r in records),
               "colors": {c: colors[c] for c in sorted(colors)},
               "findings": {level: sum(f["severity"] == level for f in findings) for level in ("error", "warn", "info")}}
    return summary, records, findings, view


def wireframe(records, view, source):
    size = max(view[2], view[3])
    mark, line = size / 260, size / 900
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{" ".join(f"{v:g}" for v in view)}" '
             f'width="{view[2]:g}" height="{view[3]:g}"><rect x="{view[0]:g}" y="{view[1]:g}" width="{view[2]:g}" '
             f'height="{view[3]:g}" fill="#fff"/>']
    if source:
        data = base64.b64encode(Path(source).read_bytes()).decode()
        parts.append(f'<image href="data:image/png;base64,{data}" x="{view[0]:g}" y="{view[1]:g}" '
                     f'width="{view[2]:g}" height="{view[3]:g}" opacity="0.3" preserveAspectRatio="none"/>')
    fmt = lambda p: f"{p[0]:.2f} {p[1]:.2f}"
    for record in records:
        for s in record["_subpaths"]:
            d = [f"M{fmt(s['segments'][0]['p0'])}"]
            for seg in s["segments"]:
                if seg["type"] == "C":
                    d.append("C" + " ".join(fmt(p) for p in (*seg["controls"], seg["p1"])))
                elif seg["type"] == "Q":
                    d.append("Q" + " ".join(fmt(p) for p in (*seg["controls"], seg["p1"])))
                else:
                    d.append("L" + " ".join(fmt(p) for p in seg["samples"][1:]))
            style = (f'stroke="#e11d48" stroke-width="{line * 2.5:.3f}" stroke-dasharray="{mark:.2f} {mark:.2f}"'
                     if record["_issue"] else f'stroke="#2563eb" stroke-width="{line:.3f}"')
            parts.append(f'<path d="{" ".join(d)}{" Z" if s["closed"] else ""}" fill="none" {style}/>')
            anchors = [seg["p0"] for seg in s["segments"]] + ([] if s["closed"] else [s["segments"][-1]["p1"]])
            for seg in s["segments"]:
                # A cubic's first handle belongs to its start anchor, the second to its end; a quadratic's
                # single control is shared by both.
                handles = (list(zip((seg["p0"], seg["p1"]), seg["controls"])) if seg["type"] == "C" else
                           [(anchor, c) for c in seg["controls"] for anchor in (seg["p0"], seg["p1"])])
                for anchor, c in handles:
                    parts.append(f'<line x1="{anchor[0]:.2f}" y1="{anchor[1]:.2f}" x2="{c[0]:.2f}" '
                                 f'y2="{c[1]:.2f}" stroke="#94a3b8" stroke-width="{line:.3f}"/>')
                for c in seg["controls"]:
                    parts.append(f'<circle cx="{c[0]:.2f}" cy="{c[1]:.2f}" r="{mark * 0.45:.2f}" fill="#94a3b8"/>')
            for p in anchors:
                parts.append(f'<rect x="{p[0] - mark / 2:.2f}" y="{p[1] - mark / 2:.2f}" width="{mark:.2f}" '
                             f'height="{mark:.2f}" fill="#fff" stroke="#1e3a8a" stroke-width="{line:.3f}"/>')
        for p in record["_flagged"]:
            parts.append(f'<circle cx="{p[0]:.2f}" cy="{p[1]:.2f}" r="{mark * 1.6:.2f}" fill="none" '
                         f'stroke="#e11d48" stroke-width="{line * 2:.3f}"/>')
    parts.append("</svg>")
    return "\n".join(parts)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("svg", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--target", choices=("general", "web", "print", "cut", "embroidery", "animation"),
                        default="general", help="output use; sets the severity of production checks")
    parser.add_argument("--source", type=Path, help="source PNG to place under the wireframe")
    parser.add_argument("--background", default="#ffffff",
                        help="color that counts as background for fake-knockout detection (default #ffffff)")
    parser.add_argument("--min-feature", type=float,
                        help="subpaths smaller than this (user units) are specks; default 0.75%% of the viewBox")
    args = parser.parse_args()
    try:
        summary, records, findings, view = inspect(args.svg, args.target, normalize_color(args.background),
                                                   args.min_feature)
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "wireframe.svg").write_text(wireframe(records, view, args.source), encoding="utf-8")
        clean = [{k: v for k, v in r.items() if not k.startswith("_")} for r in records]
        (args.out / "report.json").write_text(json.dumps({"summary": summary, "elements": clean,
                                                          "findings": findings}, indent=2) + "\n", encoding="utf-8")
        grouped = {}
        for f in findings:
            grouped.setdefault((f["severity"], f["check"]), []).append(f)
        order = {"error": 0, "warn": 1, "info": 2}
        print(json.dumps(summary, indent=2))
        for (severity, check), items in sorted(grouped.items(), key=lambda kv: order[kv[0][0]]):
            where = ", ".join(sorted({str(i["element"]) for i in items if i["element"]}))[:120]
            print(f"[{severity}] {check} x{len(items)}: {items[0]['message']}" + (f" ({where})" if where else ""))
        print(f"Wrote {args.out / 'report.json'} and {args.out / 'wireframe.svg'}")
    except (OSError, ET.ParseError, ValueError) as error:
        parser.exit(1, f"Cannot inspect SVG: {error}\n")
