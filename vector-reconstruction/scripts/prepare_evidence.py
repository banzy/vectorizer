#!/usr/bin/env python3
"""Extract a source PNG and compact evidence from a vectorizer export."""
import argparse
import base64
import binascii
import json
import math
from pathlib import Path
import struct


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
        record = {key: value for key, value in contour.items()
                  if key not in ("raw_pixel_center_points_px", "normalized_boundary")}
        if "raw_pixel_center_points_px" in contour:
            record["raw_point_count"] = len(contour["raw_pixel_center_points_px"])
        compact["contours"].append(record)
    compact["evidence_note"] = (
        "Pixel boundary coordinates are retained. Redundant normalized coordinates, raw points "
        "and the embedded image were removed from this compact view. Consult the original export "
        "for local measurements. Supplied traces and primitive fits remain hypotheses."
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    if png is not None:
        (output_dir / "source.png").write_bytes(png)
    (output_dir / "evidence.json").write_text(json.dumps(compact, indent=2) + "\n", encoding="utf-8")
    return {"dimensions": dimensions, "contours": len(contours),
            "source_extracted": png is not None, "evidence_file": str(output_dir / "evidence.json")}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    arguments = parser.parse_args()
    try:
        print(json.dumps(prepare(arguments.export, arguments.out), indent=2))
    except (OSError, ValueError, TypeError, AttributeError, binascii.Error) as error:
        parser.exit(1, f"Cannot prepare evidence: {error}\n")
