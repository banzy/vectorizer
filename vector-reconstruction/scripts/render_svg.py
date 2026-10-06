#!/usr/bin/env python3
"""Render an SVG to a transparent PNG at exact pixel dimensions using an installed renderer."""
import argparse
import html
import shutil
import subprocess
import tempfile
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("svg", type=Path)
    parser.add_argument("png", type=Path, help="output PNG path")
    parser.add_argument("--width", required=True, type=int, help="source image width in pixels")
    parser.add_argument("--height", required=True, type=int, help="source image height in pixels")
    args = parser.parse_args()
    try:
        if min(args.width, args.height) <= 0:
            raise ValueError("Width and height must be positive.")
        if not args.svg.is_file():
            raise FileNotFoundError(f"{args.svg} does not exist.")
        print(f"Rendered {args.png} ({args.width}x{args.height}) with {render(args.svg, args.png, args.width, args.height)}.")
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, f"Cannot render SVG: {error}\n")
