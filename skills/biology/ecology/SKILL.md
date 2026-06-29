---
name: ecology
description: Draw food chains, food webs, energy flow, and biogeochemical cycles as flow diagrams. Use when teaching trophic levels, the water/carbon/nitrogen cycles, or processes like photosynthesis and cellular respiration shown as a flow.
license: Apache-2.0
metadata:
  subject: biology
  version: "1.0"
---

# Biology — ecology & cycles

## Idiomatic objects & actions
- Use a **`diagram`** object (nodes + edges) for anything that is "boxes connected by arrows":
  food webs, energy flow, and **cycles**. Set `layout:"cycle"` for the water/carbon/nitrogen
  cycle (nodes auto-arrange in a ring); use explicit node `at` positions for a food web.
- `edges` carry the direction (`arrow`) and an optional `label` (e.g. "eaten by", "evaporation").
- Reveal the story by narrating each arrow in turn; `circle`/`pointer` a node you're discussing;
  `callout` extra notes.

## The `diagram` object
```json
{ "action": "diagram", "id": "cyc",
  "object": { "type": "diagram", "layout": "cycle",
    "nodes": [
      { "id": "ocean", "text": "Ocean", "color": "blue" },
      { "id": "vapor", "text": "Water vapor" },
      { "id": "clouds", "text": "Clouds" },
      { "id": "rain", "text": "Precipitation" },
      { "id": "ground", "text": "Rivers & soil", "color": "green" }
    ],
    "edges": [
      { "from": "ocean", "to": "vapor", "label": "evaporation" },
      { "from": "vapor", "to": "clouds", "label": "condensation" },
      { "from": "clouds", "to": "rain" },
      { "from": "rain", "to": "ground" },
      { "from": "ground", "to": "ocean", "label": "runoff" }
    ] },
  "pos": { "x": 60, "y": 48 } }
```
For a **food web**, give nodes explicit `at` positions (producers low, predators high) and draw
"eaten by" arrows upward. For an **energy pyramid**, stack labeled `figure` rectangles.

## Layout pattern
- The web/cycle diagram fills the right 2/3; keep a short key or definitions on the left.
- Producers/sun at the bottom, consumers above (energy flows up).

## Pitfalls
- Arrows in a food web point **from prey to predator** (energy flow), not the other way.
- Don't overcrowd — 5–8 nodes reads best; split large webs across pages with `clear`.
