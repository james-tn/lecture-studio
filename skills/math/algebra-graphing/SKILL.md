---
name: algebra-graphing
description: Plot functions and build graphs from tables of values on a coordinate plane. Use when teaching how to graph a function, read a curve, find intercepts/slopes, apply graph transformations (shifts/stretches), or compare functions.
license: Apache-2.0
metadata:
  subject: math
  version: "1.0"
---

# Algebra — graphs, tables, and functions

## Idiomatic objects & actions
- Use a **`graph`** object for a realistic coordinate plane (grid, arrowed/numbered axes,
  labels, curve, and plotted points). Give it an `id`, an `xrange`/`yrange`, the `fns` to draw,
  and the table `points`.
- To teach a **table → graph** lesson: first `write` the function and the table; then create a
  `graph` (often with no `fns` yet, just axes), and `point` each row of the table onto it one at
  a time as you read the values; finally `plot` (add the curve) or include `fns` so the smooth
  curve connects the dots.
- `circle`/`pointer` a specific plotted point; `arrow` from a table row to its dot.

## The table → graph workflow
Render the table with a **`table`** object (never cram it into a `text` run). Use `headers`
for the column labels and `rows` for the values:
```json
{ "action": "table", "id": "tbl",
  "object": { "type": "table", "headers": ["x", "y"],
    "rows": [["-3","6"],["-2","0"],["-1","-4"],["0","-6"],["1","-6"],["2","-4"],["3","0"]] },
  "pos": { "x": 22, "y": 40 } }
```
Cells may be LaTeX (e.g. a header `"x^3-2"`) or plain numbers. For a wide "x-row / y-row"
layout, make two rows whose first cell is the label: `["x","-3","-2",…]`, `["y","6","0",…]`.

## The `graph` object
```json
{ "type": "graph", "xrange": [-4, 4], "yrange": [-10, 8], "grid": true,
  "xlabel": "x", "ylabel": "y",
  "fns": [{ "expr": "x^3 - 2", "color": "red" }],
  "points": [
    {"x": -2, "y": -10, "color": "red"}, {"x": -1, "y": -3}, {"x": 0, "y": -2},
    {"x": 1, "y": -1}, {"x": 2, "y": 6}
  ] }
```
`expr` is a function of x in JS math (use `^` or `**`, and `sqrt`, `sin`, `abs`, …). Pick
`xrange`/`yrange` so the interesting behavior and all table points are visible.

## Live table-plotting (the engaging way)
```json
{ "say": "When x is negative two, y is negative ten — way down here.",
  "cues": [
    { "at": 0.2, "anchor": "negative two", "action": "circle", "target": "tbl", "part": "-2" },
    { "at": 0.7, "anchor": "down here", "action": "point", "target": "graph1",
      "gx": -2, "gy": -10, "style": { "color": "red" } }
  ] }
```
Create the graph once (`"action":"graph"` with just axes / `"fns":[]`), then add `point` cues
referencing it; `plot`/`transform` in the smooth curve at the end.

## Layout pattern
- Function + table on the left (`x` ≈ 8..45), the graph on the right (`x` ≈ 55..92, `y` ≈ 30).
- The renderer auto-sizes graphs to a readable size — omit `w`/`h` (the camera follows the graph).

## Pitfalls
- Always say coordinates in words ("the point negative two, negative ten"); the board shows them.
- Keep ranges tight — don't plot a huge domain; show the shape near the origin and the table points.
- Plot the points before drawing the curve, so students see the curve *explains* the dots.
