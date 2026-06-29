"""TTS stage. Default backend = Azure Speech (word-level sync via SSML bookmarks
and word-boundary events). Fallback backend = OpenAI gpt-4o-mini-tts.

Each cue may carry an `anchor` (a word/phrase in the beat's `say`). We insert an SSML
<bookmark> at that word and record its exact audio offset, so the renderer fires the cue
on the spoken word instead of on a proportional `at` estimate.
"""
from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from xml.sax.saxutils import escape

import azure_client

# OpenAI TTS voices (fallback backend)
OPENAI_VOICES = {"alloy", "ash", "ballad", "coral", "echo", "fable",
                 "onyx", "nova", "sage", "shimmer", "verse"}
DEFAULT_SPEECH_VOICE = "en-US-AvaMultilingualNeural"


# ---------------- SSML / bookmark construction ----------------
def _insert_bookmarks(say: str, cues: list[dict]) -> str:
    """Return XML-escaped narration with <bookmark mark="i"/> before each cue's anchor."""
    low = say.lower()
    insertions, used = [], set()
    for i, c in enumerate(cues):
        anc = c.get("anchor")
        if not anc:
            continue
        start = 0
        while True:
            idx = low.find(anc.lower(), start)
            if idx < 0:
                break
            if idx in used:
                start = idx + 1
                continue
            used.add(idx)
            insertions.append((idx, i))
            break
    insertions.sort()
    out, prev = [], 0
    for pos, i in insertions:
        out.append(escape(say[prev:pos]))
        out.append(f'<bookmark mark="{i}"/>')
        prev = pos
    out.append(escape(say[prev:]))
    return "".join(out)


def _ssml(say: str, cues: list[dict], voice: str, style: str | None = None) -> str:
    body = _insert_bookmarks(say, cues)
    if style:
        body = (f'<mstts:express-as style="{style}">{body}</mstts:express-as>')
    return ('<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
            'xmlns:mstts="http://www.w3.org/2001/mstts" xml:lang="en-US">'
            f'<voice name="{voice}">{body}</voice></speak>')


def _speech_voice(score: dict) -> str:
    name = (score.get("meta", {}).get("voice") or {}).get("name", "")
    # Accept standard Neural voices and MAI-Voice models (e.g. en-US-Harper:MAI-Voice-2).
    return name if ("Neural" in name or "MAI-Voice" in name) else DEFAULT_SPEECH_VOICE


def _is_mai(voice: str) -> bool:
    return "MAI-Voice" in voice


# ---------------- backends ----------------
def _synthesize_speech(score: dict, audio_dir: Path, beats: list[dict],
                       keys: dict[str, str]) -> list[dict]:
    import azure.cognitiveservices.speech as speechsdk
    voice = _speech_voice(score)
    style = (score.get("meta", {}).get("voice") or {}).get("style")

    def render_one(beat: dict) -> dict:
        token, region = azure_client.speech_auth()
        cfg = speechsdk.SpeechConfig(auth_token=token, region=region)
        cfg.set_speech_synthesis_output_format(
            speechsdk.SpeechSynthesisOutputFormat.Audio24Khz48KBitRateMonoMp3)
        path = audio_dir / f"{keys[beat['id']]}.mp3"
        audio_out = speechsdk.audio.AudioOutputConfig(filename=str(path))
        synth = speechsdk.SpeechSynthesizer(speech_config=cfg, audio_config=audio_out)

        marks: dict[str, float] = {}
        words: list = []
        synth.bookmark_reached.connect(
            lambda e: marks.__setitem__(e.text, round(e.audio_offset / 1e7, 3)))

        def on_word(e):
            if e.boundary_type == speechsdk.SpeechSynthesisBoundaryType.Word:
                words.append([e.text, round(e.audio_offset / 1e7, 3)])
        synth.synthesis_word_boundary.connect(on_word)

        cues = beat.get("cues", [])
        # MAI-Voice models don't emit word/bookmark events -> cues use the `at` fallback.
        r = synth.speak_ssml_async(_ssml(beat["say"], cues, voice, style)).get()
        if r.reason != speechsdk.ResultReason.SynthesizingAudioCompleted:
            det = getattr(r, "cancellation_details", None)
            raise RuntimeError(f"speech failed for {beat['id']}: "
                               f"{det.error_details if det else r.reason}")
        dur = r.audio_duration.total_seconds() if r.audio_duration else None
        return {"id": beat["id"], "file": f"audio/{path.name}", "key": keys[beat["id"]],
                "hold": beat.get("hold", 0), "dur": dur, "marks": marks, "words": words}

    with ThreadPoolExecutor(max_workers=4) as ex:
        return list(ex.map(render_one, beats))


def _synthesize_openai(score: dict, audio_dir: Path, beats: list[dict],
                       keys: dict[str, str]) -> list[dict]:
    voice_cfg = (score.get("meta", {}).get("voice") or {})
    voice = voice_cfg.get("name", "sage")
    if voice not in OPENAI_VOICES:
        voice = "sage"
    persona = voice_cfg.get("persona")

    def render_one(beat: dict) -> dict:
        instr = " ".join(x for x in [persona, beat.get("tone"),
                 "Math lesson narration; enunciate clearly with natural teaching pace."] if x)
        data = azure_client.tts(beat["say"], voice=voice, instructions=instr)
        path = audio_dir / f"{keys[beat['id']]}.mp3"
        path.write_bytes(data)
        return {"id": beat["id"], "file": f"audio/{path.name}", "key": keys[beat["id"]],
                "hold": beat.get("hold", 0), "dur": None, "marks": {}, "words": []}

    with ThreadPoolExecutor(max_workers=6) as ex:
        return list(ex.map(render_one, beats))


def _beat_key(beat: dict, voice: str, style: str | None, backend: str) -> str:
    """Content hash for a beat's audio: identical (backend, voice, style, say, anchors) ->
    identical clip + marks, so refinements only re-synthesize beats that actually changed."""
    anchors = [c.get("anchor") for c in beat.get("cues", [])]
    payload = json.dumps([backend, voice, style or "", beat.get("say", ""), anchors],
                         ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def synthesize(score: dict, out_dir: Path, backend: str = "speech") -> list[dict]:
    audio_dir = out_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    beats = score["beats"]

    # resolve voice/style the same way each backend does, so keys are stable
    if backend == "speech":
        voice = _speech_voice(score)
        style = (score.get("meta", {}).get("voice") or {}).get("style")
    else:
        voice = (score.get("meta", {}).get("voice") or {}).get("name", "sage")
        if voice not in OPENAI_VOICES:
            voice = "sage"
        style = None
    keys = {b["id"]: _beat_key(b, voice, style, backend) for b in beats}

    # reuse clips from a prior manifest in this out_dir (refinement cache)
    prior: dict[str, dict] = {}
    mpath = out_dir / "audio_manifest.json"
    if mpath.exists():
        try:
            for e in json.loads(mpath.read_text()):
                if e.get("key") and (audio_dir / Path(e["file"]).name).exists():
                    prior[e["key"]] = e
        except Exception:
            prior = {}

    reused: dict[str, dict] = {}
    to_render: list[dict] = []
    for b in beats:
        k = keys[b["id"]]
        if k in prior:
            e = dict(prior[k])
            e["id"], e["hold"], e["key"] = b["id"], b.get("hold", 0), k
            reused[b["id"]] = e
        else:
            to_render.append(b)

    fn = _synthesize_speech if backend == "speech" else _synthesize_openai
    rendered = fn(score, audio_dir, to_render, keys) if to_render else []
    by_id = {**reused, **{e["id"]: e for e in rendered}}
    manifest = [by_id[b["id"]] for b in beats]
    print(f"  tts: {len(reused)} reused, {len(to_render)} synthesized")
    (out_dir / "audio_manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


if __name__ == "__main__":
    import sys
    root = Path(__file__).resolve().parent.parent
    backend = sys.argv[1] if len(sys.argv) > 1 else "speech"
    score = json.loads((root / "out" / "score.json").read_text())
    man = synthesize(score, root / "out", backend=backend)
    nmarks = sum(len(m["marks"]) for m in man)
    print(f"[{backend}] {len(man)} clips, {nmarks} word-anchored cues, "
          f"{sum(len(m['words']) for m in man)} word timings")
