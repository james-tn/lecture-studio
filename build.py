#!/usr/bin/env python3
"""End-to-end build: source material -> synced voice + whiteboard lecture.

    python build.py examples/factoring.input.md            # author + tts + web player
    python build.py examples/factoring.input.md --mp4      # also export an MP4
    python build.py --skip-author --mp4                     # reuse out/score.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

import author as author_mod   # noqa: E402
import tts as tts_mod         # noqa: E402
import render_web             # noqa: E402
import ingest                 # noqa: E402

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("material", nargs="?", help="path to source material (txt/md)")
    ap.add_argument("--skip-author", action="store_true", help="reuse out/score.json")
    ap.add_argument("--skip-tts", action="store_true", help="reuse existing out/audio")
    ap.add_argument("--mp4", action="store_true", help="also export lecture.mp4 (web capture)")
    ap.add_argument("--tts", choices=["speech", "openai"], default="speech",
                    help="TTS backend (speech = Azure Speech word-level sync; default)")
    ap.add_argument("--flow", choices=["page", "scroll"],
                    help="override board flow: 'page' (wipe) or 'scroll' (endless whiteboard)")
    ap.add_argument("--no-vision", action="store_true",
                    help="(deprecated) same as --ingest-mode text")
    ap.add_argument("--ingest-mode", choices=["image", "vision", "text"], default="image",
                    dest="ingest_mode",
                    help="how to read material: image=send straight to brain (best for graphs); "
                         "vision=transcribe to markdown; text=plain extraction")
    ap.add_argument("--guidance", help="teacher instructions steering pace/emphasis/tone/depth")
    ap.add_argument("--subject", help="subject to scope skills (math, biology, …); default = all")
    ap.add_argument("--skills", nargs="*", help="force specific skill packs")
    args = ap.parse_args()
    if args.no_vision:
        args.ingest_mode = "text"
    OUT.mkdir(exist_ok=True)

    if args.skip_author:
        score = json.loads((OUT / "score.json").read_text())
        print(f"[author] reused score.json ({len(score['beats'])} beats)")
    else:
        if not args.material:
            ap.error("material is required unless --skip-author")
        if args.ingest_mode == "image" and ingest.is_visual(args.material):
            images = ingest.load_images(args.material)
            print(f"[ingest] {args.material} -> {len(images)} image(s) sent directly to gpt-5.4")
            print("[author] generating Lecture Score with gpt-5.4 (vision) …")
            score = author_mod.author("", skills=args.skills, guidance=args.guidance, images=images, subject=args.subject)
        else:
            material = ingest.load_material(args.material, use_vision=(args.ingest_mode != "text"))
            print(f"[ingest] {args.material} ({args.ingest_mode}) -> {len(material)} chars")
            print("[author] generating Lecture Score with gpt-5.4 …")
            score = author_mod.author(material, skills=args.skills, guidance=args.guidance, subject=args.subject)
        (OUT / "score.json").write_text(json.dumps(score, indent=2))
        print(f"[author] wrote score.json ({len(score['beats'])} beats)")

    if args.skip_tts:
        manifest = json.loads((OUT / "audio_manifest.json").read_text())
        print(f"[tts] reused {len(manifest)} clips")
    else:
        print(f"[tts] synthesizing narration ({args.tts}) …")
        manifest = tts_mod.synthesize(score, OUT, backend=args.tts)
        nmarks = sum(len(m.get("marks", {})) for m in manifest)
        print(f"[tts] wrote {len(manifest)} clips ({nmarks} word-anchored cues)")

    if args.flow:
        score.setdefault("board", {})["flow"] = args.flow
        (OUT / "score.json").write_text(json.dumps(score, indent=2))
        print(f"[flow] board flow = {args.flow}")

    page = render_web.render(score, manifest, OUT)
    print(f"[render] wrote {page}")

    if args.mp4:
        import export_mp4
        print("[mp4] capturing + muxing …")
        mp4 = export_mp4.export(OUT)
        print(f"[mp4] wrote {mp4} ({mp4.stat().st_size // 1024} KB)")

    print(f"\nDone. Open: {page}")
    if args.mp4:
        print(f"Video: {OUT / 'lecture.mp4'}")


if __name__ == "__main__":
    main()
