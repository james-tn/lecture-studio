---
name: calculus-derivatives
description: Teach derivatives and rates of change with tangent lines and graph relationships. Use when covering the definition of the derivative, slopes/tangent lines, differentiation rules, relating a function's graph to its rate of change, or optimization intuition.
license: Apache-2.0
metadata:
  subject: math
  version: "1.0"
---

# Calculus — derivatives & graphs

## Idiomatic objects & actions
- `plot` the function on axes; keep `domain` tight around the interesting region.
- `draw` a `line` for a secant/tangent; `transform` or `move` the second point toward the
  first to animate "the secant becoming the tangent".
- `pointer`/`pulse` a point on the curve while you talk about it.
- `math` lines for the limit definition; `circle` the `\\Delta x` term; `transform` the
  difference quotient into the derivative as `\\Delta x \\to 0`.
- `arrow` with `label` from a feature of the graph to its meaning ("slope = velocity").

## Layout pattern
- Graph on the left half (`x` ≈ 12..52). Symbolic work on the right half (`x` ≈ 58..92).
- Title across the top. Keep the limit-definition stack in the right column.

## Worked example (secant → tangent)
```json
{
  "say": "As the second point slides toward the first, the secant becomes the tangent.",
  "tone": "builds anticipation, settles on 'tangent'",
  "cues": [
    { "at": 0.0, "action": "pulse", "target": "ptB" },
    { "at": 0.2, "action": "move", "target": "ptB", "pos": { "x": 30, "y": 55 } },
    { "at": 0.6, "action": "transform", "target": "secant",
      "to": { "type": "shape", "kind": "line" } },
    { "at": 0.85, "action": "arrow",
      "from_anchor": { "x": 35, "y": 40 }, "to_anchor": { "ref": "tangent", "side": "center" },
      "label": "slope = f'(a)" }
  ]
}
```

## Pitfalls
- Always say limits in words ("the limit as delta x goes to zero"); never read raw notation.
- Don't over-plot: one clean curve plus one moving line is enough to carry the idea.
- Keep axis ranges fixed across beats so the viewer isn't disoriented.
