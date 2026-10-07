#!/usr/bin/env python3
"""Render an SVG to a transparent PNG at exact pixel dimensions using an installed renderer, or
build a small-size legibility sheet with --sizes (needs Pillow)."""
import argparse
import html
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

CHROME_NAMES = ("google-chrome", "chromium", "chromium-browser", "chrome",
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Chromium.app/Contents/MacOS/Chromium")


def find_chrome():
    return next((path for name in CHROME_NAMES if (path := shutil.which(name) or
                 (name if Path(name).exists() else None))), None)


def render(svg, png, width, height):
    """Try each available renderer in turn; return the name of the one that worked."""
    png.parent.mkdir(parents=True, exist_ok=True)
    try:
        import cairosvg
        cairosvg.svg2png(url=str(svg), write_to=str(png), output_width=width, output_height=height)
        return "cairosvg"
    except ImportError:
        pass
    attempts = []
    if shutil.which("rsvg-convert"):
        attempts.append(("rsvg-convert", ["rsvg-convert", "-w", str(width), "-h", str(height),
                                          "-o", str(png), str(svg)]))
    if shutil.which("inkscape"):
        attempts.append(("inkscape", ["inkscape", str(svg), "-w", str(width), "-h", str(height),
                                      "-o", str(png)]))
    for name, command in attempts:
        if subprocess.run(command, capture_output=True).returncode == 0 and png.exists():
            return name
    chrome = find_chrome()
    if chrome:
        with tempfile.TemporaryDirectory() as folder:
            page = Path(folder) / "page.html"
            page.write_text(
                f'<style>html,body{{margin:0;background:transparent}}img{{display:block}}</style>'
                f'<img src="{html.escape(svg.resolve().as_uri())}" width="{width}" height="{height}">',
                encoding="utf-8")
            command = [chrome, "--headless", "--disable-gpu", "--hide-scrollbars",
                       "--default-background-color=00000000", f"--window-size={width},{height}",
                       f"--screenshot={png.resolve()}", page.as_uri()]
            if subprocess.run(command, capture_output=True).returncode == 0 and png.exists():
                return "headless chrome"
    raise RuntimeError("No SVG renderer found. Install one of: cairosvg (pip), rsvg-convert "
                       "(librsvg), inkscape, or Chrome/Chromium.")


def aspect_ratio(svg):
    root = ET.parse(svg).getroot()
    view = [float(v) for v in re.findall(r"[-+]?(?:\d+\.?\d*|\.\d+)", root.get("viewBox", ""))]
    if len(view) == 4 and view[3] > 0:
        return view[2] / view[3]
    size = [float(re.match(r"[\d.]+", root.get(k, "0") or "0").group() or 0) for k in ("width", "height")]
    if size[1] <= 0:
        raise ValueError("The SVG has no viewBox or height to derive its proportions from.")
    return size[0] / size[1]


def size_sheet(svg, png, heights):
    """Render at each height; show it actual-size on light and dark, then magnified without smoothing."""
    from PIL import Image, ImageDraw
    aspect = aspect_ratio(svg)
    renders = []
    with tempfile.TemporaryDirectory() as folder:
        for height in heights:
            path = Path(folder) / f"{height}.png"
            render(svg, path, max(1, round(height * aspect)), height)
            with Image.open(path) as image:
                renders.append(image.convert("RGBA"))
    zoom = max(1, 160 // max(heights))
    gap, label = 16, 18
    columns = [max(image.width * max(1, 160 // image.height), image.width) for image in renders]
    rows = [max(heights), max(heights), max(image.height * max(1, 160 // image.height) for image in renders)]
    sheet = Image.new("RGB", (sum(columns) + gap * (len(columns) + 1) + 90,
                              sum(rows) + (gap + label) * len(rows) + gap), "#f3f4f6")
    draw = ImageDraw.Draw(sheet)
    y = gap
    for row, (title, background) in enumerate((("light", "#ffffff"), ("dark", "#16181d"), ("pixels", "#ffffff"))):
        draw.text((gap, y + label + rows[row] // 2 - 6), title, fill="#374151")
        x = gap + 90
        for image, width, height in zip(renders, columns, heights):
            if row == 0:
                draw.text((x, y), f"{height}px", fill="#374151")
            view = image if row < 2 else image.resize(
                (image.width * max(1, 160 // image.height), image.height * max(1, 160 // image.height)),
                Image.Resampling.NEAREST)
            tile = Image.new("RGB", view.size, background)
            tile.paste(view, mask=view)
            sheet.paste(tile, (x, y + label))
            x += width + gap
        y += rows[row] + label + gap
    png.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(png)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("svg", type=Path)
    parser.add_argument("png", type=Path, help="output PNG path")
    parser.add_argument("--width", type=int, help="source image width in pixels")
    parser.add_argument("--height", type=int, help="source image height in pixels")
    parser.add_argument("--sizes", type=int, nargs="+", metavar="PX",
                        help="instead write a legibility sheet at these heights, e.g. --sizes 16 24 32 48 64")
    args = parser.parse_args()
    try:
        if not args.svg.is_file():
            raise FileNotFoundError(f"{args.svg} does not exist.")
        if args.sizes:
            if min(args.sizes) <= 0:
                raise ValueError("Sizes must be positive.")
            size_sheet(args.svg, args.png, sorted(args.sizes))
            print(f"Wrote size sheet {args.png} at {', '.join(map(str, sorted(args.sizes)))}px.")
        else:
            if not args.width or not args.height or min(args.width, args.height) <= 0:
                raise ValueError("Give positive --width and --height (or --sizes for a size sheet).")
            print(f"Rendered {args.png} ({args.width}x{args.height}) with {render(args.svg, args.png, args.width, args.height)}.")
    except (OSError, ValueError, RuntimeError, ET.ParseError) as error:
        parser.exit(1, f"Cannot render SVG: {error}\n")
