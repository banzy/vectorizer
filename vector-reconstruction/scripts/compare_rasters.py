#!/usr/bin/env python3
"""Compare equal-sized source and rendered images. Requires Pillow, not an SVG renderer."""
import argparse
import json
from pathlib import Path

from PIL import Image, ImageChops, ImageColor, ImageDraw, ImageStat


def foreground(image, alpha_threshold, background, color_threshold):
    if background is None:
        return image.getchannel("A").point(lambda value: 255 if value >= alpha_threshold else 0)
    flat = Image.new("RGBA", image.size, (*background, 255))
    flat.alpha_composite(image)
    mask = Image.new("L", image.size)
    mask.putdata([255 if max(abs(pixel[i] - background[i]) for i in range(3)) > color_threshold
                  else 0 for pixel in flat.getdata()])
    return mask


def compare(source_path, rendered_path, output_dir, crop=None, background=None,
            source_alpha=128, rendered_alpha=128, color_threshold=20):
    source = Image.open(source_path).convert("RGBA")
    rendered = Image.open(rendered_path).convert("RGBA")
    if source.size != rendered.size:
        raise ValueError(f"Render at source dimensions first: {source.size} != {rendered.size}.")
    original_size = source.size
    if crop:
        x, y, width, height = crop
        if min(x, y) < 0 or min(width, height) <= 0 or x + width > source.width or y + height > source.height:
            raise ValueError("Crop must have positive dimensions and lie within both images.")
        bounds = (x, y, x + width, y + height)
        source, rendered = source.crop(bounds), rendered.crop(bounds)
    can_measure_mask = background is not None or source.getchannel("A").getextrema()[0] < 255
    metrics = {"source_size": list(original_size), "comparison_size": list(source.size),
               "crop_xywh": list(crop) if crop else None, "background_rgb": background,
               "source_alpha_threshold": source_alpha, "rendered_alpha_threshold": rendered_alpha,
               "color_threshold": color_threshold if background is not None else None}
    for label, color in (("white", "white"), ("black", "black")):
        a = Image.new("RGBA", source.size, color); a.alpha_composite(source)
        b = Image.new("RGBA", source.size, color); b.alpha_composite(rendered)
        difference = ImageChops.difference(a.convert("RGB"), b.convert("RGB"))
        metrics[f"mean_absolute_rgb_error_on_{label}_0_to_255"] = sum(ImageStat.Stat(difference).mean) / 3
        if label == "white":
            source_view, rendered_view, color_difference = a.convert("RGB"), b.convert("RGB"), difference
    if can_measure_mask:
        a = foreground(source, source_alpha, background, color_threshold)
        b = foreground(rendered, rendered_alpha, background, color_threshold)
        overlap, union = ImageChops.darker(a, b), ImageChops.lighter(a, b)
        intersection_count, union_count = overlap.histogram()[255], union.histogram()[255]
        missed, added = ImageChops.subtract(a, b), ImageChops.subtract(b, a)
        metrics.update(silhouette_iou=intersection_count / union_count if union_count else None,
                       missed_foreground_pixels=missed.histogram()[255],
                       added_foreground_pixels=added.histogram()[255])
        mask_view = Image.new("RGB", source.size, "white")
        mask_view.paste("#ced6df", mask=overlap)
        mask_view.paste("#e34459", mask=missed)
        mask_view.paste("#327be5", mask=added)
        views = [source_view, rendered_view, mask_view]
        labels = ["Source", "Rendered", "Mask: red missed / blue added"]
    else:
        metrics.update(silhouette_iou=None,
                       mask_note="Source is opaque. Specify a known --background for a silhouette metric.")
        views, labels = [source_view, rendered_view, color_difference], ["Source", "Rendered", "RGB difference"]
    padding, header = 12, 30
    sheet = Image.new("RGB", ((source.width + padding) * 3 + padding, source.height + header + padding * 2), "#f3f5f7")
    draw = ImageDraw.Draw(sheet)
    for index, (view, label) in enumerate(zip(views, labels)):
        x = padding + index * (source.width + padding)
        sheet.paste(view, (x, header + padding))
        draw.text((x, padding), label, fill="#243247")
    output_dir.mkdir(parents=True, exist_ok=True)
    sheet.save(output_dir / "comparison.png")
    (output_dir / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    return metrics


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("rendered", type=Path, help="SVG already rendered to PNG at source dimensions")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--crop", nargs=4, type=int, metavar=("X", "Y", "WIDTH", "HEIGHT"))
    parser.add_argument("--background", help="Known opaque background, e.g. '#ffffff'; not guessed")
    parser.add_argument("--source-alpha", type=int, choices=range(1, 256), metavar="1..255", default=128)
    parser.add_argument("--rendered-alpha", type=int, choices=range(1, 256), metavar="1..255", default=128)
    parser.add_argument("--color-threshold", type=int, choices=range(256), metavar="0..255", default=20)
    args = parser.parse_args()
    try:
        background = list(ImageColor.getrgb(args.background)) if args.background else None
        if background is not None and len(background) != 3:
            raise ValueError("Background must be an RGB color without alpha.")
        result = compare(args.source, args.rendered, args.out, args.crop, background,
                         args.source_alpha, args.rendered_alpha, args.color_threshold)
        print(json.dumps(result, indent=2))
    except (OSError, ValueError) as error:
        parser.exit(1, f"Cannot compare images: {error}\n")
