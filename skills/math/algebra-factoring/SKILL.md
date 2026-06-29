---
name: algebra-factoring
description: Factor polynomials and manipulate or solve algebraic equations on a whiteboard with step-by-step transforms. Use when teaching factoring, difference/sum-of-squares patterns, completing the square, isolating variables, or simplifying expressions.
license: Apache-2.0
metadata:
  subject: math
  version: "1.0"
---

# Algebra — factoring & equation manipulation

## Idiomatic objects & actions
- Equations as `math` objects, one per line, flowing downward.
- `transform` to show each algebra step morphing the previous line (keep the same `id`
  lineage by transforming, or write a new line below and `arrow` from the old one).
- `circle` / `highlight` the term currently being manipulated, using `part` to target a
  sub-expression (e.g. `part: "9"`, `part: "x^2"`).
- `brace` under a group of terms you're about to combine; `arrow` with a `label` to annotate
  a move ("take the square root", "factor out 2").

## Layout pattern
- Title at `y` ≈ 8. First expression at `y` ≈ 25, centered around `x` ≈ 40.
- Each subsequent line `below` the previous, or step `right_of` when showing a parallel form.
- Keep a "scratch" column on the right (`x` ≈ 70) for side-notes like `9 = 3^2`.

## Worked example (difference of squares)
```json
{
  "say": "So x squared minus nine factors as x minus three, times x plus three.",
  "cues": [
    { "at": 0.05, "action": "highlight", "target": "eq1", "style": { "color": "yellow" } },
    { "at": 0.4, "action": "write", "id": "eq2",
      "object": { "type": "math", "tex": "(x-3)(x+3)" }, "below": "eq1" },
    { "at": 0.5, "action": "arrow",
      "from_anchor": { "ref": "eq1", "side": "bottom" },
      "to_anchor":   { "ref": "eq2", "side": "top" }, "label": "= " },
    { "at": 0.85, "action": "circle", "target": "eq2", "style": { "color": "green" } }
  ]
}
```

## Pitfalls
- Don't cram multiple algebra steps into one beat — one move per beat reads far better.
- When you "take the square root of both sides", say it in words and show the ± explicitly.
- Use `transform` for genuine morphs (a→b); use a new line + `arrow` when you want both
  the before and after visible at once (usually the better teaching choice).
