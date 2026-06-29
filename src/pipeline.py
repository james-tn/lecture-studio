"""Reusable end-to-end pipeline: material -> Lecture Score -> narration -> web player.

Used by both build.py (CLI) and server.py (teacher UI). Stages report through an optional
`progress(stage, message)` callback so a UI can show live status.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import author as author_mod
import ingest
import render_web
import tts as tts_mod

Progress = Callable[[str, str], None]


_IMAGE_GEN_AVAILABLE: bool | None = None   # None=unknown, False=confirmed unavailable this process


def _looks_unconfigured(err: str) -> bool:
    e = err.lower()
    return any(k in e for k in ("unknown model", "model_not_found", "not found", "404",
                                "deployment", "does not exist", "no deployment"))


def _generate_assets(score: dict, out_dir: Path, say,
                     source_images: list | None = None) -> None:
    """Fill `src` for image objects that only carry a `prompt`, via the image model.

    Generations run in parallel (each gpt-image call is ~independent and ~50s). Best effort and
    fully optional: if no image model is deployed, the lecture renders fine with labeled
    placeholders — we detect that and skip cleanly.

    If the teacher uploaded a single focused figure, it is passed as a reference for
    image-to-image so the generated visual stays faithful to the source.
    """
    global _IMAGE_GEN_AVAILABLE
    import azure_client
    from concurrent.futures import ThreadPoolExecutor
    targets = [c.get("object") for b in score["beats"] for c in b.get("cues", [])
               if (c.get("object") or {}).get("type") == "image"
               and (c["object"].get("prompt") and not c["object"].get("src"))]
    if not targets:
        return
    if _IMAGE_GEN_AVAILABLE is False:                 # already known: don't retry / spam
        say("assets", f"image model not deployed — drawing labeled placeholders for "
                      f"{len(targets)} diagram(s)")
        (out_dir / "score.json").write_text(json.dumps(score, indent=2))
        return
    # ground on the source only when it's a single focused figure (avoid noisy multi-page refs)
    refs = source_images if (source_images and len(source_images) == 1) else None
    backend = "gpt-image" if refs else getattr(azure_client, "IMAGE_BACKEND", "flux")
    rpm = max(1, azure_client.rpm_for(backend))
    est = "" if len(targets) <= rpm else f" (limited to {rpm}/min — ~{-(-len(targets)//rpm)} min)"
    say("assets", f"generating {len(targets)} diagram image(s) [{backend}]"
                  + (" (grounded on your figure)" if refs else "")
                  + (" in parallel" if len(targets) > 1 else "") + est + " …")

    def gen(o):
        try:
            o["src"] = azure_client.generate_image(o["prompt"], images=refs)
            return None
        except Exception as e:
            return str(e)

    # concurrency matches the per-minute quota; the client's rate limiter paces the rest
    with ThreadPoolExecutor(max_workers=min(rpm, len(targets))) as ex:
        errs = [e for e in ex.map(gen, targets) if e]
    ok = len(targets) - len(errs)
    if ok:
        _IMAGE_GEN_AVAILABLE = True
        say("assets", f"{ok}/{len(targets)} image(s) generated")
    if errs:
        if ok == 0 and any(_looks_unconfigured(e) for e in errs):
            _IMAGE_GEN_AVAILABLE = False
            say("assets", "no image model reachable on this endpoint — drawing labeled "
                          "placeholders for generated diagrams (check AZURE_IMAGE_BACKEND)")
        else:
            say("assets", f"{len(errs)} image(s) skipped: {errs[0][:80]}")
    (out_dir / "score.json").write_text(json.dumps(score, indent=2))


def _finalize(score: dict, out_dir: Path, *, voice: str | None, flow: str | None,
              tts_backend: str, say, source_images: list | None = None) -> dict:
    """Shared tail: apply voice/flow, write score, generate assets, synthesize, render."""
    if voice:
        score.setdefault("meta", {}).setdefault("voice", {})["name"] = voice
    if flow:
        score.setdefault("board", {})["flow"] = flow
    (out_dir / "score.json").write_text(json.dumps(score, indent=2))

    # asset stage: fill `src` for any image objects that only have a `prompt` (best effort)
    _generate_assets(score, out_dir, say, source_images=source_images)

    say("tts", f"synthesizing narration ({tts_backend}) …")
    manifest = tts_mod.synthesize(score, out_dir, backend=tts_backend)
    nmarks = sum(len(m.get("marks", {})) for m in manifest)
    say("tts", f"{len(manifest)} clips, {nmarks} word-anchored cues")

    say("render", "building the whiteboard player …")
    render_web.render(score, manifest, out_dir)
    say("done", "lecture ready")
    return manifest


def run(material_path: str | Path, out_dir: Path, *, subject: str | None = None,
        guidance: str | None = None, voice: str | None = None, tts_backend: str = "speech",
        flow: str = "page", ingest_mode: str = "image", max_tokens: int | None = None,
        author_mode: str = "single", progress: Progress | None = None) -> dict:
    """ingest_mode: 'image' (send image/PDF straight to the brain — best for graphs),
    'vision' (transcribe to markdown first), or 'text' (plain extraction).
    author_mode: 'single' (one-shot authoring) or '2stage' (outline -> parallel section elaboration).

    Returns {score, manifest, messages, out_dir}. `messages` is the authoring chat history,
    used by `refine()` to fine-tune the lecture from teacher feedback (None in 2stage mode)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    def say(stage, msg):
        if progress:
            progress(stage, msg)
        print(f"[{stage}] {msg}")

    images = None
    material = ""
    visual = ingest.is_visual(material_path)
    if ingest_mode == "image" and visual:
        say("ingest", f"sending {Path(material_path).name} straight to the model (vision) …")
        images = ingest.load_images(material_path)
        say("ingest", f"{len(images)} image(s) → brain (graphs/diagrams preserved)")
    else:
        use_vision = (ingest_mode != "text")
        say("ingest", f"reading {Path(material_path).name} "
                      f"({'vision' if use_vision else 'text'}) …")
        material = ingest.load_material(material_path, use_vision=use_vision)
        say("ingest", f"extracted {len(material)} chars")

    kw = {"max_tokens": max_tokens} if max_tokens else {}
    sections = None
    if author_mode == "2stage":
        say("author", "gpt-5.4 (2-stage): planning the outline …")
        score, sections = author_mod.author_2stage(material, images=images, subject=subject,
                                                   guidance=guidance, flow=flow, progress=progress,
                                                   return_sections=True, **kw)
        messages = None
    else:
        say("author", "gpt-5.4 is writing the lecture script …")
        score, messages = author_mod.author(material, guidance=guidance, images=images,
                                            subject=subject, return_messages=True, **kw)
    say("author", f"{len(score['beats'])} beats")

    manifest = _finalize(score, out_dir, voice=voice, flow=flow,
                         tts_backend=tts_backend, say=say, source_images=images)
    return {"score": score, "manifest": manifest, "messages": messages,
            "sections": sections, "out_dir": str(out_dir)}


def plan_outline(material_path: str | Path, *, subject: str | None = None,
                 guidance: str | None = None, ingest_mode: str = "image",
                 progress: Progress | None = None) -> dict:
    """Stage 1 only: ingest + outline. Returns the cheap plan for the storyboard review."""
    def say(stage, msg):
        if progress:
            progress(stage, msg)
        print(f"[{stage}] {msg}")

    images, material = None, ""
    if ingest_mode == "image" and ingest.is_visual(material_path):
        say("ingest", f"reading {Path(material_path).name} (vision) …")
        images = ingest.load_images(material_path)
        say("ingest", f"{len(images)} image(s) → planner")
    else:
        use_vision = (ingest_mode != "text")
        material = ingest.load_material(material_path, use_vision=use_vision)
        say("ingest", f"extracted {len(material)} chars")
    say("author", "gpt-5.4 is planning the outline …")
    plan = author_mod.outline(material, images=images, subject=subject, guidance=guidance)
    say("done", f"outline ready: {len(plan.get('sections', []))} section(s)")
    return plan


def run_from_outline(plan: dict, out_dir: Path, *, subject: str | None = None,
                     guidance: str | None = None, voice: str | None = None,
                     tts_backend: str = "speech", flow: str = "page",
                     max_tokens: int | None = None, progress: Progress | None = None) -> dict:
    """Stage 2+3 from a (possibly teacher-edited) outline: elaborate -> finalize -> render."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    def say(stage, msg):
        if progress:
            progress(stage, msg)
        print(f"[{stage}] {msg}")

    kw = {"max_tokens": max_tokens} if max_tokens else {}
    say("author", "gpt-5.4 is drawing each section from your outline …")
    score, sections = author_mod.elaborate_outline(plan, subject=subject, guidance=guidance,
                                                   flow=flow, progress=progress,
                                                   return_sections=True, **kw)
    say("author", f"{len(score['beats'])} beats")
    manifest = _finalize(score, out_dir, voice=voice, flow=flow,
                         tts_backend=tts_backend, say=say)
    return {"score": score, "manifest": manifest, "messages": None,
            "sections": sections, "out_dir": str(out_dir)}


def agent_refine(out_dir: Path, score: dict, sections: list[dict], message: str, *,
                 images: list[str] | None = None, subject: str | None = None,
                 voice: str | None = None, flow: str = "page", tts_backend: str = "speech",
                 prev_response_id: str | None = None, max_tokens: int | None = None,
                 progress: Progress | None = None) -> dict:
    """Stage-2 conversational editing: a multimodal agent decides which section(s) a request
    (text and/or screenshot) affects, edits them, re-merges and re-renders. Only changed beats are
    re-synthesized (TTS content cache). Returns {score, sections, manifest, reply, response_id,
    edited, out_dir}."""
    out_dir = Path(out_dir)

    def say(stage, msg):
        if progress:
            progress(stage, msg)
        print(f"[{stage}] {msg}")

    say("author", "thinking about your edit …")
    kw = {"max_tokens": max_tokens} if max_tokens else {}
    res = author_mod.agent_edit(score, sections, message, images=images, subject=subject,
                                flow=flow, prev_response_id=prev_response_id, progress=progress, **kw)
    if not res["edited"]:                       # agent asked a question / no change — don't re-render
        say("done", "no changes applied")
        return {"score": score, "sections": sections, "manifest": None, "reply": res["reply"],
                "response_id": res["response_id"], "edited": [], "out_dir": str(out_dir)}

    new_score = res["score"]
    say("author", f"edited {len(res['edited'])} section(s); re-rendering …")
    manifest = _finalize(new_score, out_dir, voice=voice, flow=flow, tts_backend=tts_backend, say=say)
    return {"score": new_score, "sections": res["sections"], "manifest": manifest,
            "reply": res["reply"], "response_id": res["response_id"], "edited": res["edited"],
            "out_dir": str(out_dir)}


def refine(out_dir: Path, messages: list[dict] | None, prev_score: dict, feedback: str, *,
           subject: str | None = None, voice: str | None = None, flow: str | None = None,
           tts_backend: str = "speech", max_tokens: int | None = None,
           progress: Progress | None = None) -> dict:
    """Apply teacher feedback to an existing lecture and re-render. Re-uses ingestion/authoring
    context from `messages`; only changed beats are re-synthesized (TTS content cache)."""
    out_dir = Path(out_dir)

    def say(stage, msg):
        if progress:
            progress(stage, msg)
        print(f"[{stage}] {msg}")

    say("author", "gpt-5.4 is revising the lecture from your feedback …")
    kw = {"max_tokens": max_tokens} if max_tokens else {}
    if messages:
        score, messages = author_mod.refine(messages, feedback, prev_score=prev_score,
                                            subject=subject, **kw)
    else:
        # 2-stage (or restored) job: no prior conversation — refine from the current score
        score, messages = author_mod.refine_from_score(prev_score, feedback,
                                                       subject=subject, **kw)
    say("author", f"{len(score['beats'])} beats (revised)")

    manifest = _finalize(score, out_dir, voice=voice, flow=flow,
                         tts_backend=tts_backend, say=say)
    return {"score": score, "manifest": manifest, "messages": messages,
            "out_dir": str(out_dir)}
