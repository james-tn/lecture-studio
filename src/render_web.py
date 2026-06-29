"""Render stage (web): emit a self-contained whiteboard player into out/."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "src" / "player_template.html"


def render(score: dict, manifest: list[dict], out_dir: Path, autoplay: bool = False) -> Path:
    html = TEMPLATE.read_text()
    html = (html
            .replace("__TITLE__", score.get("meta", {}).get("title", "Math Lecture"))
            .replace("__SCORE__", json.dumps(score))
            .replace("__MANIFEST__", json.dumps(manifest))
            .replace("__AUTOPLAY__", "true" if autoplay else "false"))
    out = out_dir / "index.html"
    out.write_text(html)
    return out


if __name__ == "__main__":
    out_dir = ROOT / "out"
    score = json.loads((out_dir / "score.json").read_text())
    manifest = json.loads((out_dir / "audio_manifest.json").read_text())
    p = render(score, manifest, out_dir)
    print(f"wrote {p}")
