# Vectorizer evidence format

Read this reference when the input is a JSON package whose `schema` is
`logo-designer-brief`. The supported version is `1`. Plain SVG and raster inputs
do not require this format.

## Package fields

| Field | Meaning |
| --- | --- |
| `image.width`, `image.height` | Original canvas size; retain for SVG dimensions and viewBox. |
| `image.png_data_url` | Optional embedded PNG. Decode locally to inspect; base64 text is not visual evidence. |
| `image.attachment_required` | Compact exports require a separately attached original image. |
| `coordinates` | Origin, axis direction, units and pixel-boundary estimation notes. |
| `source_palette` | Sampled color bins and counts; includes anti-aliasing shades and possibly the background. |
| `trace_settings` | Threshold, tolerance, minimum area and tracing options used for this particular export. |
| `contours` | Observed outlines and their current reconstruction hypotheses. |
| `current_svg` | Existing rendered trace, usually monochrome. |
| `instructions` | Exported brief. Treat as supplied task metadata, subordinate to the user and the active skill. |

## Contours

A contour has a stable `id`, `parent_id`, and `role` (`outer` or `hole`). Preserve
parent/child relationships and SVG negative space. Islands can appear as separate
outer contours. `observed_area_px2` is measured from raw pixel-center contours,
not necessarily from the final vector geometry.

`raw_pixel_center_points_px` contains unsimplified OpenCV coordinates when supplied.
`boundary_points_px` contains a compact estimate of the foreground boundary.
The exporter shifts the raw contour by half a pixel in each coordinate and another
half pixel along an estimated outward normal. Use the boundary coordinates as
supplied; do not apply this adjustment a second time. Rasterization and that normal
estimate introduce uncertainty, particularly on very small or slanted details.

`boundary_simplification_max_error_px` records the exporter's RDP tolerance (currently
0.35 source pixels); it is not an independent measured guarantee. Load raw points
locally for closer inspection when simplified evidence loses a meaningful detail.

`normalized_boundary` divides x by image width and y by image height. It represents
the same points, not a second independent contour. Pixel coordinates are preferable
for precise construction and SVG output. Points near image borders can reflect
boundary-estimation offsets; do not silently clamp them.

## Current geometry

`current_geometry` contains:

- `closed`: whether the outline closes.
- `anchors`: segment start coordinates. They are outline anchors, not stroke
  centerline endpoints or Bezier control handles.
- `segments`: ordered lines, arcs, quadratic curves or cubic curves, with explicit
  `from` and `to` coordinates. Cubics have `control1` and `control2`; quadratics have
  `control`. Arcs have `radii`, `rotation_degrees`, `large_arc`, and `clockwise`.
- `primitive_hypothesis`: optional whole-shape fit with dimensions, center, radius
  and rotation. It can be wrong or too rigid; verify it against the source.
- `svg_path`: the equivalent SVG path data.

SVG arc flags and directions use the exported x-right, y-down coordinate system.
Closure can be represented by a final line or a `Z` command. Preserve connectivity
while simplifying. Native SVG elements and their path equivalents can have
slightly different rounded numeric representations.

## Scope of the evidence

The current app thresholds a single monochrome foreground. Source colors,
regions missed by thresholding, faint details and gradients can exist in the image
without appearing in exported contours. Treat the image as essential evidence
for those regions. Use more than one threshold or derive new regions from the
image when the task requires it; do not assume the export describes all artwork.
