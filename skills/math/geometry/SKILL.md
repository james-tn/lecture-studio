---
name: geometry
description: Construct geometric figures, diagrams, and proofs with vector primitives. Use when teaching triangles, polygons, circles, angles, the Pythagorean theorem, congruence/similarity, transformations (translate/rotate/reflect/dilate), coordinate geometry, or area/perimeter.
license: Apache-2.0
metadata:
  subject: math
  version: "1.0"
---

# Geometry — figures, constructions, and proofs

## Idiomatic objects & actions
- Build diagrams with a **`figure`** object: a self-contained, uniformly-scaled drawing made of
  geometry `elements`. Give it an `id`, optional `xrange`/`yrange` (else auto-fit), and the
  `elements` list. Place it on the right; keep statements/equations on the left.
- Annotate the figure as a whole with `circle`/`box`/`pointer`; emphasize a side or angle by
  adding it as a colored element.

## Element kinds (in the figure's own coordinates)
- `point` — `{kind:"point", at:[x,y], label:"A"}`
- `segment` / `line` / `ray` — `{kind:"segment", from:[x,y], to:[x,y], ticks:1, label:"5"}`
  (`ticks` draws equal-length marks; `label` sits beside the side)
- `polygon` — `{kind:"polygon", points:[[x,y],…], fill:false, color:"blue"}`
- `circle` — `{kind:"circle", center:[x,y], r:2}`
- `arc` — `{kind:"arc", center:[x,y], r:2, start:0, end:90}` (degrees)
- `angle` — `{kind:"angle", at:[v], from:[p], to:[q], mark:"right"}` (`mark:"right"` draws the
  small square; otherwise an arc; add `label:"\\theta"` for a measure)
- `label` — `{kind:"label", at:[x,y], text:"hypotenuse"}`

## Worked example (right triangle, Pythagoras)
```json
{ "action": "figure", "id": "tri",
  "object": { "type": "figure",
    "elements": [
      { "kind": "polygon", "points": [[0,0],[4,0],[0,3]], "color": "blue" },
      { "kind": "point", "at": [0,0], "label": "C" },
      { "kind": "point", "at": [4,0], "label": "A" },
      { "kind": "point", "at": [0,3], "label": "B" },
      { "kind": "angle", "at": [0,0], "from": [4,0], "to": [0,3], "mark": "right" },
      { "kind": "segment", "from": [0,0], "to": [4,0], "label": "4" },
      { "kind": "segment", "from": [0,0], "to": [0,3], "label": "3" },
      { "kind": "segment", "from": [4,0], "to": [0,3], "label": "c", "color": "red" }
    ] },
  "pos": { "x": 70, "y": 42 } }
```

## Layout pattern
- Figure on the right (`x` ≈ 58..92, `y` ≈ 30), the statement/given/equations on the left.
- Default figure size ~380×330 board px; the renderer auto-fits and keeps it undistorted.

## Pitfalls
- Coordinates are the figure's own units (not board %); the renderer scales them uniformly.
- Say side/angle names in words ("side A B", "the right angle at C"); the figure shows the marks.
- For transformations, draw the pre-image, then a second `figure` (or `transform`) for the image.
