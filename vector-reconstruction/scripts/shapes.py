"""Whole-shape primitives: a contour that is practically a basic figure becomes that exact figure.

A senior designer never leaves a 98% circle as four anchors, a 1-degree-tilted rectangle, or a triangle
whose sides differ by a pixel. When a whole closed contour is within a small tolerance of a circle, an
ellipse, a rectangle, a square, an isosceles or right triangle, or a regular polygon, it is replaced by the
exact figure: true centre, equal radii and sides, right angles, axis-aligned when it was nearly so.

Used by fit_curves.py. Shapes are plain dicts; `outline` samples them, `path_data` writes them as path
commands (for compound paths with holes) and `element` writes the matching SVG element.
"""
import math

import numpy as np

import geometry as geo

ANGLE_TOLERANCE = 3.0        # degrees a corner or an orientation may be off
SIDE_TOLERANCE = 0.03        # relative difference of sides or radii still counted as equal
SIDE_FLOOR = 2.0             # pixels: differences below this never matter
MAX_SIDES = 12               # regular polygons up to this many sides (more look like circles)


def close(a, b, floor=SIDE_FLOOR, ratio=SIDE_TOLERANCE):
    return abs(a - b) <= max(floor, ratio * max(abs(a), abs(b)))


def wrap(angle):
    """Degrees folded into (-180, 180]."""
    return (angle + 180.0) % 360.0 - 180.0


def snap_axis(angle, step=90.0):
    """`angle` (degrees) moved onto the nearest multiple of `step` when within tolerance."""
    nearest = round(angle / step) * step
    return nearest if abs(angle - nearest) <= ANGLE_TOLERANCE else angle


def unit(angle):
    r = math.radians(angle)
    return np.array([math.cos(r), math.sin(r)])


# ---------------------------------------------------------------- ellipses and circles

def fit_ellipse(points):
    """Direct least-squares ellipse (Halir-Flusser): returns (centre, rx, ry, angle in degrees) or None."""
    mean = points.mean(axis=0)
    scale = float(points.std()) or 1.0
    x, y = ((points - mean) / scale).T
    d1 = np.column_stack([x * x, x * y, y * y])
    d2 = np.column_stack([x, y, np.ones(len(x))])
    s1, s2, s3 = d1.T @ d1, d1.T @ d2, d2.T @ d2
    try:
        t = -np.linalg.solve(s3, s2.T)
        m = s1 + s2 @ t
        m = np.array([m[2] / 2, -m[1], m[0] / 2])
        values, vectors = np.linalg.eig(m)
    except np.linalg.LinAlgError:
        return None
    cond = 4 * vectors[0] * vectors[2] - vectors[1] ** 2
    good = np.where(cond > 0)[0]
    if len(good) == 0:
        return None
    a1 = vectors[:, good[0]].real
    a, b, c, d, e, f = np.concatenate([a1, t @ a1])
    matrix = np.array([[a, b / 2], [b / 2, c]])
    try:
        centre = np.linalg.solve(2 * matrix, [-d, -e])
    except np.linalg.LinAlgError:
        return None
    level = a * centre[0] ** 2 + b * centre[0] * centre[1] + c * centre[1] ** 2 + d * centre[0] + e * centre[1] + f
    eigenvalues, axes = np.linalg.eigh(matrix)
    if np.any(eigenvalues * -level <= 0):
        return None
    radii = np.sqrt(-level / eigenvalues)
    angle = math.degrees(math.atan2(axes[1, 0], axes[0, 0]))
    centre = centre * scale + mean
    return centre, float(radii[0] * scale), float(radii[1] * scale), angle


def ellipse_shapes(loop):
    """Candidate figures for a smooth closed outline, simplest first: a circle, then the ellipse."""
    fit = fit_ellipse(loop)
    if fit is None:
        return []
    centre, rx, ry, angle = fit
    if min(rx, ry) <= 0 or max(rx, ry) / min(rx, ry) > 20:
        return []
    found = []
    if close(rx, ry, floor=2.0, ratio=0.05):            # practically round
        middle, radius, _ = geo.fit_circle(loop)
        found.append({"kind": "circle", "center": middle, "radius": float(radius), "nodes": 1})
    if rx < ry:
        rx, ry, angle = ry, rx, angle + 90.0
    angle = (angle + 90.0) % 180.0 - 90.0               # the major axis direction, in (-90, 90]
    if abs(abs(angle) - 90.0) <= ANGLE_TOLERANCE:       # nearly vertical: an axis-aligned one, swapped
        rx, ry, angle = ry, rx, 0.0
    elif abs(angle) <= ANGLE_TOLERANCE:
        angle = 0.0
    found.append({"kind": "ellipse", "center": centre, "rx": rx, "ry": ry, "angle": angle, "nodes": 1})
    return found


def circle_from_pieces(pieces):
    """The circle when every piece is an arc of the same circle (the constrained centre and radius)."""
    if not pieces or any(p["kind"] != "arc" for p in pieces):
        return None
    first = pieces[0]
    for p in pieces:
        if np.linalg.norm(p["center"] - first["center"]) > 1e-3 or abs(p["radius"] - first["radius"]) > 1e-3:
            return None
    return {"kind": "circle", "center": np.array(first["center"], dtype=float), "radius": float(first["radius"]),
            "nodes": 1}


# ---------------------------------------------------------------- polygons

def rectangle_shape(corners):
    edges = np.roll(corners, -1, axis=0) - corners
    lengths = np.linalg.norm(edges, axis=1)
    if lengths.min() < 1e-6:
        return None
    angles = np.degrees(np.arctan2(edges[:, 1], edges[:, 0]))
    for i in range(4):                                  # every corner a right angle
        if abs(abs(wrap(angles[(i + 1) % 4] - angles[i])) - 90.0) > ANGLE_TOLERANCE:
            return None
    if not (close(lengths[0], lengths[2]) and close(lengths[1], lengths[3])):
        return None
    sense = 1.0 if wrap(angles[1] - angles[0]) > 0 else -1.0
    turned = np.radians(angles - sense * 90.0 * np.arange(4))   # all four sides folded onto the first
    mean = wrap(math.degrees(math.atan2(np.sin(turned).sum(), np.cos(turned).sum())))
    quarter = round(mean / 90.0)
    angle = snap_axis(mean - 90.0 * quarter)
    w, h = float((lengths[0] + lengths[2]) / 2), float((lengths[1] + lengths[3]) / 2)
    if quarter % 2:                                     # the first side was the vertical one
        w, h = h, w
    if close(w, h):
        w = h = (w + h) / 2
    return {"kind": "square" if w == h else "rectangle", "center": corners.mean(axis=0), "w": w, "h": h,
            "angle": angle, "nodes": 4}


def regular_shape(corners):
    n = len(corners)
    if n < 3 or n > MAX_SIDES:
        return None
    centre = corners.mean(axis=0)
    spokes = corners - centre
    radii = np.linalg.norm(spokes, axis=1)
    mean_radius = float(radii.mean())
    if mean_radius < 1e-6 or any(not close(r, mean_radius) for r in radii):
        return None
    edges = np.linalg.norm(np.roll(corners, -1, axis=0) - corners, axis=1)
    if any(not close(e, edges.mean()) for e in edges):
        return None
    phi = np.degrees(np.arctan2(spokes[:, 1], spokes[:, 0]))
    sense = 1.0 if wrap(phi[1] - phi[0]) > 0 else -1.0
    step = 360.0 / n
    offsets = np.radians(phi - sense * step * np.arange(n))
    start = math.degrees(math.atan2(np.sin(offsets).sum(), np.cos(offsets).sum()))
    for i in range(n):
        if abs(wrap(phi[i] - (start + sense * step * i))) > ANGLE_TOLERANCE:
            return None
    # A vertex straight up/down/left/right, or a flat side on an axis, is how a designer sets it.
    for candidate in (round(start / 90.0) * 90.0, round((start - step / 2) / 90.0) * 90.0 + step / 2):
        if abs(wrap(start - candidate)) <= ANGLE_TOLERANCE:
            start = candidate
            break
    return {"kind": "polygon", "sides": n, "center": centre, "radius": mean_radius, "start": start,
            "sense": sense, "nodes": n}


def triangle_shape(corners):
    """Isosceles (two equal sides) and/or right-angled triangles made exact; None for any other triangle."""
    sides = [np.linalg.norm(corners[(i + 1) % 3] - corners[i]) for i in range(3)]
    apex = next((i for i in range(3) if close(sides[(i - 1) % 3], sides[i])), None)
    right = next((i for i in range(3) if abs(abs(wrap(
        math.degrees(math.atan2(*(corners[(i + 1) % 3] - corners[i])[::-1])
                     - math.atan2(*(corners[i] - corners[(i - 1) % 3])[::-1])))) - 90.0) <= ANGLE_TOLERANCE), None)
    if apex is None and right is None:
        return None
    if apex is not None and right is not None and apex != right:
        right = None
    if apex is not None:
        a, b, c = corners[apex], corners[(apex + 1) % 3], corners[(apex - 1) % 3]
        mid = (b + c) / 2
        base = c - b
        length = float(np.linalg.norm(base))
        along = base / length
        normal = np.array([-along[1], along[0]])
        height = float((a - mid) @ normal)
        if right == apex:                               # right isosceles: the apex sits half a base from it
            height = math.copysign(length / 2, height)
        new = {apex: mid + normal * height, (apex + 1) % 3: mid - along * length / 2,
               (apex - 1) % 3: mid + along * length / 2}
    else:
        v, a, b = corners[right], corners[(right + 1) % 3], corners[(right - 1) % 3]
        first = a - v
        l1, l2 = float(np.linalg.norm(first)), float(np.linalg.norm(b - v))
        u1 = first / l1
        u2 = np.array([-u1[1], u1[0]])
        if (b - v) @ u2 < 0:
            u2 = -u2
        new = {right: v, (right + 1) % 3: v + u1 * l1, (right - 1) % 3: v + u2 * l2}
    result = np.array([new[i] for i in range(3)])
    # Turn it onto the axes when the base or a leg was nearly horizontal or vertical.
    pivot = result.mean(axis=0)
    k = apex if apex is not None else right
    edge = result[(k + 2) % 3] - result[(k + 1) % 3] if apex is not None else result[(k + 1) % 3] - result[k]
    heading = math.degrees(math.atan2(edge[1], edge[0]))
    turn = snap_axis(heading) - heading
    if turn:
        c, s = math.cos(math.radians(turn)), math.sin(math.radians(turn))
        result = (result - pivot) @ np.array([[c, s], [-s, c]]) + pivot
    return {"kind": "triangle", "points": result, "nodes": 3}


def polygon_corners(pieces):
    if len(pieces) < 3 or any(p["kind"] != "line" for p in pieces):
        return None
    return np.array([p["p0"] for p in pieces], dtype=float)


# ---------------------------------------------------------------- geometry of a shape

def vertices(shape):
    kind = shape["kind"]
    if kind in ("rectangle", "square"):
        hw, hh = shape["w"] / 2, shape["h"] / 2
        local = np.array([[-hw, -hh], [hw, -hh], [hw, hh], [-hw, hh]])
        u = unit(shape["angle"])
        v = np.array([-u[1], u[0]])
        return shape["center"] + local[:, :1] * u + local[:, 1:] * v
    if kind == "polygon":
        angles = [shape["start"] + shape["sense"] * 360.0 * i / shape["sides"] for i in range(shape["sides"])]
        return np.array([shape["center"] + shape["radius"] * unit(a) for a in angles])
    return np.array(shape["points"])


def ellipse_axes(shape):
    rx, ry = (shape["radius"], shape["radius"]) if shape["kind"] == "circle" else (shape["rx"], shape["ry"])
    return rx, ry, (0.0 if shape["kind"] == "circle" else shape["angle"])


def outline(shape, spacing=0.4):
    """Points along the shape's edge, about `spacing` pixels apart."""
    if shape["kind"] in ("circle", "ellipse"):
        rx, ry, angle = ellipse_axes(shape)
        count = max(64, int(2 * math.pi * max(rx, ry) / spacing))
        t = np.linspace(0, 2 * math.pi, count, endpoint=False)
        u = unit(angle)
        v = np.array([-u[1], u[0]])
        return shape["center"] + np.outer(rx * np.cos(t), u) + np.outer(ry * np.sin(t), v)
    corners = vertices(shape)
    points = []
    for a, b in zip(corners, np.roll(corners, -1, axis=0)):
        count = max(2, int(np.linalg.norm(b - a) / spacing))
        points.extend(a + (b - a) * np.linspace(0, 1, count, endpoint=False)[:, None])
    return np.array(points)


def path_data(shape, f):
    """Path commands for use inside a compound path (a shape with holes, or a hole itself)."""
    if shape["kind"] in ("circle", "ellipse"):
        rx, ry, angle = ellipse_axes(shape)
        # Round first, then derive the end points: two half-arcs whose chord is not exactly 2r are
        # ill-conditioned (a 0.005 px slip moves the rendered centre by about a pixel).
        cx, cy, rx, ry = (round(float(v), 2) for v in (shape["center"][0], shape["center"][1], rx, ry))
        if angle == 0:
            arc = f"A{f(rx)} {f(ry)} 0 1 1 "
            return (f"M{f(cx + rx)} {f(cy)}{arc}{f(cx - rx)} {f(cy)}{arc}{f(cx + rx)} {f(cy)}Z")
        u = unit(angle)                                 # tilted: four well-conditioned quarter arcs
        v = np.array([-u[1], u[0]])
        stops = [np.array([cx, cy]) + rx * u, np.array([cx, cy]) + ry * v,
                 np.array([cx, cy]) - rx * u, np.array([cx, cy]) - ry * v]
        arc = f"A{f(rx)} {f(ry)} {f(angle)} 0 1 "
        return (f"M{f(stops[0][0])} {f(stops[0][1])}"
                + "".join(f"{arc}{f(p[0])} {f(p[1])}" for p in stops[1:] + stops[:1]) + "Z")
    corners = vertices(shape)
    return f"M{f(corners[0][0])} {f(corners[0][1])}" + "".join(f"L{f(p[0])} {f(p[1])}" for p in corners[1:]) + "Z"


def element(shape, color, f):
    """The matching SVG element for a shape that stands alone (no holes inside it)."""
    fill = f'fill="{color}"'
    kind = shape["kind"]
    if kind == "circle":
        c = shape["center"]
        return f'<circle cx="{f(c[0])}" cy="{f(c[1])}" r="{f(shape["radius"])}" {fill}/>'
    if kind == "ellipse":
        c = shape["center"]
        turn = f' transform="rotate({f(shape["angle"])} {f(c[0])} {f(c[1])})"' if shape["angle"] else ""
        return f'<ellipse cx="{f(c[0])}" cy="{f(c[1])}" rx="{f(shape["rx"])}" ry="{f(shape["ry"])}"{turn} {fill}/>'
    if kind in ("rectangle", "square") and shape["angle"] == 0:
        c = shape["center"]
        return (f'<rect x="{f(c[0] - shape["w"] / 2)}" y="{f(c[1] - shape["h"] / 2)}" '
                f'width="{f(shape["w"])}" height="{f(shape["h"])}" {fill}/>')
    points = " ".join(f"{f(p[0])},{f(p[1])}" for p in vertices(shape))
    return f'<polygon points="{points}" {fill}/>'


# ---------------------------------------------------------------- recognition

def recognise(pieces, loop, built, deviation, limit):
    """The exact shape for one closed contour, or None.

    `loop` is the traced outline and `built` the outline already fitted from `pieces`; a figure made from
    the pieces is checked against `built` (so a sharp tip the trace blunts is not held against it), a smooth
    figure fitted to the trace against the trace. `deviation(points, reference)` returns (worst, mean)
    distance and `limit` is the worst distance (pixels) a replacement may move.
    """
    candidates = []
    shape = circle_from_pieces(pieces)
    corners = polygon_corners(pieces)
    reference = built
    if shape is not None:
        candidates = [shape]
    elif corners is not None:
        n = len(corners)
        for make, ok in ((rectangle_shape, n == 4), (regular_shape, 3 <= n <= MAX_SIDES),
                         (triangle_shape, n == 3)):
            found = make(corners) if ok else None
            if found is not None:
                candidates = [found]
                break
    elif not any(p["kind"] == "line" for p in pieces):
        candidates, reference = ellipse_shapes(loop), loop      # curves only: a smooth closed figure
    smooth = reference is loop
    allowed = max(0.8, limit * 0.5) if smooth else limit        # smooth figures must hug the trace closer
    for shape in candidates:
        worst, _ = deviation(outline(shape), reference)
        if worst <= allowed:
            return shape
    return None
