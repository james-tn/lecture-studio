---
name: enzymes-kinetics
description: Plot Michaelis-Menten and enzyme-activity curves with reference lines. Use when teaching enzyme activity, enzyme kinetics, Vmax and KM, competitive vs. noncompetitive inhibition, or temperature effects (Q10).
license: Apache-2.0
metadata:
  subject: biology
  version: "1.0"
---

# Biology — enzymes & kinetics

## Idiomatic objects & actions
- The **Michaelis–Menten curve** is a `graph` of velocity vs. substrate concentration with the
  function `V = Vmax*x/(KM+x)`. Use the graph's **`hlines`** for the `Vmax` and `½Vmax`
  asymptotes and a **`vline`** at `KM` (all dashed, labeled). Label axes `xlabel:"[S]"`,
  `ylabel:"V"`.
- **Inhibitor comparison:** one `graph` with two `fns` — no inhibitor `Vmax*x/(KM+x)` and
  competitive `Vmax*x/(KM2+x)` (same Vmax, larger KM); noncompetitive lowers Vmax. Add a
  `vline` at each KM (color-matched) and `callout` each curve.
- **Enzyme/substrate schematics** (active site, competitive vs. allosteric binding): a `figure`
  with a `polygon` enzyme that has a notch (active site) + a small `polygon` substrate/inhibitor,
  an `arrow` for binding, and `callout` labels. (Or an `image` for a realistic picture.)
- **Practice data** (rate vs. temperature): a `graph` with `points` and `connect:true` to join
  them, plus a `table` of the values; show the `Q10` formula as `math`.

## The Michaelis–Menten graph
```json
{ "action": "graph", "id": "mm",
  "object": { "type": "graph", "xrange": [0,10], "yrange": [0,12], "grid": false,
    "xlabel": "[S]", "ylabel": "V",
    "fns": [{ "expr": "10*x/(2+x)", "color": "red" }],
    "hlines": [ {"y":10,"label":"Vmax"}, {"y":5,"label":"½Vmax"} ],
    "vlines": [ {"x":2,"label":"KM","color":"blue"} ] },
  "pos": { "x": 64, "y": 46 } }
```
Here Vmax = 10 and KM = 2 (the curve passes through ½Vmax = 5 at x = KM).

## Q10
`Q10 = (K2/K1)^{10/(T2-T1)}` — write it with `math`; build the rate–temperature graph with
`points` + `connect:true`; put the data in a `table`.

## Pitfalls
- Pick `Vmax`/`KM` so the curve clearly saturates within `yrange` and ½Vmax sits at x = KM.
- For inhibitors: **competitive** raises KM (curve shifts right, same Vmax); **noncompetitive**
  lowers Vmax (lower plateau). Keep both curves on one graph for the contrast.
