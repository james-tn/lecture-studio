---
name: cell-biology
description: Build labeled cell schematics and reveal each part as you narrate. Use when teaching cell structure and organelles, plant vs. animal vs. prokaryotic cell differences, membrane transport, or the cell cycle (mitosis/meiosis).
license: Apache-2.0
metadata:
  subject: biology
  version: "1.0"
---

# Biology — cell biology

## How to teach biology visually (important)
Biology is **diagram-driven**, not formula-driven. Build a clear **schematic** with the vector
primitives — you place every part, so labels line up perfectly — then **reveal and label it
part by part** as you narrate. Two tools:
- a **`figure`** (or `diagram`) to draw the schematic (e.g., a cell as nested ellipses), and
- **`callout`** cues to label each part with a leader line as you mention it, and **`region`**
  to highlight an area.
- If you need a photo-realistic base picture, use an **`image`** object with a `prompt`
  (the asset stage generates it) and then `callout`/`region` over it with normalized `spot`
  coordinates. Prefer the schematic for parts you must label precisely.

## Idiomatic pattern — label a cell part by part
```json
[
  { "id": "b1", "say": "Here is an animal cell.",
    "cues": [ { "at": 0.3, "action": "figure", "id": "cell",
      "object": { "type": "figure", "elements": [
        { "kind": "circle", "center": [0,0], "r": 5, "color": "blue" },
        { "kind": "circle", "center": [1,0.5], "r": 1.6, "color": "purple" },
        { "kind": "circle", "center": [-2,-1.5], "r": 0.9, "color": "green" },
        { "kind": "circle", "center": [2.5,-1.8], "r": 0.8, "color": "green" }
      ] }, "pos": { "x": 64, "y": 46 } } ] },
  { "id": "b2", "say": "The nucleus, in the center, holds the DNA.",
    "cues": [ { "at": 0.3, "anchor": "nucleus", "action": "callout", "target": "cell",
                "spot": [0.62,0.42], "text": "Nucleus", "side": "right" } ] },
  { "id": "b3", "say": "These small structures are the mitochondria, the powerhouses.",
    "cues": [ { "at": 0.4, "anchor": "mitochondria", "action": "callout", "target": "cell",
                "spot": [0.25,0.7], "text": "Mitochondrion", "side": "left", "style": {"color":"green"} } ] }
]
```
`spot` is normalized `[x,y]` (0..1) within the target's box, so callouts land on the right part.

## Layout pattern
- Diagram on the right (`x` ≈ 60..90), the term/definition list building on the left.
- One organelle per beat: say its name, `callout` it, then its function.

## Pitfalls
- Don't dump every label at once — reveal one per beat (that's the teaching value).
- Keep the schematic simple and clearly separated so callouts don't overlap.
- Say structure names in words; the diagram carries the labels.
