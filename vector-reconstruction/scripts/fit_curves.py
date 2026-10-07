#!/usr/bin/env python3
"""Rebuild a flat-color image (logo, wordmark, mark) as the outline a designer would draw.

extract_contours.py traces pixel edges, so its draft is a polyline that follows every bump of
a rough or low-resolution edge. This script takes the same sub-pixel contours and rebuilds
every edge as the simplest true primitive the measurements support:

- edge noise is smoothed away and real corners stay sharp;
- straight edges become exact lines (near-horizontal/vertical ones snapped to the axis);
- circular edges become exact SVG arcs; arcs whose centres agree share one exact centre,
  radii and ring widths that agree are merged, and straight ends through a shared centre
  become radial (see geometry.py; every rule is a test against the image's own measurements);
- arcs that run into straight edges are made tangent to them (rounded corners, round caps);
- everything else becomes few cubic Beziers anchored at horizontal/vertical extrema, with
  smooth joins made exactly smooth, refitted after the constraints are applied.

Requires Pillow and numpy. The result is a starting point for rebuilding, not a replacement
for looking: compare it with the source, then redraw anything it got wrong.
"""
import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import extract_contours as ec  # noqa: E402
import geometry as geo  # noqa: E402
import shapes as shp  # noqa: E402

STEP = 0.5  # arc-length spacing, in pixels, of the resampled contour


# ---------------------------------------------------------------- contour preparation

def resample(points, step=STEP):
    """Resample a closed loop at uniform arc length."""
    loop = np.vstack([points, points[:1]])
    seg = np.linalg.norm(np.diff(loop, axis=0), axis=1)
    arc = np.r_[0.0, np.cumsum(seg)]
    count = max(16, int(round(arc[-1] / step)))
    target = np.linspace(0, arc[-1], count, endpoint=False)
    return np.column_stack([np.interp(target, arc, loop[:, 0]), np.interp(target, arc, loop[:, 1])])


def kernel(sigma_px, limit):
    """Gaussian weights for a smoothing width in pixels, with at most `limit` samples on each side."""
    sigma = max(sigma_px, 1e-3) / STEP
    radius = max(0, min(int(math.ceil(3 * sigma)), limit))
    weights = np.exp(-0.5 * (np.arange(-radius, radius + 1) / sigma) ** 2)
    return weights / weights.sum(), radius


def smooth_closed(points, sigma_px):
    weights, radius = kernel(sigma_px, len(points) // 2 - 1)
    padded = np.pad(points, ((radius, radius), (0, 0)), mode="wrap")
    return np.column_stack([np.convolve(padded[:, axis], weights, mode="valid") for axis in (0, 1)])


def smooth_open(run, sigma_px):
    """Smooth an open run while keeping both ends exactly where they are (odd reflection)."""
    weights, radius = kernel(sigma_px, len(run) - 2)
    if radius < 1:
        return run.copy()
    left = 2 * run[0] - run[1:radius + 1][::-1]
    right = 2 * run[-1] - run[-radius - 1:-1][::-1]
    padded = np.vstack([left, run, right])
    return np.column_stack([np.convolve(padded[:, axis], weights, mode="valid") for axis in (0, 1)])


def unit(vector):
    length = np.linalg.norm(vector)
    return vector / length if length > 1e-12 else np.array([1.0, 0.0])


def find_corners(loop, window_px, angle_deg, light_sigma=1.0):
    """Indices where the contour really turns (not just edge noise), as local maxima of the turning angle."""
    count = len(loop)
    q = smooth_closed(loop, light_sigma)
    k = max(2, int(round(window_px / STEP)))
    before = q - np.roll(q, k, axis=0)
    after = np.roll(q, -k, axis=0) - q
    cross = before[:, 0] * after[:, 1] - before[:, 1] * after[:, 0]
    dot = (before * after).sum(axis=1)
    angle = np.degrees(np.abs(np.arctan2(cross, dot)))
    corners = []
    for index in np.argsort(-angle):
        if angle[index] < angle_deg:
            break
        if all(min((index - c) % count, (c - index) % count) > k for c in corners):
            corners.append(int(index))
    return sorted(corners)


# ---------------------------------------------------------------- Bezier fitting (Schneider)

def bezier(ctrl, t):
    t = t[:, None]
    mt = 1 - t
    return mt ** 3 * ctrl[0] + 3 * mt ** 2 * t * ctrl[1] + 3 * mt * t ** 2 * ctrl[2] + t ** 3 * ctrl[3]


def bezier_d1(ctrl, t):
    t = t[:, None]
    mt = 1 - t
    return 3 * (mt ** 2 * (ctrl[1] - ctrl[0]) + 2 * mt * t * (ctrl[2] - ctrl[1]) + t ** 2 * (ctrl[3] - ctrl[2]))


def bezier_d2(ctrl, t):
    t = t[:, None]
    return 6 * ((1 - t) * (ctrl[2] - 2 * ctrl[1] + ctrl[0]) + t * (ctrl[3] - 2 * ctrl[2] + ctrl[1]))


def chord_params(points):
    arc = np.r_[0.0, np.cumsum(np.linalg.norm(np.diff(points, axis=0), axis=1))]
    return arc / arc[-1] if arc[-1] > 0 else np.linspace(0, 1, len(points))


def generate(points, u, t_left, t_right):
    p0, p3 = points[0], points[-1]
    b0, b1, b2, b3 = (1 - u) ** 3, 3 * u * (1 - u) ** 2, 3 * u ** 2 * (1 - u), u ** 3
    a1, a2 = b1[:, None] * t_left, b2[:, None] * t_right
    rest = points - (b0 + b1)[:, None] * p0 - (b2 + b3)[:, None] * p3
    c00, c01, c11 = (a1 * a1).sum(), (a1 * a2).sum(), (a2 * a2).sum()
    x0, x1 = (a1 * rest).sum(), (a2 * rest).sum()
    det = c00 * c11 - c01 * c01
    span = np.linalg.norm(p3 - p0)
    alpha1 = alpha2 = span / 3
    if abs(det) > 1e-12:
        alpha1, alpha2 = (x0 * c11 - x1 * c01) / det, (c00 * x1 - c01 * x0) / det
    if alpha1 < 1e-6 * span or alpha2 < 1e-6 * span:
        alpha1 = alpha2 = span / 3
    return np.array([p0, p0 + t_left * alpha1, p3 + t_right * alpha2, p3])


def worst_error(points, ctrl, u):
    distance = np.linalg.norm(bezier(ctrl, u) - points, axis=1)
    index = int(np.argmax(distance[1:-1])) + 1 if len(points) > 2 else 0
    return float(distance.max()), index


def reparametrize(points, ctrl, u):
    q, q1, q2 = bezier(ctrl, u), bezier_d1(ctrl, u), bezier_d2(ctrl, u)
    numerator = ((q - points) * q1).sum(axis=1)
    denominator = (q1 * q1).sum(axis=1) + ((q - points) * q2).sum(axis=1)
    step = np.where(np.abs(denominator) > 1e-12, numerator / np.where(denominator == 0, 1, denominator), 0)
    return np.clip(u - step, 0, 1)


def fit_cubic(points, t_left, t_right, error, lo=0, depth=0):
    """Fit one or more cubics; t_left points forward from the start, t_right points back from the end.

    Returns a list of (control points, first index, last index) into `points` (offset by `lo`).
    """
    last = lo + len(points) - 1
    if len(points) < 3:
        span = np.linalg.norm(points[-1] - points[0])
        return [(np.array([points[0], points[0] + t_left * span / 3, points[-1] + t_right * span / 3, points[-1]]),
                 lo, last)]
    u = chord_params(points)
    ctrl = generate(points, u, t_left, t_right)
    worst, split = worst_error(points, ctrl, u)
    if worst <= error:
        return [(ctrl, lo, last)]
    for _ in range(12):
        u = reparametrize(points, ctrl, u)
        ctrl = generate(points, u, t_left, t_right)
        worst, split = worst_error(points, ctrl, u)
        if worst <= error:
            return [(ctrl, lo, last)]
    if depth > 12 or len(points) < 6:
        return [(ctrl, lo, last)]
    split = min(max(split, 2), len(points) - 3)
    center = unit(points[split - 1] - points[split + 1])
    return (fit_cubic(points[:split + 1], t_left, center, error, lo, depth + 1) +
            fit_cubic(points[split:], -center, t_right, error, lo + split, depth + 1))


def merge_segments(points, parts, error):
    """Join neighbouring cubics of one piece when a single cubic through both stays within the tolerance."""
    parts = list(parts)
    merged = True
    while merged and len(parts) > 1:
        merged = False
        for i in range(len(parts) - 1):
            (c1, lo1, _), (c2, _, hi2) = parts[i], parts[i + 1]
            joined = fit_cubic(points[lo1:hi2 + 1], unit(c1[1] - c1[0]), unit(c2[2] - c2[3]), error, lo1)
            if len(joined) == 1:
                parts[i:i + 2] = joined
                merged = True
                break
    return parts


# ---------------------------------------------------------------- splitting runs into pieces

def is_straight(points, tolerance, ratio=0.01):
    """True when the run hugs its chord. Longer runs may wander a little more (`ratio` of their length)."""
    chord = points[-1] - points[0]
    length = np.linalg.norm(chord)
    if length < 1e-9:
        return False
    offset = np.abs(chord[0] * (points[:, 1] - points[0, 1]) - chord[1] * (points[:, 0] - points[0, 0])) / length
    return float(offset.max()) <= max(tolerance, ratio * length)


def heading_change(points, index, reach):
    """Angle (degrees) the direction of travel turns between `reach` samples before and after `index`."""
    lo, hi = max(index - reach, 0), min(index + reach, len(points) - 1)
    before, after = points[index] - points[lo], points[hi] - points[index]
    return abs(math.degrees(math.atan2(before[0] * after[1] - before[1] * after[0], float(before @ after))))


def axis_extrema(run, closed, sigma_px, min_gap_px, min_turn_deg):
    """Horizontal/vertical tangent positions as (index, 'h' or 'v'), keeping only real ones, not noise."""
    count, gap = len(run), int(round(min_gap_px / STEP))
    reach = max(4, min(int(round(60 / STEP)), count // 4))   # look 60 px (or a quarter of the run) either side
    smooth = smooth_closed(run, sigma_px) if closed else smooth_open(run, sigma_px)
    slope = (np.roll(smooth, -1, axis=0) - np.roll(smooth, 1, axis=0)) / 2 if closed else np.gradient(smooth, axis=0)
    wrapped = np.vstack([smooth, smooth, smooth]) if closed else smooth
    offset = count if closed else 0
    found = []
    for axis, name in ((0, "v"), (1, "h")):   # x extremum -> vertical tangent; y extremum -> horizontal tangent
        sign = np.sign(slope[:, axis])
        for i in range(count if closed else count - 1):
            j = (i + 1) % count
            if sign[i] * sign[j] >= 0:
                continue
            index = i if abs(slope[i, axis]) <= abs(slope[j, axis]) else j
            if not closed and not gap <= index <= count - 1 - gap:
                continue
            if heading_change(wrapped, index + offset, reach) >= min_turn_deg:
                found.append((index, name))
    found.sort()
    kept = []
    for index, name in found:
        if all(min(abs(index - other), count - abs(index - other)) >= gap for other, _ in kept):
            kept.append((index, name))
    return kept


def travel_direction(run, index, name, reach):
    lo, hi = max(index - reach, 0), min(index + reach, len(run) - 1)
    axis = 1 if name == "v" else 0   # vertical tangent travels along y; horizontal along x
    sign = 1.0 if run[hi, axis] - run[lo, axis] >= 0 else -1.0
    return np.array([0.0, sign]) if name == "v" else np.array([sign, 0.0])


def straight_run(points, args):
    return is_straight(points, args.line_tolerance, args.line_ratio) and \
        heading_change(points, len(points) // 2, len(points) // 2) < 12


def fit_line(points, axis_tolerance=2.0):
    """Line through the middle of a straight stretch: (point, unit direction), snapped to an axis when close."""
    middle = points.mean(axis=0)
    _, vectors = np.linalg.eigh(np.cov((points - middle).T))
    direction = vectors[:, -1]
    if direction @ (points[-1] - points[0]) < 0:
        direction = -direction
    angle = math.degrees(math.atan2(direction[1], direction[0]))
    for axis in (0, 90, 180, -90, -180):
        if abs(angle - axis) <= axis_tolerance:
            direction = np.array([round(math.cos(math.radians(axis))), round(math.sin(math.radians(axis)))], float)
    return middle, direction


def fit_run(run, closed, start_corner, end_corner, args):
    """Fit one smoothed run (between two corners, or a whole smooth loop) into segments.

    Returns (start point, segments); a segment is ("L", end, line or None) or ("C", control 1, control 2, end).
    A line from a straight stretch of edge carries the line fitted through that stretch.
    """
    count = len(run)
    reach = max(2, int(round(3.0 / STEP)))
    extrema = [] if (not closed and is_straight(run, args.line_tolerance, args.line_ratio)) else \
        axis_extrema(run, closed, args.smooth * 2, args.min_gap, args.min_turn)
    if closed:
        if len(extrema) < 2:                              # a smooth loop needs two anchors to fit
            extrema = [(0, None), (count // 2, None)]
        anchors = extrema
        pieces = [(anchors[i], anchors[(i + 1) % len(anchors)]) for i in range(len(anchors))]
    else:
        anchors = [(0, None)] + [(i, n) for i, n in extrema if 0 < i < count - 1] + [(count - 1, None)]
        pieces = [(anchors[i], anchors[i + 1]) for i in range(len(anchors) - 1)]
    segments = []
    for (a, a_axis), (b, b_axis) in pieces:
        idx = np.arange(a, b + 1) if b > a else np.r_[np.arange(a, count), np.arange(0, b + 1)]
        piece = run[idx % count]
        if not closed and a == 0 and b == count - 1:
            piece = run
        if len(piece) < 2:
            continue
        if straight_run(piece, args):
            segments.append(("L", piece[-1], fit_line(piece)))
            continue
        k = int(min(max(0.1 * len(piece), reach), 40, max(1, len(piece) // 3)))   # tangent reach: 10% of the piece
        t_left = travel_direction(run, a, a_axis, reach) if a_axis else unit(piece[min(k, len(piece) - 1)] - piece[0])
        t_right = -travel_direction(run, b, b_axis, reach) if b_axis else unit(piece[max(len(piece) - 1 - k, 0)] - piece[-1])
        parts = merge_segments(piece, fit_cubic(piece, t_left, t_right, args.tolerance), args.tolerance)
        for ctrl, _, _ in parts:
            chord = ctrl[3] - ctrl[0]
            length = np.linalg.norm(chord)
            handles = np.abs(chord[0] * (ctrl[1:3, 1] - ctrl[0, 1]) - chord[1] * (ctrl[1:3, 0] - ctrl[0, 0])) / max(length, 1e-9)
            if length > 1e-9 and handles.max() <= 0.5:    # handles lie on the chord: it is a straight line
                segments.append(("L", ctrl[3], None))
            else:
                segments.append(("C", ctrl[1], ctrl[2], ctrl[3]))
    return run[anchors[0][0]].copy(), segments


# ---------------------------------------------------------------- whole contour

def diagonal_of(points):
    return float(np.linalg.norm(points.max(axis=0) - points.min(axis=0)))


def split_runs(points, args):
    """Resample a traced loop and cut it at real corners into runs (smoothed, ends kept exactly)."""
    loop = resample(np.array(points, dtype=float))
    tolerance = args.tolerance
    if tolerance is None:          # scale with the shape: 0.4% of its diagonal, between 0.6 and 1.5 px
        diagonal = float(np.linalg.norm(loop.max(axis=0) - loop.min(axis=0)))
        tolerance = min(1.5, max(0.6, 0.004 * diagonal))
    corners = find_corners(loop, args.corner_window, args.corner_angle)
    count = len(loop)
    if not corners:
        smooth = smooth_closed(loop, args.smooth)
        circular = geo.classify(smooth, True, 2.0 * diagonal_of(loop), STEP) is not None
        spans = [] if circular else straight_spans(smooth, True, args, tolerance)
        if not spans:
            return loop, tolerance, [{"points": smooth, "closed": True, "straight": False, "join": "tangent"}]
        start = spans[0][0]                          # start the loop where a straight stretch begins
        rotated = np.r_[smooth[start:], smooth[:start + 1]]
        shifted = [((a - start) % count, (b - start) % count) for a, b in spans]
        return loop, tolerance, split_at_spans(rotated, shifted, args, "tangent")
    runs = []
    for position, a in enumerate(corners):
        b = corners[(position + 1) % len(corners)]
        idx = np.arange(a, b + 1) if b > a else np.r_[np.arange(a, count), np.arange(0, b + 1)]
        if len(corners) == 1:
            idx = np.r_[np.arange(a, count), np.arange(0, a + 1)]
        run = smooth_open(loop[idx % count], args.smooth)
        if straight_run(run, args):
            runs.append({"points": run, "closed": False, "straight": True, "join": "corner"})
        elif geo.classify(run, False, 2.0 * diagonal_of(loop), STEP) is not None:
            runs.append({"points": run, "closed": False, "straight": False, "join": "corner"})   # an arc: keep whole
        else:
            runs += split_at_spans(run, straight_spans(run, False, args, tolerance), args, "corner")
    return loop, tolerance, runs


def straight_spans(points, closed, args, tolerance):
    """(first, last) index pairs of straight stretches inside a smooth run, e.g. the sides of a rounded rectangle.

    A stretch counts only by contrast: it must be straight within the line tolerance, long enough to matter,
    and the rest of the run must turn clearly more sharply. A large arc has the same gentle curvature
    everywhere, so no part of it is mistaken for a line.
    """
    count = len(points)
    reach = max(2, int(round(4.0 / STEP)))
    min_len = int(round(max(12.0, 8 * tolerance) / STEP))
    if count < 2 * min_len:
        return []
    padded = np.vstack([points, points, points]) if closed else points
    offset = count if closed else 0
    turn = np.array([heading_change(padded, i + offset, reach) for i in range(count)])
    flat = turn < 1.5
    if flat.all() or not flat.any():
        return []
    start = int(np.argmin(flat)) if closed else 0          # begin scanning at a curved sample
    order = [(start + k) % count for k in range(count)] if closed else list(range(count))
    candidates, i = [], 0
    while i < len(order):
        if not flat[order[i]]:
            i += 1
            continue
        j = i
        while j + 1 < len(order) and flat[order[j + 1]]:
            j += 1
        lo, hi = i - reach, j + reach                      # the turn test trims `reach` at both ends
        if not closed:
            lo, hi = max(lo, 0), min(hi, len(order) - 1)
        if hi - lo + 1 >= min_len:
            idx = [order[k % len(order)] for k in range(lo, hi + 1)]
            if is_straight(points[idx], args.line_tolerance, 0.004):
                candidates.append(idx)
        i = j + 1
    spans = []
    for idx in candidates:
        # The edge must turn sharply right after the stretch at each end (as at a rounded corner), not
        # merely somewhere else in the run: an O's sides or a bowl's flank curve away gradually.
        needed = max(6.0, 5 * float(np.mean(turn[idx])))
        ok = True
        for end, step in ((idx[0], -1), (idx[-1], 1)):
            if not closed and (end <= reach or end >= count - 1 - reach):
                continue                                   # the run's own end is a corner
            near = [(end + step * k) % count if closed else end + step * k for k in range(reach, min_len)]
            near = [k for k in near if 0 <= k < count]
            if not near or max(turn[k] for k in near) < needed:
                ok = False
        if ok:
            spans.append((idx[0], idx[-1]))
    spans = merge_spans(points, sorted(spans), closed, args)
    if not closed:
        spans = [(a, b) for a, b in spans if not (a == 0 and b == count - 1)]
    return spans


def span_indices(a, b, count):
    return list(range(a, b + 1)) if b >= a else list(range(a, count)) + list(range(0, b + 1))


def merge_spans(points, spans, closed, args, min_bend=15.0):
    """Straight stretches separated by a bend of less than `min_bend` degrees are not separate lines:
    the bend is edge noise. Join them when the whole stretch is straight; otherwise it is a gentle curve."""
    count = len(points)
    changed = True
    while changed and len(spans) > 1:
        changed = False
        pairs = len(spans) if closed else len(spans) - 1
        for k in range(pairs):
            (a1, b1), (a2, b2) = spans[k], spans[(k + 1) % len(spans)]
            u1 = fit_line(points[span_indices(a1, b1, count)], 0)[1]
            u2 = fit_line(points[span_indices(a2, b2, count)], 0)[1]
            bend = math.degrees(math.acos(max(-1.0, min(1.0, float(u1 @ u2)))))
            if bend >= min_bend:
                continue
            union = (a1, b2)
            if is_straight(points[span_indices(a1, b2, count)], args.line_tolerance, 0.004):
                spans[k:k + 2] = [union] if k + 1 < len(spans) else [union]
                if k + 1 >= len(spans):           # wrapped pair in a closed loop
                    spans = [union] + spans[1:-1]
            else:
                spans = [sp for j, sp in enumerate(spans) if j not in (k, (k + 1) % len(spans))]
            changed = True
            break
    return sorted(spans)


def split_at_spans(run, spans, args, last_join):
    """Cut an open run into straight and curved sub-runs; joins inside it are tangent, the last keeps `last_join`."""
    cuts, kinds = [0], []
    for a, b in spans:
        if a > cuts[-1]:
            cuts.append(a)
            kinds.append(False)
        cuts.append(b)
        kinds.append(True)
    if cuts[-1] < len(run) - 1:
        cuts.append(len(run) - 1)
        kinds.append(False)
    pieces = []
    for (a, b), straight in zip(zip(cuts, cuts[1:]), kinds):
        if b - a < 2:
            continue
        pieces.append({"points": run[a:b + 1], "closed": False, "straight": straight, "join": "tangent"})
    if not pieces:
        return [{"points": run, "closed": False, "straight": straight_run(run, args), "join": last_join}]
    pieces[-1]["join"] = last_join
    return pieces


def run_pieces(run, args, tolerance):
    """Pieces for one run: an arc, a line, or the curves and lines fit_run produces."""
    points = run["points"]
    arc = run.get("arc")
    if arc is not None:
        center, radius = arc["center"], arc["radius"]
        sign = 1.0 if geo.sweep_degrees(points, center) > 0 else -1.0
        if run["closed"]:                 # anchors at the four extremes, like a designer's circle
            quarter = [center + radius * np.array([math.cos(a), math.sin(a)])
                       for a in (np.arange(4) * math.pi / 2 * sign)]
            return [{"kind": "arc", "p0": quarter[i], "p1": quarter[(i + 1) % 4], "center": center,
                     "radius": radius, "sweep": sign, "join": "tangent"} for i in range(4)]
        return [{"kind": "arc", "p0": points[0].copy(), "p1": points[-1].copy(),
                 "center": center, "radius": radius, "sweep": sign, "join": run["join"], "arc": arc}]
    if run["straight"]:
        return [{"kind": "line", "p0": points[0].copy(), "p1": points[-1].copy(), "join": run["join"],
                 "carrier": run["carrier"] if run.get("carrier") is not None else fit_line(points)}]
    local = argparse.Namespace(**{**vars(args), "tolerance": tolerance})
    start, segments = fit_run(points, run["closed"], None, None, local)
    pieces, current = [], np.array(start, dtype=float)
    cursor = int(np.argmin(np.linalg.norm(points - current, axis=1)))
    for segment in segments:
        end = np.array(segment[1] if segment[0] == "L" else segment[3], dtype=float)
        order = [(cursor + k) % len(points) for k in range(len(points))] if run["closed"] else \
            list(range(cursor, len(points)))
        hit = order[int(np.argmin(np.linalg.norm(points[order] - end, axis=1)))]
        span = order[:order.index(hit) + 1]
        if segment[0] == "L":
            carrier = segment[2] if segment[2] is not None else fit_line(np.array([current, end]))
            pieces.append({"kind": "line", "p0": current, "p1": end, "carrier": carrier, "join": "tangent"})
        else:
            pieces.append({"kind": "curve", "p0": current, "h0": np.array(segment[1]), "h1": np.array(segment[2]),
                           "p1": end, "join": "tangent", "source": points[span], "tolerance": tolerance})
        cursor = hit                                  # every piece, line or curve, consumes its stretch
        current = end
    if pieces:
        pieces[-1]["join"] = run["join"]
    return pieces


def merge_coarcs(pieces):
    """Consecutive arcs on the same circle (split by a spurious corner) are one arc."""
    i = 0
    while i < len(pieces) and len(pieces) > 2:
        a, b = pieces[i], pieces[(i + 1) % len(pieces)]
        same = (a["kind"] == b["kind"] == "arc" and a["join"] == "corner" and a["sweep"] == b["sweep"]
                and abs(a["radius"] - b["radius"]) < 1e-6
                and np.linalg.norm(a["center"] - b["center"]) < 1e-6 and b is not a)
        if same and geo.arc_flags(a)[2] + geo.arc_flags(b)[2] < 2 * math.pi - 0.1:
            joined = {**a, "p1": b["p1"], "join": b["join"]}
            pieces = pieces[:i] + [joined] + pieces[i + 2:] if i + 1 < len(pieces) else [joined] + pieces[1:-1]
            continue
        i += 1
    return pieces


def fillet(pieces):
    """Make an arc that runs into straight edges exactly tangent to them, as a designer constructs it.

    An edge counts when the join is smooth, or when the edge nearly touches the arc (within
    max(1 px, 25% of the radius)) even if a corner was detected there. With one such edge the centre
    slides to exactly one radius from it; with two crossing edges it is a rounded corner (the centre
    is where the edges, offset by the radius, cross); with two parallel edges it is a round cap (the
    radius is half the gap and the centre lies on the midline). Arcs whose centre is shared with other
    arcs keep it: concentric construction takes priority.
    """
    pieces = merge_coarcs(pieces)
    count = len(pieces)
    if count < 2:
        return pieces
    for i, piece in enumerate(pieces):
        if piece["kind"] != "arc" or piece.get("arc", {}).get("shared", True):
            continue
        before, after = pieces[i - 1], pieces[(i + 1) % count]
        r, center = piece["radius"], piece["center"]
        edges = []
        for line, owner in ((before, before), (after, piece)):   # owner carries the join flag of that junction
            if line["kind"] != "line" or line is piece:
                continue
            p, u = line["carrier"]
            normal = np.array([-u[1], u[0]])
            gap = float((center - p) @ normal)
            if owner["join"] == "tangent" or abs(abs(gap) - r) <= max(1.0, 0.25 * r):
                edges.append((line, owner, p, u, normal * (1.0 if gap > 0 else -1.0)))
        if not edges:
            continue
        new_center, new_radius = None, r
        if len(edges) == 2:
            (_, _, p1, u1, n1), (_, _, p2, u2, n2) = edges
            if abs(u1[0] * u2[1] - u1[1] * u2[0]) < math.sin(math.radians(4)):   # parallel: a round cap
                width = abs(float((p2 - p1) @ n1))
                if n1 @ n2 < 0 and abs(width / 2 - r) <= max(1.0, 0.25 * r):
                    new_radius = width / 2
                    middle = p1 - n1 * width / 2 if (p2 - p1) @ n1 < 0 else p1 + n1 * width / 2
                    new_center = middle + float((center - middle) @ u1) * u1
            else:
                meet = geo.line_line(p1 + n1 * r, u1, p2 + n2 * r, u2)
                if meet and np.linalg.norm(meet[0] - center) <= max(2.0, 0.1 * r):
                    new_center = meet[0]
        if new_center is None:
            _, _, p, u, n = edges[0]
            moved = center + float(((p + n * r) - center) @ n) * n
            if np.linalg.norm(moved - center) <= max(2.0, 0.1 * r):
                new_center = moved
        if new_center is not None:
            piece["center"], piece["radius"] = new_center, new_radius
            for _, owner, *_ in edges:
                owner["join"] = "tangent"
    return pieces


def merge_collinear(pieces, max_bend=3.0):
    """Consecutive lines that continue each other (bend under `max_bend` degrees) are one line."""
    changed = True
    while changed and len(pieces) > 2:
        changed = False
        for i in range(len(pieces)):
            a, b = pieces[i], pieces[(i + 1) % len(pieces)]
            if a["kind"] != "line" or b["kind"] != "line" or b is a:
                continue
            u, v = unit(a["p1"] - a["p0"]), unit(b["p1"] - b["p0"])
            if math.degrees(math.acos(max(-1.0, min(1.0, float(u @ v))))) >= max_bend:
                continue
            joined = {"kind": "line", "p0": a["p0"], "p1": b["p1"], "join": b["join"],
                      "carrier": fit_line(np.array([a["p0"], a["p1"], b["p0"], b["p1"]]))}
            if i + 1 < len(pieces):
                pieces = pieces[:i] + [joined] + pieces[i + 2:]
            else:
                pieces = [joined] + pieces[1:-1]
            changed = True
            break
    return pieces


def resolve(pieces, reach):
    """Move every junction to where its two neighbours should meet; curve handles follow their anchors."""
    pieces = merge_collinear(pieces)
    count = len(pieces)
    for i in range(count):
        first, second = pieces[i], pieces[(i + 1) % count]
        old = np.array(first["p1"], dtype=float)
        new = geo.meet(old, first, second, reach, tangent=first["join"] == "tangent")
        delta = new - old
        first["p1"], second["p0"] = new.copy(), new.copy()
        if first["kind"] == "curve":
            first["h1"] = first["h1"] + delta
        if second["kind"] == "curve":
            second["h0"] = second["h0"] + delta
        if first["join"] == "tangent":
            align(first, second)
    return pieces


def refit_curves(pieces):
    """Refit every curve to its traced points with the end points and end tangents the constraints fixed."""
    result = []
    for piece in pieces:
        if piece["kind"] != "curve" or piece.get("source") is None or len(piece["source"]) < 4:
            result.append(piece)
            continue
        points = np.vstack([piece["p0"], piece["source"][1:-1], piece["p1"]])
        t_left, t_right = unit(piece["h0"] - piece["p0"]), unit(piece["h1"] - piece["p1"])
        parts = merge_segments(points, fit_cubic(points, t_left, t_right, piece["tolerance"]), piece["tolerance"])
        for k, (ctrl, _, _) in enumerate(parts):
            result.append({"kind": "curve", "p0": ctrl[0], "h0": ctrl[1], "h1": ctrl[2], "p1": ctrl[3],
                           "join": piece["join"] if k == len(parts) - 1 else "tangent"})
    return result


def tangent_at(piece, point, outgoing):
    """Unit direction of travel of a line or arc at `point`."""
    if piece["kind"] == "line":
        u = piece["carrier"][1]
        return u if u @ (piece["p1"] - piece["p0"]) >= 0 else -u
    d = point - piece["center"]
    t = np.array([-d[1], d[0]]) * piece["sweep"]
    return unit(t)


def align(first, second):
    """Make a smooth join exactly smooth: turn the curve handle(s) at the join onto one tangent."""
    point = first["p1"]
    if first["kind"] != "curve" and second["kind"] != "curve":
        return
    if first["kind"] != "curve":
        direction = tangent_at(first, point, False)
    elif second["kind"] != "curve":
        direction = tangent_at(second, point, True)
    else:
        a, b = point - first["h1"], second["h0"] - point
        if np.linalg.norm(a) < 1e-9 or np.linalg.norm(b) < 1e-9 or unit(a) @ unit(b) < math.cos(math.radians(12)):
            return                                   # a real kink, not an almost-smooth join
        direction = unit(unit(a) + unit(b))
    if first["kind"] == "curve":
        length = np.linalg.norm(point - first["h1"])
        if (point - first["h1"]) @ direction > 0:
            first["h1"] = point - direction * length
    if second["kind"] == "curve":
        length = np.linalg.norm(second["h0"] - point)
        if (second["h0"] - point) @ direction > 0:
            second["h0"] = point + direction * length


def path_data(pieces):
    f = lambda v: ec.number(round(float(v), 2))  # noqa: E731
    start = pieces[0]["p0"]
    parts = [f"M{f(start[0])} {f(start[1])}"]
    for piece in pieces:
        end = piece["p1"]
        if piece["kind"] == "line":
            parts.append(f"L{f(end[0])} {f(end[1])}")
        elif piece["kind"] == "arc":
            large, sweep, _ = geo.arc_flags(piece)
            r = f(piece["radius"])
            parts.append(f"A{r} {r} 0 {large} {sweep} {f(end[0])} {f(end[1])}")
        else:
            parts.append("C" + " ".join(f"{f(p[0])} {f(p[1])}" for p in (piece["h0"], piece["h1"], end)))
    return "".join(parts) + "Z"


def sample_path(pieces, spacing=0.4):
    """Points along the outline about `spacing` pixels apart, for measuring how far it strays."""
    points = [np.array(pieces[0]["p0"], dtype=float)]
    for piece in pieces:
        p0, p1 = np.array(piece["p0"], dtype=float), np.array(piece["p1"], dtype=float)
        if piece["kind"] == "arc":
            points.extend(geo.arc_points(piece, spacing)[1:])
            continue
        if piece["kind"] == "line":
            ctrl = np.array([p0, p0 + (p1 - p0) / 3, p0 + 2 * (p1 - p0) / 3, p1])
        else:
            ctrl = np.array([p0, piece["h0"], piece["h1"], p1])
        length = float(np.linalg.norm(np.diff(ctrl, axis=0), axis=1).sum())
        points.extend(bezier(ctrl, np.linspace(0, 1, max(8, int(length / spacing)))[1:]))
    return np.array(points)


def polygon_area(points):
    return 0.5 * float(np.sum(points[:, 0] * np.roll(points[:, 1], -1) - np.roll(points[:, 0], -1) * points[:, 1]))


def perimeter(points):
    return float(np.linalg.norm(np.diff(np.vstack([points, points[:1]]), axis=0), axis=1).sum())


def deviation(curve, dense):
    """Largest and mean distance between the fitted outline and the traced contour, both ways."""
    def nearest(a, b):
        out = []
        for i in range(0, len(a), 512):
            out.append(np.sqrt(((a[i:i + 512, None, :] - b[None, :, :]) ** 2).sum(axis=2)).min(axis=1))
        return np.concatenate(out)
    forward, backward = nearest(curve, dense), nearest(dense, curve)
    return float(max(forward.max(), backward.max())), float(np.concatenate([forward, backward]).mean())


def inside(point, polygon):
    """Even-odd point-in-polygon test."""
    x, y = point
    a, b = polygon, np.roll(polygon, -1, axis=0)
    crosses = (a[:, 1] > y) != (b[:, 1] > y)
    with np.errstate(divide="ignore", invalid="ignore"):
        x_at = a[:, 0] + (y - a[:, 1]) * (b[:, 0] - a[:, 0]) / (b[:, 1] - a[:, 1])
    return bool((crosses & (x < x_at)).sum() % 2)


def recognise_geometry(contours, max_radius):
    """Find circular runs across every contour and apply shared centres, radii, widths and radial ends."""
    arcs, owners = [], []
    for c, contour in enumerate(contours):
        for r, run in enumerate(contour["runs"]):
            if run["straight"]:
                continue
            arc = geo.classify(run["points"], run["closed"], max_radius, STEP)
            if arc is not None:
                arcs.append(arc)
                owners.append((c, r))
    clusters = geo.cluster(arcs)
    accepted = {m for group in clusters for m in group["members"]}
    cluster_of = {m: k for k, group in enumerate(clusters) for m in group["members"]}
    by_run = {owners[m]: m for m in accepted}
    # Bands: the two edges of one ring segment (one contour with arcs at two radii around one centre),
    # or a full-circle outline and the full-circle hole just inside it.
    bands = []
    for c, contour in enumerate(contours):
        for k in range(len(clusters)):
            members = [m for m in accepted if owners[m][0] == c and cluster_of[m] == k and not arcs[m]["closed"]]
            if len(members) < 2:
                continue
            levels = geo.merge_values([arcs[m]["radius"] for m in members], [1] * len(members), geo.WIDTH_TOLERANCE)
            distinct = sorted(set(round(v, 6) for v in levels))
            if len(distinct) == 2:
                inner = [m for m, v in zip(members, levels) if round(v, 6) == distinct[0]]
                outer = [m for m, v in zip(members, levels) if round(v, 6) == distinct[1]]
                bands.append((inner, outer))
    circles = [m for m in accepted if arcs[m]["closed"]]
    for hole in circles:
        if contours[owners[hole][0]]["area"] >= 0:
            continue
        outers = [m for m in circles if contours[owners[m][0]]["area"] > 0 and cluster_of[m] == cluster_of[hole]
                  and contours[owners[m][0]]["color"] == contours[owners[hole][0]]["color"]
                  and arcs[m]["radius"] > arcs[hole]["radius"]]
        if outers:
            bands.append(([hole], [min(outers, key=lambda m: arcs[m]["radius"])]))
    report = geo.constrain(arcs, clusters, bands)
    singles = [m for m in accepted if len(clusters[cluster_of[m]]["members"]) == 1 and arcs[m]["grade"] == "strict"]
    if len(singles) > 1:                  # e.g. the four corners of a rounded rectangle: one corner radius
        merged = geo.merge_values([arcs[m]["radius"] for m in singles], [geo.precision(arcs[m]) for m in singles],
                                  geo.RADIUS_TOLERANCE)
        for m, value in zip(singles, merged):
            arcs[m]["radius"] = value
    for m in accepted:
        c, r = owners[m]
        members = clusters[cluster_of[m]]["members"]   # shared with another radius or another shape
        arcs[m]["shared"] = len({(owners[j][0], round(arcs[j]["radius"], 3)) for j in members}) > 1
        contours[c]["runs"][r]["arc"] = arcs[m]
    # Radial ends: a straight run between two arcs around one centre that points at that centre.
    radial = {}
    for c, contour in enumerate(contours):
        runs = contour["runs"]
        for r, run in enumerate(runs):
            if not run["straight"] or len(runs) < 3:
                continue
            before, after = by_run.get((c, (r - 1) % len(runs))), by_run.get((c, (r + 1) % len(runs)))
            if before is None or after is None or cluster_of[before] != cluster_of[after]:
                continue
            k = cluster_of[before]
            center = clusters[k]["center"]
            point, direction = fit_line(run["points"])
            offset = abs((center - point)[0] * direction[1] - (center - point)[1] * direction[0])
            mean_radius = (arcs[before]["radius"] + arcs[after]["radius"]) / 2
            if offset <= geo.limit(geo.RADIAL_TOLERANCE, mean_radius):
                middle = run["points"].mean(axis=0) - center
                radial.setdefault(k, []).append(((c, r), math.degrees(math.atan2(middle[1], middle[0]))))
    radial_count = 0
    for k, ends in radial.items():
        snapped = geo.snap_radial_angles([angle for _, angle in ends])
        center = clusters[k]["center"]
        for ((c, r), _), angle in zip(ends, snapped):
            rad = math.radians(angle)
            contours[c]["runs"][r]["carrier"] = (center.copy(), np.array([math.cos(rad), math.sin(rad)]))
            radial_count += 1
    return {"centres": [{"centre": [round(float(v), 2) for v in group["center"]],
                         "radii": sorted({round(arcs[m]["radius"], 2) for m in group["members"]}),
                         "arcs": len(group["members"])} for group in clusters],
            "band_widths": report["band_widths"], "merged_radii": report["merged_radii"], "radial_ends": radial_count}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("image", type=Path, help="PNG, JPEG, WebP or another raster Pillow can open")
    parser.add_argument("--out", required=True, type=Path, help="SVG file to write")
    parser.add_argument("--mode", choices=("auto", "color", "mono"), default="auto")
    parser.add_argument("--threshold", type=int, default=128, help="edge position, 1..255")
    parser.add_argument("--background", help="opaque background color, e.g. '#ffffff'; detected otherwise")
    parser.add_argument("--min-area", type=float, default=4.0, help="drop contours smaller than this (px^2)")
    parser.add_argument("--tolerance", type=float, default=None,
                        help="max distance in pixels between a fitted curve and the smoothed edge "
                             "(default: 0.4%% of each shape's size, between 0.6 and 1.5)")
    parser.add_argument("--smooth", type=float, default=1.5,
                        help="edge smoothing in pixels; raise for rougher edges, lower to keep fine detail")
    parser.add_argument("--corner-angle", type=float, default=35.0,
                        help="turn in degrees that counts as a corner (default 35)")
    parser.add_argument("--corner-window", type=float, default=5.0, help="arc length in pixels used to measure turns")
    parser.add_argument("--line-tolerance", type=float, default=0.6,
                        help="a run staying this close (pixels) to its chord becomes an exact line")
    parser.add_argument("--line-ratio", type=float, default=0.01,
                        help="long edges may bow this fraction of their length and still become lines "
                             "(default 0.01; raise to 0.015 for rough lettering, 0 for strict)")
    parser.add_argument("--min-gap", type=float, default=8.0, help="minimum pixels between anchors from extrema")
    parser.add_argument("--no-geometry", action="store_true",
                        help="do not recognise circles and arcs (shared centres, equal radii and widths, radial ends)")
    parser.add_argument("--no-shapes", action="store_true",
                        help="do not replace near-perfect circles, ellipses, rectangles, triangles and regular "
                             "polygons with the exact figure")
    parser.add_argument("--min-turn", type=float, default=5.0,
                        help="minimum degrees a curve must turn around an extremum to get an anchor there")
    args = parser.parse_args()
    try:
        fixed = list(ec.ImageColor.getrgb(args.background)[:3]) if args.background else None
        layers, context = ec.prepare_layers(args.image, fixed, args.mode)
    except (OSError, ValueError) as error:
        sys.exit(f"Cannot fit curves: {error}")
    width, height = context["image"].size
    contours = []
    for color, field in layers:
        for traced in ec.trace(field, args.threshold):
            area = ec.signed_area(traced)
            if abs(area) < args.min_area:
                continue
            loop, tolerance, runs = split_runs(traced, args)
            contours.append({"color": color, "area": area, "loop": loop, "tolerance": tolerance, "runs": runs,
                             "traced_points": len(traced)})
    geometry = None if args.no_geometry else recognise_geometry(contours, 2.0 * math.hypot(width, height))
    svg_paths, report, nodes_total = [], [], 0
    fmt = lambda v: ec.number(round(float(v), 2))  # noqa: E731
    for color, _ in layers:
        built = []
        for contour in (c for c in contours if c["color"] == color):
            pieces = [piece for run in contour["runs"] for piece in run_pieces(run, args, contour["tolerance"])]
            pieces = refit_curves(resolve(fillet(pieces), reach=max(6.0, 6 * contour["tolerance"])))
            shape = None
            if not args.no_shapes:
                size = diagonal_of(contour["loop"])
                shape = shp.recognise(pieces, contour["loop"], sample_path(pieces), deviation, min(max(1.0, 0.012 * size), 4.0))
            outline = shp.outline(shape) if shape else sample_path(pieces)
            worst, mean = deviation(outline, contour["loop"])
            area_change = abs(abs(polygon_area(outline)) - abs(polygon_area(contour["loop"]))) / abs(polygon_area(contour["loop"]))
            perimeter_change = perimeter(outline) / perimeter(contour["loop"]) - 1
            count = (lambda kind: 0 if shape else sum(p["kind"] == kind for p in pieces))
            nodes = shape["nodes"] if shape else len(pieces)
            nodes_total += nodes
            report.append({"color": color, "nodes": nodes, "primitive": shape["kind"] if shape else None,
                           "lines": count("line"), "arcs": count("arc"), "curves": count("curve"),
                           "traced_points": contour["traced_points"],
                           "max_deviation_px": round(worst, 2), "mean_deviation_px": round(mean, 2),
                           "area_change_pct": round(100 * area_change, 2),
                           "perimeter_change_pct": round(100 * perimeter_change, 2)})
            built.append((contour, shape, pieces))
        holes = [c for c, _, _ in built if c["area"] < 0]
        data, standalone = [], []
        for contour, shape, pieces in built:
            alone = shape is not None and contour["area"] > 0 and not any(
                inside(h["loop"][0], contour["loop"]) for h in holes)
            if alone:
                standalone.append(shp.element(shape, color, fmt))
            else:
                data.append(shp.path_data(shape, fmt) if shape else path_data(pieces))
        if data:
            svg_paths.append(f'<path fill="{color}" fill-rule="evenodd" d="{" ".join(data)}"/>')
        svg_paths.extend(standalone)
    if not svg_paths:
        sys.exit("No contours were found. Lower --threshold or --min-area.")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
                        f'viewBox="0 0 {width} {height}">' + "".join(svg_paths) + "</svg>\n", encoding="utf-8")
    worst = max(r["max_deviation_px"] for r in report)
    summary = {"svg": str(args.out), "contours": len(report), "nodes": nodes_total,
               "traced_points": sum(r["traced_points"] for r in report),
               "max_deviation_px": worst, "geometry": geometry, "per_contour": report}
    warnings = []
    folds = [i for i, r in enumerate(report) if r["perimeter_change_pct"] > 5 or r["area_change_pct"] > 5]
    if folds:
        warnings.append(f"Contours {folds} differ from the trace by more than 5% in area or length: an edge may "
                        "fold back on itself. Look at the wireframe and redraw those shapes.")
    loose = [i for i, r in enumerate(report) if r["max_deviation_px"] > 2.0 and r["arcs"] == 0 and not r["primitive"]]
    if loose:
        warnings.append(f"Contours {loose} stray more than 2 px from the traced edge: check them against the "
                        "source, then lower --smooth or --tolerance, or redraw that part by hand.")
    moved = [i for i, r in enumerate(report) if r["max_deviation_px"] > 2.0 and (r["arcs"] > 0 or r["primitive"])]
    if moved:
        summary["note"] = (f"Contours {moved} were moved more than 2 px to make their arcs exact (shared centres, "
                           "equal radii and widths). That is intended when the source edge is uneven; compare the "
                           "render with the source to confirm the corrected shape is the intended one.")
    if warnings:
        summary["warning"] = " ".join(warnings)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
