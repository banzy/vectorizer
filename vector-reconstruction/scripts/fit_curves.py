#!/usr/bin/env python3
"""Fit clean, minimal Bezier outlines to a flat-color image (lettering, wordmarks, logos).

extract_contours.py traces pixel edges, so its draft is a polyline that follows every
bump of a rough or low-resolution edge. This script takes the same dense sub-pixel
contours and builds the kind of outline a designer would draw: edge noise is smoothed
away, real corners stay sharp, straight edges become exact lines (horizontals and
verticals snapped to the axis), and curves become few cubic Beziers with anchors at the
horizontal/vertical extrema and handles aligned there. Requires Pillow and numpy.

The result is a better starting point for rebuilding, not a replacement for looking:
compare it with the source, then redraw anything it got wrong (a rounded terminal
turned into a corner, a flare flattened into a line).
"""
import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import extract_contours as ec  # noqa: E402

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
    reach = max(4, min(int(round(20 / STEP)), count // 4))   # look 20 px (or a quarter of the run) either side
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


def fit_run(run, closed, start_corner, end_corner, args):
    """Fit one smoothed run (between two corners, or a whole smooth loop) into segments.

    Returns (start point, segments, shifts); a segment is ("L", end) or ("C", control 1, control 2, end).
    A straight segment's shift moves it sideways so it sits in the middle of the edge it replaces.
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
    segments, shifts = [], []
    for (a, a_axis), (b, b_axis) in pieces:
        idx = np.arange(a, b + 1) if b > a else np.r_[np.arange(a, count), np.arange(0, b + 1)]
        piece = run[idx % count]
        if not closed and a == 0 and b == count - 1:
            piece = run
        if len(piece) < 2:
            continue
        if is_straight(piece, args.line_tolerance, args.line_ratio) and heading_change(piece, len(piece) // 2, len(piece) // 2) < 12:
            chord = piece[-1] - piece[0]
            normal = np.array([-chord[1], chord[0]]) / np.linalg.norm(chord)
            offset = (piece - piece[0]) @ normal
            segments.append(("L", piece[-1]))
            shifts.append(normal * (offset.max() + offset.min()) / 2)
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
                segments.append(("L", ctrl[3]))
            else:
                segments.append(("C", ctrl[1], ctrl[2], ctrl[3]))
            shifts.append(None)
    return run[anchors[0][0]].copy(), segments, shifts


# ---------------------------------------------------------------- whole contour

def fit_loop(points, args):
    loop = resample(np.array(points, dtype=float))
    if args.tolerance is None:     # scale with the shape: 0.4% of its diagonal, between 0.6 and 1.5 px
        diagonal = float(np.linalg.norm(loop.max(axis=0) - loop.min(axis=0)))
        args = argparse.Namespace(**{**vars(args), "tolerance": min(1.5, max(0.6, 0.004 * diagonal))})
    corners = find_corners(loop, args.corner_window, args.corner_angle)
    count = len(loop)
    sigma = args.smooth
    if not corners:
        start, segments, shifts = fit_run(smooth_closed(loop, sigma), True, None, None, args)
        return start, segments, shifts, loop
    segments, shifts, start = [], [], None
    for position, a in enumerate(corners):
        b = corners[(position + 1) % len(corners)]
        idx = np.arange(a, b + 1) if b > a else np.r_[np.arange(a, count), np.arange(0, b + 1)]
        if len(corners) == 1:
            idx = np.r_[np.arange(a, count), np.arange(0, a + 1)]
        run = smooth_open(loop[idx % count], sigma)
        first, part, part_shifts = fit_run(run, False, a, b, args)
        start = first if start is None else start
        segments += part
        shifts += part_shifts
    return start, segments, shifts, loop


def snap_axes(start, segments, shifts, tolerance_deg=2.0):
    """Centre straight edges, then make near-horizontal/vertical ones exact, moving shared anchors and handles."""
    anchors = [np.array(start)] + [np.array(segment[-1]) for segment in segments[:-1]]
    ends = [np.array(segment[-1]) for segment in segments]
    # anchors[i] is the start of segment i; ends[i] is its end (which is anchors[i+1], the last wraps to start).
    points = anchors[:]                      # mutable anchor positions, indexed like segments
    delta = [np.zeros(2) for _ in points]
    for i, shift in enumerate(shifts):
        if shift is not None:
            for j in (i, (i + 1) % len(points)):
                points[j] = points[j] + shift
                delta[j] = delta[j] + shift
    for i, segment in enumerate(segments):
        if segment[0] != "L":
            continue
        a, b = points[i], points[(i + 1) % len(points)]
        vector = b - a
        if np.linalg.norm(vector) < 1e-9:
            continue
        angle = math.degrees(math.atan2(vector[1], vector[0])) % 180
        if min(angle, 180 - angle) <= tolerance_deg:          # horizontal: share y
            y = (a[1] + b[1]) / 2
            for p, j in ((a, i), (b, (i + 1) % len(points))):
                delta[j][1] += y - p[1]
                p[1] = y
        elif abs(angle - 90) <= tolerance_deg:                # vertical: share x
            x = (a[0] + b[0]) / 2
            for p, j in ((a, i), (b, (i + 1) % len(points))):
                delta[j][0] += x - p[0]
                p[0] = x
    new_segments = []
    for i, segment in enumerate(segments):
        end_index = (i + 1) % len(points)
        if segment[0] == "L":
            new_segments.append(("L", points[end_index]))
        else:
            new_segments.append(("C", segment[1] + delta[i], segment[2] + delta[end_index], points[end_index]))
    return points[0], new_segments


def path_data(start, segments):
    f = lambda v: ec.number(round(float(v), 2))  # noqa: E731
    parts = [f"M{f(start[0])} {f(start[1])}"]
    for segment in segments:
        if segment[0] == "L":
            parts.append(f"L{f(segment[1][0])} {f(segment[1][1])}")
        else:
            parts.append("C" + " ".join(f"{f(p[0])} {f(p[1])}" for p in segment[1:]))
    return "".join(parts) + "Z"


def sample_path(start, segments, spacing=0.4):
    """Points along the outline about `spacing` pixels apart, for measuring how far it strays."""
    points, current = [np.array(start, dtype=float)], np.array(start, dtype=float)
    for segment in segments:
        end = np.array(segment[-1], dtype=float)
        if segment[0] == "L":
            ctrl = np.array([current, current + (end - current) / 3, current + 2 * (end - current) / 3, end])
        else:
            ctrl = np.array([current, segment[1], segment[2], end])
        length = float(np.linalg.norm(np.diff(ctrl, axis=0), axis=1).sum())
        points.extend(bezier(ctrl, np.linspace(0, 1, max(8, int(length / spacing)))[1:]))
        current = end
    return np.array(points)


def deviation(curve, dense):
    """Largest and mean distance between the fitted outline and the traced contour, both ways."""
    def nearest(a, b):
        out = []
        for i in range(0, len(a), 512):
            out.append(np.sqrt(((a[i:i + 512, None, :] - b[None, :, :]) ** 2).sum(axis=2)).min(axis=1))
        return np.concatenate(out)
    forward, backward = nearest(curve, dense), nearest(dense, curve)
    return float(max(forward.max(), backward.max())), float(np.concatenate([forward, backward]).mean())


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
    parser.add_argument("--min-turn", type=float, default=5.0,
                        help="minimum degrees a curve must turn around an extremum to get an anchor there")
    args = parser.parse_args()
    try:
        fixed = list(ec.ImageColor.getrgb(args.background)[:3]) if args.background else None
        layers, context = ec.prepare_layers(args.image, fixed, args.mode)
    except (OSError, ValueError) as error:
        sys.exit(f"Cannot fit curves: {error}")
    width, height = context["image"].size
    svg_paths, report, nodes_total = [], [], 0
    for color, field in layers:
        data = []
        for loop in ec.trace(field, args.threshold):
            if abs(ec.signed_area(loop)) < args.min_area:
                continue
            start, segments, shifts, dense = fit_loop(loop, args)
            start, segments = snap_axes(start, segments, shifts)
            curve = sample_path(start, segments)
            worst, mean = deviation(curve, dense)
            nodes = len(segments)
            nodes_total += nodes
            report.append({"color": color, "nodes": nodes,
                           "lines": sum(s[0] == "L" for s in segments), "curves": sum(s[0] == "C" for s in segments),
                           "traced_points": len(set(map(tuple, np.round(loop, 3)))),
                           "max_deviation_px": round(worst, 2), "mean_deviation_px": round(mean, 2)})
            data.append(path_data(start, segments))
        if data:
            svg_paths.append(f'<path fill="{color}" fill-rule="evenodd" d="{" ".join(data)}"/>')
    if not svg_paths:
        sys.exit("No contours were found. Lower --threshold or --min-area.")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
                        f'viewBox="0 0 {width} {height}">' + "".join(svg_paths) + "</svg>\n", encoding="utf-8")
    worst = max(r["max_deviation_px"] for r in report)
    summary = {"svg": str(args.out), "contours": len(report), "nodes": nodes_total,
               "traced_points": sum(r["traced_points"] for r in report),
               "max_deviation_px": worst, "per_contour": report}
    if worst > 2.0:
        summary["warning"] = ("Some outline strays more than 2 px from the traced edge: check it against the "
                              "source, then lower --smooth or --tolerance, or redraw that part by hand.")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
