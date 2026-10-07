"""Recognise circles and arcs in traced outlines and give them the constraints a designer would.

Used by fit_curves.py. Nothing here is tuned to a particular logo: every decision is a test
against measurements of the image itself, with tolerances relative to the feature's size.

- A run of outline is a circular arc when it stays close to a fitted circle. A clean arc
  ("strict") stands on its own; a noisier arc ("loose") is accepted only when it shares its
  centre with a clean one, because one arc alone is weak evidence but concentric arcs are a
  deliberate construction.
- Arcs whose centres agree share one exact centre, fitted jointly to all of them.
- Radii that agree are merged; so are the widths of bands (rings and ring segments).
- A straight end that passes through a shared centre becomes exactly radial, and radial
  ends lying on one line through the centre share one angle.
"""
import math

import numpy as np

STRICT = {"rms": (0.35, 0.003), "max": (1.0, 0.010), "sweep": 30.0}   # (pixels, fraction of radius)
LOOSE = {"rms": (0.35, 0.020), "max": (1.0, 0.040), "sweep": 60.0}
CENTER_TOLERANCE = (2.0, 0.03)     # centres closer than max(2 px, 3% of the smaller radius) may be shared
RADIUS_TOLERANCE = (0.75, 0.004)   # radii closer than this are one radius
WIDTH_TOLERANCE = (1.0, 0.02)      # band widths closer than this are one width
RADIAL_TOLERANCE = (1.5, 0.010)    # a straight end this close to a centre is radial
ANGLE_TOLERANCE = 1.0              # radial ends within this many degrees (mod 180) share one line
AXIS_TOLERANCE = 1.5               # radial lines this close to horizontal/vertical become exact


def limit(spec, radius):
    return max(spec[0], spec[1] * radius)


def fit_circle(points):
    """Geometric least-squares circle: returns (centre, radius, residuals)."""
    x, y = points[:, 0], points[:, 1]
    a = np.column_stack([2 * x, 2 * y, np.ones(len(x))])
    cx, cy, c = np.linalg.lstsq(a, x * x + y * y, rcond=None)[0]
    center, radius = np.array([cx, cy]), math.sqrt(max(c + cx * cx + cy * cy, 1e-12))
    for _ in range(30):
        d = points - center
        dist = np.maximum(np.hypot(d[:, 0], d[:, 1]), 1e-9)
        jac = np.column_stack([-d / dist[:, None], -np.ones(len(points))])
        step = np.linalg.lstsq(jac, -(dist - radius), rcond=None)[0]
        center, radius = center + step[:2], radius + step[2]
        if np.abs(step).max() < 1e-7:
            break
    d = points - center
    return center, radius, np.hypot(d[:, 0], d[:, 1]) - radius


def joint_fit(groups, noise=None):
    """One centre shared by several point sets, each with its own radius.

    `noise` gives each set's edge noise (rms, px); cleaner sets weigh more, as in any weighted fit.
    """
    fits = [fit_circle(g) for g in groups]
    noise = noise or [max(rms(f[2]), 0.05) for f in fits]
    weights = [1.0 / max(n, 0.05) ** 2 for n in noise]
    center = sum(w * f[0] for w, f in zip(weights, fits)) / sum(weights)
    radii = np.array([np.hypot(*(g - center).T).mean() for g in groups])
    owner = np.concatenate([np.full(len(g), i) for i, g in enumerate(groups)])
    points = np.vstack(groups)
    root = np.sqrt(np.array(weights)[owner])
    for _ in range(30):
        d = points - center
        dist = np.maximum(np.hypot(d[:, 0], d[:, 1]), 1e-9)
        jac = np.zeros((len(points), 2 + len(groups)))
        jac[:, :2] = -d / dist[:, None]
        jac[np.arange(len(points)), 2 + owner] = -1.0
        step = np.linalg.lstsq(jac * root[:, None], -(dist - radii[owner]) * root, rcond=None)[0]
        center, radii = center + step[:2], radii + step[2:]
        if np.abs(step).max() < 1e-7:
            break
    residuals = [np.hypot(*(g - center).T) - radii[i] for i, g in enumerate(groups)]
    return center, radii, residuals


def rms(values):
    return float(np.sqrt(np.mean(np.square(values))))


def passes(spec, radius, residuals, sweep):
    return (rms(residuals) <= limit(spec["rms"], radius) and float(np.abs(residuals).max()) <= limit(spec["max"], radius)
            and abs(sweep) >= spec["sweep"])


def sweep_degrees(points, center):
    angles = np.unwrap(np.arctan2(points[:, 1] - center[1], points[:, 0] - center[0]))
    return math.degrees(angles[-1] - angles[0])


def trimmed(points, closed, trim_px, step):
    """Drop the first and last few pixels of an open run: corners are rounded there by rasterisation."""
    if closed:
        return points
    k = int(round(trim_px / step))
    return points[k:len(points) - k] if len(points) > 2 * k + 8 else points


def classify(points, closed, max_radius, step):
    """Return a candidate arc record, or None when the run is not circular."""
    sample = trimmed(points, closed, 2.0, step)
    if len(sample) < 8:
        return None
    center, radius, residuals = fit_circle(sample)
    if not 1.0 < radius < max_radius:
        return None
    sweep = 360.0 if closed else sweep_degrees(points, center)
    grade = "strict" if passes(STRICT, radius, residuals, sweep) else \
        "loose" if passes(LOOSE, radius, residuals, sweep) else None
    if grade is None:
        return None
    return {"points": sample, "center": center, "radius": radius, "grade": grade, "closed": closed,
            "rms": rms(residuals), "sweep": sweep}


def cluster(arcs):
    """Group arcs that share a centre; refit each group jointly. Arcs are dicts from classify()."""
    order = sorted(range(len(arcs)), key=lambda i: (arcs[i]["grade"] != "strict", -len(arcs[i]["points"])))
    clusters = []
    for i in order:
        arc = arcs[i]
        placed = False
        for group in clusters:
            members = group["members"] + [i]
            if np.linalg.norm(arc["center"] - group["center"]) > limit(CENTER_TOLERANCE, arc["radius"]):
                continue                  # a centre is as uncertain as the arc measuring it is large
            center, radii, residuals = joint_fit([arcs[m]["points"] for m in members],
                                                 [arcs[m]["rms"] for m in members])
            ok = all(rms(res) <= limit((STRICT if arcs[m]["grade"] == "strict" else LOOSE)["rms"], r) * 1.5
                     for m, r, res in zip(members, radii, residuals))
            if ok:
                group.update(members=members, center=center, radii=list(radii))
                placed = True
                break
        if not placed:
            clusters.append({"members": [i], "center": arc["center"].copy(), "radii": [arc["radius"]]})
    accepted = []
    for group in clusters:
        if not any(arcs[m]["grade"] == "strict" for m in group["members"]):
            continue                      # noisy arcs with no clean arc to confirm them stay as curves
        for m, r in zip(group["members"], group["radii"]):
            arcs[m]["radius"], arcs[m]["center"] = float(r), group["center"]
        accepted.append(group)
    return accepted


def merge_values(values, weights, tolerances):
    """Merge values that lie within each other's tolerance to their weighted mean; returns new values.

    `tolerances` is one tolerance per value (a spec tuple applies the same rule to all of them).
    """
    if isinstance(tolerances, tuple):
        tolerances = [limit(tolerances, v) for v in values]
    order = sorted(range(len(values)), key=lambda i: values[i])
    result, run = list(values), [order[0]] if order else []
    for i in order[1:] + [None]:
        if i is not None and values[i] - values[run[-1]] <= max(tolerances[i], tolerances[run[-1]]):
            run.append(i)
            continue
        mean = sum(values[j] * weights[j] for j in run) / sum(weights[j] for j in run)
        for j in run:
            result[j] = mean
        run = [i] if i is not None else []
    return result


def precision(arc):
    """Weight of an arc's measurements: more points and a cleaner edge weigh more."""
    return len(arc["points"]) / max(arc["rms"], 0.05) ** 2


def constrain(arcs, clusters, bands):
    """Merge equal radii within each shared centre and equal band widths across the image.

    `bands` lists (inner arc indices, outer arc indices) pairs: the two edges of one ring or ring segment.
    Returns a report of what was merged.
    """
    report = {"merged_radii": 0, "band_widths": []}
    for group in clusters:
        members = group["members"]
        # Two radii closer than the edge noise of the arcs measuring them cannot be told apart: one radius.
        merged = merge_values([arcs[m]["radius"] for m in members], [precision(arcs[m]) for m in members],
                              [max(limit(RADIUS_TOLERANCE, arcs[m]["radius"]), arcs[m]["rms"]) for m in members])
        report["merged_radii"] += len(members) - len({round(r, 6) for r in merged})
        for m, r in zip(members, merged):
            arcs[m]["radius"] = r
    if not bands:
        return report
    for _ in range(8):                    # alternate: equal widths, then keep shared edges consistent
        widths = [arcs[outer[0]]["radius"] - arcs[inner[0]]["radius"] for inner, outer in bands]
        weights = [1 / (1 / precision(arcs[inner[0]]) + 1 / precision(arcs[outer[0]])) for inner, outer in bands]
        noise = [math.hypot(arcs[inner[0]]["rms"], arcs[outer[0]]["rms"]) for inner, outer in bands]
        target = merge_values(widths, weights, [max(limit(WIDTH_TOLERANCE, w), n) for w, n in zip(widths, noise)])
        proposals = {}
        for (inner, outer), width in zip(bands, target):
            middle = (arcs[outer[0]]["radius"] + arcs[inner[0]]["radius"]) / 2
            for m in inner:
                proposals.setdefault(m, []).append(middle - width / 2)
            for m in outer:
                proposals.setdefault(m, []).append(middle + width / 2)
        for m, values in proposals.items():
            arcs[m]["radius"] = float(np.mean(values))
    report["band_widths"] = sorted({round(arcs[o[0]]["radius"] - arcs[i[0]]["radius"], 2) for i, o in bands})
    return report


def snap_radial_angles(angles):
    """Radial lines (angles in degrees, mod 360) on one line through the centre share an angle; near-axis ones are exact."""
    if not angles:
        return []
    doubled = [math.radians(2 * a) for a in angles]          # work mod 180 so opposite ends match
    result = list(angles)
    used = [False] * len(angles)
    for i in range(len(angles)):
        if used[i]:
            continue
        group = [j for j in range(len(angles)) if not used[j] and
                 abs(math.degrees(math.atan2(math.sin(doubled[j] - doubled[i]), math.cos(doubled[j] - doubled[i])))) / 2
                 <= ANGLE_TOLERANCE]
        mean = math.degrees(math.atan2(np.mean([math.sin(doubled[j]) for j in group]),
                                       np.mean([math.cos(doubled[j]) for j in group]))) / 2
        for axis in (0.0, 90.0, 180.0, -90.0):
            if abs((mean - axis + 90) % 180 - 90) <= AXIS_TOLERANCE:
                mean = axis
        for j in group:
            used[j] = True
            line = mean % 180                                   # keep each end on its own side of the centre
            result[j] = line if abs((angles[j] - line + 180) % 360 - 180) <= 90 else line - 180
    return result


# ---------------------------------------------------------------- intersections

def line_line(p, u, q, v):
    cross = u[0] * v[1] - u[1] * v[0]
    if abs(cross) < math.sin(math.radians(4)):
        return []
    t = ((q[0] - p[0]) * v[1] - (q[1] - p[1]) * v[0]) / cross
    return [p + t * u]


def line_circle(p, u, c, r):
    d = p - c
    b, cc = float(d @ u), float(d @ d - r * r)
    disc = b * b - cc
    if disc < 0:
        return []
    root = math.sqrt(disc)
    return [p + (-b - root) * u, p + (-b + root) * u]


def circle_circle(c1, r1, c2, r2):
    d = float(np.linalg.norm(c2 - c1))
    if d < 1e-6 or d > r1 + r2 or d < abs(r1 - r2):
        return []
    a = (r1 * r1 - r2 * r2 + d * d) / (2 * d)
    h = math.sqrt(max(r1 * r1 - a * a, 0.0))
    mid = c1 + a * (c2 - c1) / d
    perp = np.array([-(c2 - c1)[1], (c2 - c1)[0]]) / d
    return [mid + h * perp, mid - h * perp]


def project(point, piece):
    if piece["kind"] == "line":
        p, u = piece["carrier"]
        return p + float((point - p) @ u) * u
    if piece["kind"] == "arc":
        c, r = piece["center"], piece["radius"]
        d = point - c
        n = np.linalg.norm(d)
        return c + d / n * r if n > 1e-9 else point
    return point


def meet(point, first, second, reach, tangent=False):
    """Where two pieces should meet near `point`.

    At a corner two lines or arcs meet at their exact intersection. At a smooth (tangent) join an
    intersection is ill-conditioned, so the join is the tangent point instead: the foot of the
    perpendicular from the arc's centre to the line, or the point on the line of centres for two arcs.
    """
    kinds = {first["kind"], second["kind"]}
    if tangent and kinds <= {"line", "arc"} and kinds != {"line"}:   # two lines always meet at their intersection
        if first["kind"] == second["kind"] == "arc":
            axis = second["center"] - first["center"]
            if np.linalg.norm(axis) < 1e-6:
                return project(point, first)
            u = axis / np.linalg.norm(axis)
            side = 1.0 if (point - first["center"]) @ u >= 0 else -1.0
            return first["center"] + side * first["radius"] * u
        line = first if first["kind"] == "line" else second
        arc = second if line is first else first
        foot = project(arc["center"], line)
        return foot if np.linalg.norm(foot - point) <= reach else project(point, line)
    if kinds <= {"line", "arc"}:
        if first["kind"] == second["kind"] == "line":
            options = line_line(*first["carrier"], *second["carrier"])
        elif first["kind"] == second["kind"] == "arc":
            if np.linalg.norm(first["center"] - second["center"]) < 1e-6 and abs(first["radius"] - second["radius"]) < 1e-6:
                return project(point, first)
            options = circle_circle(first["center"], first["radius"], second["center"], second["radius"])
        else:
            line, arc = (first, second) if first["kind"] == "line" else (second, first)
            options = line_circle(*line["carrier"], arc["center"], arc["radius"])
        if options:
            best = min(options, key=lambda q: np.linalg.norm(q - point))
            if np.linalg.norm(best - point) <= reach:
                return best
        for piece in (first, second):              # no clean intersection: stay exactly on the line
            if piece["kind"] == "line":
                return project(point, piece)
        return project(point, first)
    if first["kind"] in ("line", "arc"):
        return project(point, first)
    if second["kind"] in ("line", "arc"):
        return project(point, second)
    return point


def arc_flags(piece):
    """SVG large-arc and sweep flags for an arc piece (y-down: sweep 1 is clockwise on screen)."""
    c = piece["center"]
    a0 = math.atan2(piece["p0"][1] - c[1], piece["p0"][0] - c[0])
    a1 = math.atan2(piece["p1"][1] - c[1], piece["p1"][0] - c[0])
    span = (a1 - a0) % (2 * math.pi) if piece["sweep"] > 0 else (a0 - a1) % (2 * math.pi)
    return int(span > math.pi), int(piece["sweep"] > 0), span


def arc_points(piece, spacing=0.4):
    large, sweep, span = arc_flags(piece)
    c, r = piece["center"], piece["radius"]
    a0 = math.atan2(piece["p0"][1] - c[1], piece["p0"][0] - c[0])
    count = max(8, int(span * r / spacing))
    angles = a0 + (1 if sweep else -1) * np.linspace(0, span, count)
    return np.column_stack([c[0] + r * np.cos(angles), c[1] + r * np.sin(angles)])
