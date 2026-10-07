#!/usr/bin/env python3
"""Write a one-button download page for an SVG, to publish with the Artifact tool (declare the
`downloads` capability). Artifact viewers block plain download links, so the button uses it.
The output is an Artifact page body (title, style, markup, script), not a full HTML document."""
import argparse
import base64
import html
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

PAGE = """<title>__TITLE__</title>
<style>
:root {
  --bg: #f4f5f7; --surface: #ffffff; --ink: #14181f; --muted: #5b6573; --line: #dde1e7;
  --accent: #1f4fd8; --accent-ink: #ffffff; --tile: #ffffff; --tile-alt: #e9ecf1;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg: #0f1217; --surface: #181c23; --ink: #eef1f5; --muted: #99a3b1; --line: #2a303a;
  --accent: #7ea1ff; --accent-ink: #0b1226; --tile: #ffffff; --tile-alt: #d9dde4; color-scheme: dark; } }
:root[data-theme="dark"] {
  --bg: #0f1217; --surface: #181c23; --ink: #eef1f5; --muted: #99a3b1; --line: #2a303a;
  --accent: #7ea1ff; --accent-ink: #0b1226; --tile: #ffffff; --tile-alt: #d9dde4; color-scheme: dark; }
body { background: var(--bg); color: var(--ink); padding-inline: 16px; padding-block: 32px;
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 640px; margin-inline: auto; display: flex; flex-direction: column; gap: 20px; }
.stage { background: var(--surface); border: 1px solid var(--line); border-radius: 14px; padding: 16px; }
.tile { border-radius: 8px; min-height: 220px; display: grid; place-items: center; padding: 24px;
  background-color: var(--tile); }
.tile.checker { background-image: linear-gradient(45deg, var(--tile-alt) 25%, transparent 25%),
  linear-gradient(-45deg, var(--tile-alt) 25%, transparent 25%),
  linear-gradient(45deg, transparent 75%, var(--tile-alt) 75%),
  linear-gradient(-45deg, transparent 75%, var(--tile-alt) 75%);
  background-size: 20px 20px; background-position: 0 0, 0 10px, 10px -10px, -10px 0; }
.tile.dark { background: #16181d; }
.tile img { max-width: 100%; max-height: 360px; display: block; }
.row { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; justify-content: space-between; }
.tabs { display: flex; gap: 4px; }
.tabs button { font: inherit; font-size: 13px; color: var(--muted); background: none; border: 1px solid var(--line);
  border-radius: 6px; padding: 4px 10px; cursor: pointer; }
.tabs button[aria-pressed="true"] { color: var(--ink); border-color: var(--ink); }
.meta { color: var(--muted); font-size: 13px; font-variant-numeric: tabular-nums; }
.go { font: inherit; font-weight: 600; font-size: 16px; color: var(--accent-ink); background: var(--accent);
  border: 0; border-radius: 10px; padding: 14px 22px; cursor: pointer; width: 100%; }
.go:focus-visible, .tabs button:focus-visible { outline: 3px solid var(--accent); outline-offset: 2px; }
.status { min-height: 1.5em; color: var(--muted); font-size: 14px; text-align: center; }
textarea { width: 100%; min-height: 140px; font: 12px/1.4 ui-monospace, Menlo, monospace; background: var(--surface);
  color: var(--ink); border: 1px solid var(--line); border-radius: 8px; padding: 10px; box-sizing: border-box; }
</style>
<main>
  <section class="stage" aria-label="Preview">
    <div class="tile" id="tile"><img id="preview" alt="__ALT__" src="__DATA_URI__"></div>
    <div class="row" style="margin-top:12px">
      <div class="tabs" role="group" aria-label="Preview background">
        <button type="button" data-bg="" aria-pressed="true">Light</button>
        <button type="button" data-bg="dark" aria-pressed="false">Dark</button>
        <button type="button" data-bg="checker" aria-pressed="false">Transparent</button>
      </div>
      <span class="meta">__FILENAME__ · __SIZE__</span>
    </div>
  </section>
  <button class="go" id="download" type="button">Download SVG</button>
  <div class="status" id="status" role="status"></div>
  <div id="fallback" hidden>
    <p class="meta">Saving is not available in this view. Copy the SVG and paste it into a file named __FILENAME__.</p>
    <textarea id="code" readonly></textarea>
    <button class="go" id="copy" type="button" style="margin-top:8px">Copy SVG</button>
  </div>
</main>
<script>
const SVG = __SVG_JSON__;
const FILENAME = __FILENAME_JSON__;
const $ = (id) => document.getElementById(id);
const status = (text) => { $("status").textContent = text; };
document.querySelectorAll(".tabs button").forEach((button) => button.addEventListener("click", () => {
  $("tile").className = "tile" + (button.dataset.bg ? " " + button.dataset.bg : "");
  document.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-pressed", String(b === button)));
}));
function showFallback() {
  $("download").hidden = true;
  $("fallback").hidden = false;
  $("code").value = SVG;
}
$("download").addEventListener("click", async () => {
  let downloads = null;
  try { downloads = await claude.use("downloads"); } catch (error) { downloads = null; }
  if (!downloads) { showFallback(); return; }
  try {
    await downloads.save({ filename: FILENAME, data: SVG });
    status("Saved " + FILENAME);
  } catch (error) {
    if (error && error.code === "declined") status("Download cancelled. Click the button to try again.");
    else if (error && error.code === "rate_limited") status("A save prompt is already open. Answer it, then try again.");
    else showFallback();
  }
});
$("copy").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText(SVG); status("SVG copied"); }
  catch (error) { $("code").select(); status("Press Ctrl/Cmd+C to copy the selected SVG"); }
});
</script>
"""


def build(svg_path, name, filename):
    text = svg_path.read_text(encoding="utf-8")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as error:
        raise ValueError(f"The file is not valid SVG: {error}") from error
    if not root.tag.endswith("svg"):
        raise ValueError("The file's root element is not <svg>.")
    filename = filename or (re.sub(r"[^A-Za-z0-9._-]+", "-", svg_path.stem).strip("-") or "logo") + ".svg"
    if not filename.lower().endswith(".svg"):
        filename += ".svg"
    data_uri = "data:image/svg+xml;base64," + base64.b64encode(text.encode("utf-8")).decode("ascii")
    size = len(text.encode("utf-8"))
    size_text = f"{size / 1024:.1f} KB" if size >= 1024 else f"{size} bytes"
    safe_json = lambda value: json.dumps(value).replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    replacements = {"__TITLE__": html.escape(f"{name} Vector"), "__ALT__": html.escape(f"{name} logo preview", quote=True),
                    "__DATA_URI__": data_uri, "__FILENAME__": html.escape(filename), "__SIZE__": size_text,
                    "__SVG_JSON__": safe_json(text), "__FILENAME_JSON__": safe_json(filename)}
    page = PAGE
    for key, value in replacements.items():
        page = page.replace(key, value)
    return page, filename


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("svg", type=Path)
    parser.add_argument("--out", required=True, type=Path, help="page file to write, e.g. download.html")
    parser.add_argument("--name", default="Logo", help="logo name used in the page title (2-4 words total)")
    parser.add_argument("--filename", help="suggested download name; default is derived from the SVG file name")
    args = parser.parse_args()
    try:
        page, filename = build(args.svg, args.name, args.filename)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(page, encoding="utf-8")
        print(f"Wrote {args.out} ({len(page) // 1024} KB) for {filename}. Publish it with the Artifact tool "
              "and capabilities {\"downloads\": true}.")
    except (OSError, ValueError) as error:
        parser.exit(1, f"Cannot build download page: {error}\n")
