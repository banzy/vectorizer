#!/usr/bin/env python3
"""Extract a source PNG and compact evidence from a vectorizer export."""
import argparse
import base64
import binascii
import json
import math
from pathlib import Path
import struct


def segment_distance(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = dx * dx + dy * dy
    t = 0 if length == 0 else max(0, min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length))
    return math.dist(p, (a[0] + t * dx, a[1] + t * dy))


def mean_distance(points, outline):
    """Mean distance from up to 48 of `points` to the closed polyline `outline`."""
    step = max(1, len(points) // 48)
    edges = list(zip(outline, outline[1:] + outline[:1]))
    samples = points[::step]
    return sum(min(segment_distance(p, a, b) for a, b in edges) for p in samples) / len(samples)


def find_duplicates(contours):
    """Contours describing the same object twice (same role and color), or outer/hole pairs that
    nearly coincide (a zero-width sliver). Either must be drawn once, or not at all."""
    shapes = []
    for contour in contours:
        points = contour.get("boundary_points_px") or contour.get("raw_pixel_center_points_px") or []
        if len(points) < 3:
            continue
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        area = abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1]))) / 2
        shapes.append((contour, points, (min(xs), min(ys), max(xs), max(ys)), area))
    found = []
    for i, (a, points_a, box_a, area_a) in enumerate(shapes):
        for b, points_b, box_b, area_b in shapes[i + 1:]:
            same_role = a.get("role") == b.get("role")
            if a.get("color_hex") != b.get("color_hex"):
                continue
            if same_role and min(area_a, area_b) < 0.9 * max(area_a, area_b):
                continue
            if not same_role and a["id"] not in (b.get("parent_id"),) and b["id"] not in (a.get("parent_id"),):
                continue
            tolerance = max(1.5, 0.02 * max(box_a[2] - box_a[0], box_a[3] - box_a[1]))
            if max(abs(u - v) for u, v in zip(box_a, box_b)) > tolerance:
                continue
            distance = (mean_distance(points_a, points_b) + mean_distance(points_b, points_a)) / 2
            if distance <= (1.0 if same_role else 0.5):
                found.append({"ids": [a["id"], b["id"]], "kind": "duplicate" if same_role else "sliver",
                              "mean_distance_px": round(distance, 3)})
    return found


def prepare(export_path, output_dir):
    package = json.loads(export_path.read_text(encoding="utf-8"))
    if package.get("schema") != "logo-designer-brief" or package.get("version") != 1:
        raise ValueError("Expected a logo-designer-brief export, version 1.")
    image = package.get("image", {})
    dimensions = [image.get("width"), image.get("height")]
    if any(type(value) is not int or value <= 0 for value in dimensions):
        raise ValueError("Image dimensions must be positive integers.")
    contours = package.get("contours")
    if not isinstance(contours, list) or not contours:
        raise ValueError("No contour evidence was supplied.")
    ids = [contour.get("id") for contour in contours]
    if any(type(value) is not int for value in ids) or len(set(ids)) != len(ids):
        raise ValueError("Contour IDs must be unique integers.")
    for contour in contours:
        if contour.get("parent_id") is not None and contour["parent_id"] not in ids:
            raise ValueError("A contour refers to an absent parent.")
        for field in ("boundary_points_px", "normalized_boundary", "raw_pixel_center_points_px"):
            for point in contour.get(field, []):
                if (not isinstance(point, list) or len(point) != 2 or
                    any(type(value) not in (int, float) or not math.isfinite(value) for value in point)):
                    raise ValueError(f"Invalid coordinate in contour {contour['id']}: {field}")

    png = None
    data_url = image.get("png_data_url")
    if data_url is not None:
        prefix = "data:image/png;base64,"
        if not isinstance(data_url, str) or not data_url.startswith(prefix):
            raise ValueError("Embedded source must be a base64 PNG data URL.")
        png = base64.b64decode(data_url[len(prefix):], validate=True)
        if (len(png) < 33 or png[:8] != b"\x89PNG\r\n\x1a\n" or png[12:16] != b"IHDR"):
            raise ValueError("Embedded source does not have a valid PNG header.")
        if list(struct.unpack(">II", png[16:24])) != dimensions:
            raise ValueError("Embedded PNG dimensions disagree with the export.")

    compact = {key: value for key, value in package.items() if key != "instructions"}
    compact["image"] = {key: value for key, value in image.items() if key != "png_data_url"}
    if png is not None:
        compact["image"]["local_source_file"] = "source.png"
    else:
        compact["image"]["attachment_required"] = "Use the separately supplied original image."
    compact["contours"] = []
    for contour in contours:
        drop = ["raw_pixel_center_points_px", "normalized_boundary"]
        if package.get("producer") == "extract_contours.py":
            drop.append("current_geometry")  # only a polyline copy of boundary_points_px; see trace.svg
        record = {key: value for key, value in contour.items() if key not in drop}
        if "raw_pixel_center_points_px" in contour:
            record["raw_point_count"] = len(contour["raw_pixel_center_points_px"])
        compact["contours"].append(record)
    duplicates = find_duplicates(contours)
    later = {pair["ids"][1]: pair for pair in duplicates}
    for record in compact["contours"]:
        if record["id"] in later:
            pair = later[record["id"]]
            record["duplicate_of" if pair["kind"] == "duplicate" else "sliver_with"] = pair["ids"][0]
    compact["possible_duplicates"] = duplicates
    compact["evidence_note"] = (
        "Pixel boundary coordinates are retained. Redundant normalized coordinates, raw points "
        "and the embedded image were removed from this compact view. Consult the original export "
        "for local measurements. Supplied traces and primitive fits remain hypotheses. "
        "possible_duplicates lists contours that trace the same object twice (draw it once) or "
        "outer/hole pairs that nearly coincide (a zero-width sliver: usually drop both)."
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    if png is not None:
        (output_dir / "source.png").write_bytes(png)
    (output_dir / "evidence.json").write_text(json.dumps(compact, indent=2) + "\n", encoding="utf-8")
    return {"dimensions": dimensions, "contours": len(contours),
            "source_extracted": png is not None, "possible_duplicates": len(duplicates), "evidence_file": str(output_dir / "evidence.json")}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    arguments = parser.parse_args()
    try:
        print(json.dumps(prepare(arguments.export, arguments.out), indent=2))
    except (OSError, ValueError, TypeError, AttributeError, binascii.Error) as error:
        parser.exit(1, f"Cannot prepare evidence: {error}\n")
