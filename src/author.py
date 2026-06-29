"""Authoring stage: source material -> validated Lecture Score (JSON) via gpt-5.4."""
from __future__ import annotations

import json
import re
from pathlib import Path

import jsonschema
import yaml

from azure_client import chat, respond, response_tool_calls, response_text

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((ROOT / "schema" / "lecture_score.schema.json").read_text())
SKILLS_DIR = ROOT / "skills"

# Authoring reasoning effort. "medium" is the sweet spot (most of the accuracy gain of "high"
# at ~15% less latency); raise to "high" for very complex content.
AUTHOR_REASONING = "medium"

# Output-token budget for one authoring pass. Long lectures (up to ~15 min / ~120 beats) need
# a large budget; the server scales this by the chosen lesson length.
AUTHOR_MAX_TOKENS = 32000


def load_base() -> str:
    return (SKILLS_DIR / "_base.md").read_text()


def list_subjects() -> list[str]:
    return sorted(d.name for d in SKILLS_DIR.iterdir()
                  if d.is_dir() and any(d.glob("*/SKILL.md")))


def _read_skill(path: Path) -> tuple[dict, str]:
    """Split an Agent-Skills SKILL.md (agentskills.io) into (frontmatter dict, markdown body)."""
    text = path.read_text()
    if text.startswith("---"):
        m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.DOTALL)
        if m:
            try:
                meta = yaml.safe_load(m.group(1)) or {}
            except Exception:
                meta = {}
            return meta, m.group(2).lstrip("\n")
    return {}, text


def list_packs(subject: str | None = None) -> dict[str, str]:
    """Return {relpath_to_SKILL.md: description} for each skill pack, optionally scoped to a subject.

    Packs follow the Agent Skills open standard (agentskills.io): each is a folder containing a
    SKILL.md whose YAML frontmatter carries `name` + `description`. This is *level-1* progressive
    disclosure — only the description (the index) is read so the router can pick relevant packs
    without loading their full bodies.
    """
    packs = {}
    globber = ((SKILLS_DIR / subject).glob("*/SKILL.md") if subject
               else SKILLS_DIR.glob("*/*/SKILL.md"))
    for p in sorted(globber):
        meta, _ = _read_skill(p)
        rel = str(p.relative_to(SKILLS_DIR))
        packs[rel] = meta.get("description") or meta.get("name") or p.parent.name
    return packs


def select_skills(material: str, images: list[str] | None = None,
                  subject: str | None = None) -> list[str]:
    """Ask the model which subdomain packs apply (within a subject, if given). Falls back to all."""
    packs = list_packs(subject)
    if not packs:
        return []
    if len(packs) == 1:
        return list(packs)
    menu = "\n".join(f"- {name}: {desc}" for name, desc in packs.items())
    scope = f" within {subject}" if subject else ""
    user_text = f"Packs:\n{menu}\n\nMaterial:\n{material[:2000]}"
    content = [{"type": "text", "text": user_text}]
    for url in (images or []):
        content.append({"type": "image_url", "image_url": {"url": url}})
    user_content = content if images else user_text
    try:
        raw = chat([
            {"role": "system", "content": f"You route a topic{scope} to the right skill packs. "
             "Return ONLY a JSON array of the pack paths that apply (most relevant first)."},
            {"role": "user", "content": user_content},
        ], max_completion_tokens=2000, reasoning_effort="minimal")
        picked = json.loads(_extract_json(raw))
        chosen = [p for p in picked if p in packs]
        return chosen or list(packs)
    except Exception:
        return list(packs)


def _extract_json(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```$", "", text.strip())
    # Grab the outermost JSON value (object OR array) — whichever opens first, so an array of
    # beats isn't mis-extracted as the first inner object.
    candidates = []
    for opener, closer in (("{", "}"), ("[", "]")):
        i, j = text.find(opener), text.rfind(closer)
        if 0 <= i < j:
            candidates.append((i, text[i:j + 1]))
    if candidates:
        candidates.sort()
        return candidates[0][1]
    return text


def _system_prompt(skills: list[str]) -> str:
    parts = [load_base(), "\n\n# Active skill packs\n"]
    for rel in skills:
        meta, body = _read_skill(SKILLS_DIR / rel)
        name = meta.get("name") or Path(rel).parent.name
        parts.append(f"\n## {name}\n" + body)
    parts.append("\n\n# JSON Schema (authoritative)\n```json\n"
                 + json.dumps(SCHEMA) + "\n```")
    return "".join(parts)


# Actions that create a new board object (carry `id` + `object`); everything else references
# existing objects. Mirrors the renderer's `fireCue` dispatch in player_template.html.
_CREATE_ACTIONS = {"write", "draw", "plot", "graph", "table", "figure", "diagram",
                   "chart", "image"}


def _object_text_parts(obj: dict) -> list[str]:
    """Strings inside an object that a cue `part` could legitimately point at."""
    t = obj.get("type")
    if t == "math":
        return [obj.get("tex", "")]
    if t == "text":
        return [obj.get("text", "")]
    if t == "table":
        cells = list(obj.get("headers", []) or [])
        for row in obj.get("rows", []) or []:
            cells += [c for c in (row or [])]
        return [str(c) for c in cells]
    if t in ("figure", "diagram"):
        out = []
        for e in obj.get("elements", []) or []:
            out += [str(e.get(k)) for k in ("label", "text", "name") if e.get(k)]
        for n in obj.get("nodes", []) or []:
            out.append(str(n.get("label") or n.get("name") or n.get("id") or n))
        return out
    return []


def validate_score(score: dict) -> tuple[list[str], list[str]]:
    """Deterministic semantic lint of a Lecture Score, beyond JSON-schema structure.

    Catches the render-error class the schema can't express: dangling/forward references,
    unresolvable `part`/`anchor`, out-of-range timing/positions, unbalanced TeX. Returns
    `(errors, warnings)`; `errors` are render-breaking (drive an auto-repair turn), `warnings`
    degrade gracefully at render time.
    """
    errors: list[str] = []
    warnings: list[str] = []
    created: dict[str, dict] = {}     # id -> object dict, accumulated in play order
    seen_ids: set[str] = set()

    def ref_check(loc: str, rid, kind: str):
        if not isinstance(rid, str) or not rid:
            return
        if rid not in created:
            errors.append(f"{loc}: {kind} references id '{rid}' that has not been created yet "
                          f"(dangling or forward reference)")

    for bi, beat in enumerate(score.get("beats", [])):
        say = (beat.get("say") or "")
        say_lc = say.lower()
        for ci, cue in enumerate(beat.get("cues", [])):
            loc = f"beat[{bi}].cue[{ci}] (action={cue.get('action')})"
            action = cue.get("action")

            # placement anchors must already exist
            ref_check(loc, cue.get("below"), "below")
            ref_check(loc, cue.get("right_of"), "right_of")
            # arrow endpoints that reference objects
            for fld in ("from_anchor", "to_anchor"):
                a = cue.get(fld)
                if isinstance(a, dict) and a.get("ref") is not None:
                    ref_check(loc, a.get("ref"), fld)
            # keep list (clear) must reference existing ids
            for kid in (cue.get("keep") or []):
                ref_check(loc, kid, "keep")

            if action in _CREATE_ACTIONS:
                cid = cue.get("id")
                if not cid:
                    warnings.append(f"{loc}: creating action has no id; later cues can't reference it")
                else:
                    if cid in seen_ids:
                        warnings.append(f"{loc}: id '{cid}' is created more than once")
                    seen_ids.add(cid)
                    created[cid] = cue.get("object") or {}
            else:
                # reference actions point at an existing target
                tgt = cue.get("target")
                if tgt is not None:
                    ref_check(loc, tgt, "target")
                # transform keeps the target id alive; nothing new to register
                # part resolvability against the (already-created) target object
                part = cue.get("part")
                if part is not None and isinstance(tgt, str) and tgt in created:
                    parts = _object_text_parts(created[tgt])
                    pstr = str(part).strip()
                    if parts and not any(pstr == str(s).strip() or pstr in str(s) for s in parts):
                        warnings.append(f"{loc}: part '{part}' not found in target '{tgt}' "
                                        f"— annotation will fall back to the whole object")

            # anchor should be a word actually spoken in this beat
            anchor = cue.get("anchor")
            if isinstance(anchor, str) and anchor.strip():
                a = anchor.strip().lower()
                if not re.search(r"(?:^|\W)" + re.escape(a) + r"(?:\W|$)", say_lc):
                    warnings.append(f"{loc}: anchor '{anchor}' is not a word in say "
                                    f"— cue timing falls back to 'at'")

            # timing / position ranges
            at = cue.get("at")
            if isinstance(at, (int, float)) and not (0 <= at <= 1):
                warnings.append(f"{loc}: at={at} is outside 0..1")
            pos = cue.get("pos")
            if isinstance(pos, dict):
                for k in ("x", "y"):
                    v = pos.get(k)
                    if isinstance(v, (int, float)) and not (0 <= v <= 100):
                        warnings.append(f"{loc}: pos.{k}={v} is outside 0..100")

            # light TeX sanity (KaTeX renders bad TeX as red text, never an error)
            obj = cue.get("object") or {}
            tex = obj.get("tex") if obj.get("type") == "math" else None
            if isinstance(tex, str) and tex:
                if tex.count("{") != tex.count("}"):
                    warnings.append(f"{loc}: tex has unbalanced braces {{ }}")
                if len(re.findall(r"\\left", tex)) != len(re.findall(r"\\right", tex)):
                    warnings.append(f"{loc}: tex has unbalanced \\left/\\right")

    return errors, warnings


def _run_chat_loop(messages: list[dict], *, max_tokens: int, max_retries: int = 3) -> dict:
    """Run the chat->validate->lint->repair loop on `messages`, returning a valid score.

    Mutates `messages` in place, appending repair turns. Does NOT append the final assistant
    turn — the caller appends a canonical `assistant` turn so the conversation can continue.
    """
    last_err = None
    for attempt in range(1, max_retries + 1):
        raw = chat(messages, max_completion_tokens=max_tokens, reasoning_effort=AUTHOR_REASONING)
        try:
            score = json.loads(_extract_json(raw))
            jsonschema.validate(score, SCHEMA)
        except (json.JSONDecodeError, jsonschema.ValidationError) as e:
            last_err = e
            print(f"  attempt {attempt} invalid: {str(e)[:160]}")
            messages.append({"role": "assistant", "content": raw[:4000]})
            messages.append({"role": "user", "content":
                             f"That was invalid: {str(e)[:600]}. Return corrected JSON only."})
            continue
        # schema OK -> deterministic semantic lint (references, parts, anchors, ranges, tex)
        errors, warnings = validate_score(score)
        for w in warnings:
            print(f"  lint warn: {w}")
        if errors and attempt < max_retries:
            print(f"  lint found {len(errors)} render-breaking error(s); requesting a fix")
            messages.append({"role": "assistant", "content": raw[:4000]})
            messages.append({"role": "user", "content":
                             "Your JSON is schema-valid but has these render-breaking reference "
                             "errors:\n- " + "\n- ".join(errors[:25]) +
                             "\nFix ONLY these (every target/below/right_of/keep id must be "
                             "created by an earlier cue). Return corrected JSON only."})
            continue
        if errors:                       # out of retries: log and ship the best we have
            for e in errors:
                print(f"  lint error (unresolved): {e}")
        return score
    raise RuntimeError(f"authoring failed after {max_retries} tries: {last_err}")


# Subjects where realistic generated images (photos/micrographs/apparatus) aid understanding.
# Math (and other primitive-only subjects) are fully served by vector primitives.
IMAGE_SUBJECTS = {"biology", "physics", "chemistry"}


def _image_policy(subject: str | None) -> str:
    if subject in IMAGE_SUBJECTS:
        return ("\n\n=== IMAGE POLICY ===\nRealistic `image` objects (photos, micrographs, lab "
                "apparatus, anatomical art) are AVAILABLE and encouraged where a real-world picture "
                "aids understanding: give a vivid `prompt`, then label it with `callout`/`region`. "
                "Use them **sparingly — at most ~5 per lecture** (each is generated by a slow, "
                "rate-limited model); for everything else use vector primitives "
                "(figure/diagram/graph/chart), which you must also use for schematics you label "
                "precisely.\n")
    if subject:   # known primitive-only subject (e.g. math, english)
        return ("\n\n=== IMAGE POLICY ===\nDo NOT use `image` objects for this subject — it is fully "
                "served by the vector primitives (figure/diagram/graph/chart/table). Build every "
                "visual yourself with those.\n")
    return ""     # unknown/any subject: keep the base default (image only when essential)


def author(material: str = "", skills: list[str] | None = None, guidance: str | None = None,
           images: list[str] | None = None, subject: str | None = None,
           max_retries: int = 3, max_tokens: int = AUTHOR_MAX_TOKENS,
           return_messages: bool = False):
    """Generate and validate a Lecture Score from source material.

    `subject` (e.g. 'math', 'biology') scopes which skill packs are considered — the teacher
    picks it up front. `guidance` is the teacher's free-form instruction steering pace, key
    highlights, tone, depth, length. `images` is an optional list of data-URLs sent directly to
    the (multimodal) model so graphs/diagrams are preserved without a markdown round-trip.

    With `return_messages=True`, also returns the running chat history so the caller can later
    continue the conversation (see `refine`) to fine-tune the lecture from teacher feedback.
    """
    skills = skills if skills is not None else select_skills(material, images, subject)
    print(f"  subject: {subject}  skills: {skills}")
    system = _system_prompt(skills)
    guide_block = ""
    if guidance and guidance.strip():
        guide_block = ("\n\n=== TEACHER'S INSTRUCTIONS (steer pacing, emphasis, tone, depth, "
                       "length, and which steps to highlight — on top of the default goal of a "
                       "complete, engaging lecture) ===\n" + guidance.strip() + "\n")
    src = ("\n=== MATERIAL (attached image" + ("s" if (images and len(images) > 1) else "")
           + ") — read graphs, tables, and equations directly from it ===\n"
           if images else "\n=== MATERIAL ===\n" + material)
    user_text = ("Create a Lecture Score for the following material. Output ONLY the JSON "
                 "object." + guide_block + _image_policy(subject) + src)
    if images:
        user_content = [{"type": "text", "text": user_text}]
        for url in images:
            user_content.append({"type": "image_url", "image_url": {"url": url}})
    else:
        user_content = user_text
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user_content}]

    score = _run_chat_loop(messages, max_tokens=max_tokens, max_retries=max_retries)
    score.setdefault("meta", {})["skills"] = skills
    if subject:
        score["meta"]["subject"] = subject
    messages.append({"role": "assistant", "content": json.dumps(score)})
    return (score, messages) if return_messages else score


def refine(messages: list[dict], feedback: str, *, prev_score: dict | None = None,
           subject: str | None = None, max_tokens: int = AUTHOR_MAX_TOKENS,
           max_retries: int = 3) -> tuple[dict, list[dict]]:
    """Continue an authoring conversation: apply the teacher's feedback to the current lecture.

    `messages` is the history returned by `author(..., return_messages=True)` (it already ends
    with the latest score as an assistant turn). Returns the revised `(score, messages)`. The
    caller should persist the returned `messages` to keep the chat multi-turn.
    """
    convo = list(messages)
    convo.append({"role": "user", "content":
                  "The teacher reviewed the lecture and gave this feedback. Revise the SAME "
                  "Lecture Score to address it, keeping the rest intact and ids stable where "
                  "possible. Output ONLY the full corrected JSON object for the whole lecture.\n\n"
                  "FEEDBACK:\n" + (feedback or "").strip()})
    score = _run_chat_loop(convo, max_tokens=max_tokens, max_retries=max_retries)
    meta = score.setdefault("meta", {})
    if prev_score:
        for k in ("skills", "subject", "voice"):
            if k not in meta and prev_score.get("meta", {}).get(k) is not None:
                meta[k] = prev_score["meta"][k]
    if subject:
        meta["subject"] = subject
    convo.append({"role": "assistant", "content": json.dumps(score)})
    return score, convo


def refine_from_score(prev_score: dict, feedback: str, *, subject: str | None = None,
                      max_tokens: int = AUTHOR_MAX_TOKENS,
                      max_retries: int = 3) -> tuple[dict, list[dict]]:
    """Refine a lecture WITHOUT a prior conversation (e.g. a 2-stage job): rebuild a minimal
    authoring conversation around the current score + feedback, then return `(score, messages)`."""
    skills = [s for s in (prev_score.get("meta", {}).get("skills") or [])
              if s and (SKILLS_DIR / s).exists()]
    if not skills:
        skills = list(list_packs(subject))
    convo = [{"role": "system", "content": _system_prompt(skills)},
             {"role": "user", "content":
              "Here is the current Lecture Score JSON:\n" + json.dumps(prev_score) +
              "\n\nApply this teacher feedback and return ONLY the full corrected Lecture Score JSON "
              "object (keep ids stable where possible):\n" + (feedback or "").strip()}]
    score = _run_chat_loop(convo, max_tokens=max_tokens, max_retries=max_retries)
    meta = score.setdefault("meta", {})
    for k in ("skills", "subject", "voice"):
        if k not in meta and prev_score.get("meta", {}).get(k) is not None:
            meta[k] = prev_score["meta"][k]
    if subject:
        meta["subject"] = subject
    convo.append({"role": "assistant", "content": json.dumps(score)})
    return score, convo
# Stage 1 (Director): plan the lecture as an OUTLINE of sections (independent topics) with the
# spoken script + a short visual intent per beat. Stage 2 (Animators): elaborate each section's
# beats into full cues, in parallel. Stage 3: merge + global validate. Decomposing "what to teach"
# from "how to draw it" reduces the burden on each call (better quality) and parallelizes the
# heavy visual work (lower latency on long content).

OUTLINE_REASONING = "low"


def _skills_menu(subject: str | None) -> str:
    return "\n".join(f"- {name}: {desc}" for name, desc in list_packs(subject).items())


def outline(material: str = "", images: list[str] | None = None, subject: str | None = None,
            guidance: str | None = None, max_tokens: int = 8000, max_retries: int = 3) -> dict:
    """Stage 1: a cheap, fast plan of the lecture (sections -> beats with `say` + `visual`)."""
    menu = _skills_menu(subject)
    system = (
        "You are a master teacher and lecture DIRECTOR. Plan a "
        + (subject + " " if subject else "") + "video lecture from the material as a JSON OUTLINE — "
        "the teaching plan only, NOT the drawing detail.\n\n"
        "Return ONLY JSON of the form:\n"
        '{ "title": str,\n'
        '  "sections": [ { "id": "sec1", "title": str, "skill": <one pack path from the list>,\n'
        '      "beats": [ { "say": one spoken sentence (words only, NO LaTeX/symbols),\n'
        '                   "visual": short note on what should appear/animate on the whiteboard } ] } ] }\n\n'
        "Rules:\n"
        "- Split into SECTIONS only where it helps the audience — each section is a relatively "
        "independent topic that begins on a fresh board. A simple concept can be ONE section.\n"
        "- Teach the material thoroughly in a logical order; use many short beats (one idea each).\n"
        "- `say` is the spoken script. `visual` is a brief intent, e.g. 'plot y=x^2-4, mark vertex "
        "(0,-4)' or 'factor x^2-9 as (x-3)(x+3) step by step'.\n"
        "- Give each section the most relevant skill pack from:\n" + menu
    )
    user_text = ("Plan the lecture for the attached material."
                 + (f"\n\nTEACHER GUIDANCE (follow closely): {guidance}" if guidance else ""))
    if images:
        user_content = [{"type": "text", "text": user_text}] + \
                       [{"type": "image_url", "image_url": {"url": u}} for u in images]
    else:
        user_content = user_text + "\n\n=== MATERIAL ===\n" + material
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user_content}]
    for attempt in range(1, max_retries + 1):
        raw = chat(messages, max_completion_tokens=max_tokens, reasoning_effort=OUTLINE_REASONING)
        try:
            o = json.loads(_extract_json(raw))
            if not (isinstance(o.get("sections"), list) and o["sections"]):
                raise ValueError("missing sections")
            return o
        except Exception as e:
            messages.append({"role": "assistant", "content": raw[:3000]})
            messages.append({"role": "user", "content":
                             f"That outline was invalid ({str(e)[:200]}). Return corrected JSON only."})
    raise RuntimeError("outline failed")


def elaborate_section(section: dict, subject: str | None = None, guidance: str | None = None,
                      max_tokens: int = AUTHOR_MAX_TOKENS, max_retries: int = 3) -> list[dict]:
    """Stage 2: turn one section's plan into a schema-valid array of Lecture Score beats."""
    skill = section.get("skill")
    if skill and (SKILLS_DIR / skill).exists():
        skills = [skill]
    else:
        skills = list(list_packs(subject))
    sid = section.get("id", "sec")
    system = _system_prompt(skills) + (
        "\n\n# Your task\nYou are elaborating ONE SECTION of a larger lecture into Lecture Score "
        "beats. The board STARTS EMPTY for this section. Realize each beat's `visual` intent with "
        "full cues (write/draw/graph/table/figure/diagram/chart/transform/circle/callout/region/"
        "point/etc.), with timing `anchor`s, positions, and meaningful colors. Do NOT set `w`/`h` on "
        "graphs/charts — the renderer auto-sizes them to be readable. KEEP each provided "
        "`say` faithfully (you may split one beat into two if a sentence needs two drawing steps). "
        f"Prefix EVERY object `id` and beat `id` with '{sid}_' so ids stay unique across sections. "
        "Output ONLY a JSON ARRAY of beats (no surrounding object, no prose)."
    )
    sec_json = json.dumps({"title": section.get("title"), "beats": section.get("beats", [])})
    user = ("Section to elaborate:\n" + sec_json
            + (f"\n\nTEACHER GUIDANCE: {guidance}" if guidance else "")
            + "\n\nReturn ONLY the JSON array of Lecture Score beats for this section.")
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    title = section.get("title") or "Section"
    last_err = None
    for attempt in range(1, max_retries + 1):
        raw = chat(messages, max_completion_tokens=max_tokens, reasoning_effort=AUTHOR_REASONING)
        try:
            beats = json.loads(_extract_json(raw))
            if not (isinstance(beats, list) and beats):
                raise ValueError("expected a non-empty array of beats")
            jsonschema.validate({"meta": {"title": title}, "board": {}, "beats": beats}, SCHEMA)
        except (json.JSONDecodeError, jsonschema.ValidationError, ValueError) as e:
            last_err = e
            messages.append({"role": "assistant", "content": raw[:4000]})
            messages.append({"role": "user", "content":
                             f"That was invalid ({str(e)[:300]}). Return ONLY the corrected JSON "
                             "array of beats."})
            continue
        # schema OK -> referential lint within the section (repair dangling refs like the main author)
        errors, _ = validate_score({"meta": {"title": title}, "board": {}, "beats": beats})
        if errors and attempt < max_retries:
            messages.append({"role": "assistant", "content": raw[:4000]})
            messages.append({"role": "user", "content":
                             "These references are invalid within this section:\n- "
                             + "\n- ".join(errors[:20]) +
                             "\nEvery target/below/right_of/keep id must be created by an EARLIER cue "
                             "in this section (the board starts empty here). Fix ONLY these and return "
                             "the corrected JSON array of beats."})
            continue
        return beats
    raise RuntimeError(f"section '{sid}' elaboration failed: {last_err}")


def _merge_sections(sections: list[dict], *, title: str, flow: str, subject: str | None) -> dict:
    """Build a Lecture Score from ordered sections (each {id,title,skill,plan_beats,beats}).

    A `clear` separates topics; beat ids are renumbered globally. Returns the validated score."""
    merged: list = []
    for sec in sections:
        beats = sec.get("beats") or []
        if not beats:
            continue
        cues0 = beats[0].get("cues") or []  # idempotent: drop a break-clear from a previous merge
        if cues0 and cues0[0].get("action") == "clear" and not cues0[0].get("keep") \
                and (cues0[0].get("at") or 0.0) == 0.0:
            cues0.pop(0)
        if merged:                          # section break: clear the board for the new topic
            beats[0].setdefault("cues", []).insert(0, {"at": 0.0, "action": "clear", "keep": []})
        merged.extend(beats)
    for idx, b in enumerate(merged):        # guarantee globally-unique beat ids
        b["id"] = f"b{idx:04d}"
    score = {"meta": {"title": title or "Lecture"}, "board": {"flow": flow}, "beats": merged}
    try:
        jsonschema.validate(score, SCHEMA)
    except jsonschema.ValidationError as e:
        print(f"  [merge] schema warning: {str(e)[:160]}")
    score["meta"]["skills"] = [s.get("skill") for s in sections if s.get("skill")]
    if subject:
        score["meta"]["subject"] = subject
    errs, warns = validate_score(score)
    for w in warns[:8]:
        print(f"  lint warn: {w}")
    for e in errs[:8]:
        print(f"  lint error: {e}")
    return score


def elaborate_outline(plan: dict, *, subject: str | None = None, guidance: str | None = None,
                      max_tokens: int = AUTHOR_MAX_TOKENS, flow: str = "page", progress=None,
                      return_sections: bool = False):
    """Stage 2+3: elaborate an outline's sections (in parallel) and merge into a Lecture Score.

    Works on ANY outline — freshly planned or teacher-edited in the storyboard. With
    `return_sections=True`, also returns the per-section structure (for section-scoped editing)."""
    from concurrent.futures import ThreadPoolExecutor

    def say(stage, msg):
        if progress:
            progress(stage, msg)
        print(f"  [2stage] {msg}")

    secs = plan.get("sections", [])
    say("author", f"elaborating {len(secs)} section(s), "
                  f"{sum(len(s.get('beats', [])) for s in secs)} planned beats …")

    results: dict[str, list] = {}

    def work(s):
        try:
            return s["id"], elaborate_section(s, subject=subject, guidance=guidance,
                                              max_tokens=max_tokens)
        except Exception as e:
            print(f"  section '{s.get('id')}' failed: {e}")
            return s["id"], []

    with ThreadPoolExecutor(max_workers=min(4, max(1, len(secs)))) as ex:
        for sid, beats in ex.map(work, secs):
            results[sid] = beats
    say("author", f"elaborated {sum(1 for v in results.values() if v)}/{len(secs)} sections")

    sections = [{"id": s["id"], "title": s.get("title"), "skill": s.get("skill"),
                 "plan_beats": s.get("beats", []), "beats": results.get(s["id"]) or []}
                for s in secs]
    score = _merge_sections(sections, title=plan.get("title", "Lecture"), flow=flow, subject=subject)
    say("author", f"{len(score['beats'])} beats total")
    return (score, sections) if return_sections else score


def author_2stage(material: str = "", images: list[str] | None = None, subject: str | None = None,
                  guidance: str | None = None, max_tokens: int = AUTHOR_MAX_TOKENS,
                  flow: str = "page", progress=None, return_sections: bool = False):
    """Two-stage pipeline: outline -> parallel section elaboration -> merge + validate."""
    plan = outline(material, images=images, subject=subject, guidance=guidance)
    if progress:
        progress("author", f"outline: {len(plan.get('sections', []))} section(s)")
    return elaborate_outline(plan, subject=subject, guidance=guidance, max_tokens=max_tokens,
                             flow=flow, progress=progress, return_sections=return_sections)


def refine_outline(plan: dict, instruction: str, subject: str | None = None,
                   prev_response_id: str | None = None, max_tokens: int = 8000,
                   max_retries: int = 3) -> tuple[dict, str | None]:
    """Apply a teacher's natural-language instruction to the OUTLINE (cheap, no full generation).

    Uses the Responses API with `previous_response_id` so the storyboard chat has conversation
    memory across turns (e.g. "revert that", "a bit shorter still"). The current plan is re-sent
    each turn (it's small) so the model always edits the exact current state. Returns
    `(revised_outline, response_id)`; pass the id back as `prev_response_id` on the next turn."""
    menu = _skills_menu(subject)
    instructions = (
        "You edit a video-lecture OUTLINE (the plan, not the drawing detail) for a teacher. "
        "Apply their instruction and return the FULL revised outline as JSON in the SAME shape:\n"
        '{ "title": str, "sections": [ { "id": str, "title": str, "skill": <pack path>,\n'
        '    "beats": [ { "say": one spoken sentence (words only, NO LaTeX), "visual": short note } ] } ] }\n'
        "Rules: keep existing section `id`s stable where a section is kept; give any NEW section a "
        "fresh unique `id`; assign each section a `skill` from the list below; `say` stays "
        "spoken-only. Change only what the instruction asks; keep everything else intact.\n"
        "Skill packs:\n" + menu
    )
    msg = ("Current outline JSON:\n" + json.dumps(plan) + "\n\nInstruction:\n"
           + (instruction or "").strip() + "\n\nReturn ONLY the full revised outline JSON.")
    rid = prev_response_id
    last = None
    for attempt in range(1, max_retries + 1):
        resp = respond([{"role": "user", "content": [{"type": "input_text", "text": msg}]}],
                       instructions=instructions, previous_response_id=rid,
                       reasoning_effort=OUTLINE_REASONING, max_output_tokens=max_tokens)
        rid = resp.get("id")
        raw = response_text(resp)
        try:
            o = json.loads(_extract_json(raw))
            if not (isinstance(o.get("sections"), list) and o["sections"]):
                raise ValueError("missing sections")
            for i, s in enumerate(o["sections"]):   # ensure every section has an id
                s.setdefault("id", f"sec{i+1}")
            return o, rid
        except (json.JSONDecodeError, ValueError) as e:
            last = e
            msg = (f"That was invalid ({str(e)[:200]}). Return ONLY the full revised outline JSON.")
    raise RuntimeError(f"outline refine failed: {last}")


# ---------------- Stage-2 multimodal editing agent ----------------
def _refine_section_beats(title: str, skill: str | None, current_beats: list, instruction: str,
                          subject: str | None, max_tokens: int, max_retries: int = 3) -> list:
    """Apply a teacher's edit to ONE section's beats (preserving the rest). Schema + lint validated."""
    skills = [skill] if (skill and (SKILLS_DIR / skill).exists()) else list(list_packs(subject))
    system = _system_prompt(skills) + (
        "\n\n# Your task\nYou are EDITING the cues of ONE section of an existing lecture. Apply the "
        "teacher's edit and keep everything else faithful. The board starts EMPTY for this section. "
        "Keep object ids stable; every target/below/right_of/keep id must be created by an earlier "
        "cue here. Output ONLY the corrected JSON ARRAY of beats.")
    convo = [{"role": "system", "content": system},
             {"role": "user", "content": "Section: " + (title or "") + "\nCurrent beats JSON:\n"
              + json.dumps(current_beats) + "\n\nTEACHER EDIT:\n" + (instruction or "").strip()
              + "\n\nReturn ONLY the corrected JSON array of beats."}]
    last = None
    for attempt in range(1, max_retries + 1):
        raw = chat(convo, max_completion_tokens=max_tokens, reasoning_effort=AUTHOR_REASONING)
        try:
            beats = json.loads(_extract_json(raw))
            if not (isinstance(beats, list) and beats):
                raise ValueError("expected a non-empty array of beats")
            jsonschema.validate({"meta": {"title": title or "S"}, "board": {}, "beats": beats}, SCHEMA)
        except (json.JSONDecodeError, jsonschema.ValidationError, ValueError) as e:
            last = e
            convo.append({"role": "assistant", "content": raw[:4000]})
            convo.append({"role": "user", "content":
                          f"Invalid ({str(e)[:200]}). Return ONLY the corrected JSON array of beats."})
            continue
        errs, _ = validate_score({"meta": {"title": "S"}, "board": {}, "beats": beats})
        if errs and attempt < max_retries:
            convo.append({"role": "assistant", "content": raw[:4000]})
            convo.append({"role": "user", "content": "These references are invalid:\n- "
                          + "\n- ".join(errs[:15]) + "\nFix and return ONLY the corrected array of beats."})
            continue
        return beats
    raise RuntimeError(f"section edit failed: {last}")


_EDIT_TOOLS = [
    {"type": "function", "name": "edit_section",
     "description": "Re-edit one or more sections of the lecture to apply a visual/teaching change "
                    "(e.g. circle a value, change a colour, add a step, fix an overlapping label, "
                    "enlarge or relabel a graph).",
     "parameters": {"type": "object", "properties": {
         "section_ids": {"type": "array", "items": {"type": "string"},
                         "description": "ids of the section(s) the change applies to"},
         "instruction": {"type": "string", "description": "the concrete change to make"}},
         "required": ["section_ids", "instruction"]}},
    {"type": "function", "name": "edit_global",
     "description": "Apply a lecture-wide change to every section (e.g. overall tone, colour scheme, "
                    "make all graphs larger).",
     "parameters": {"type": "object", "properties": {
         "instruction": {"type": "string", "description": "the lecture-wide change"}},
         "required": ["instruction"]}},
]


def agent_edit(score: dict, sections: list[dict], message: str, *, images: list[str] | None = None,
               subject: str | None = None, flow: str = "page", prev_response_id: str | None = None,
               max_tokens: int = AUTHOR_MAX_TOKENS, progress=None) -> dict:
    """One turn of the multimodal Stage-2 editing agent.

    The agent (Responses API) decides which section(s) a request — text and/or a screenshot — affects
    and calls `edit_section`/`edit_global`; we execute the edit with the focused per-section
    elaborator, re-merge, and return the new score. Returns {score, sections, reply, response_id,
    edited}."""
    def say(msg):
        if progress:
            progress("author", msg)
        print(f"  [edit] {msg}")

    toc = "\n".join(
        f"- {s['id']}: {s.get('title') or ''} — "
        + " / ".join((b.get('say') or '')[:38] for b in (s.get('plan_beats') or [])[:4])
        for s in sections)
    instructions = (
        "You edit an existing whiteboard video lecture for a teacher. The lecture's sections are:\n"
        + toc + "\n\nThe teacher describes a change (text and/or a screenshot of the part they mean). "
        "Decide which section(s) it affects and call edit_section(section_ids, instruction) with a "
        "concrete instruction. Use edit_global only for lecture-wide style/tone changes. You may call "
        "several tools for a compound request. If the request is genuinely ambiguous, ask one short "
        "clarifying question instead of guessing. After tools run, briefly tell the teacher what changed.")
    content = [{"type": "input_text", "text": (message or "(see the attached screenshot)")}]
    for url in (images or []):
        content.append({"type": "input_image", "image_url": url})

    resp = respond([{"role": "user", "content": content}], instructions=instructions,
                                tools=_EDIT_TOOLS, previous_response_id=prev_response_id,
                                reasoning_effort="low")
    calls = response_tool_calls(resp)
    if not calls:                            # a clarifying question or plain reply — nothing to edit
        return {"score": score, "sections": sections,
                "reply": response_text(resp) or "Could you clarify what to change?",
                "response_id": resp.get("id"), "edited": []}

    sec_by_id = {s["id"]: s for s in sections}
    edited, tool_outputs = [], []
    for c in calls:
        args, name = c["arguments"], c["name"]
        try:
            if name == "edit_global":
                instr = args.get("instruction") or message
                say("applying a lecture-wide change …")
                for sec in sections:
                    if sec.get("beats"):
                        sec["beats"] = _refine_section_beats(sec.get("title"), sec.get("skill"),
                                                            sec["beats"], instr, subject, max_tokens)
                        edited.append(sec["id"])
                out = "applied to all sections"
            else:  # edit_section
                ids = [i for i in (args.get("section_ids") or []) if i in sec_by_id]
                instr = args.get("instruction") or message
                for sid in ids:
                    sec = sec_by_id[sid]
                    say(f"editing section {sid} ({sec.get('title')}) …")
                    sec["beats"] = _refine_section_beats(sec.get("title"), sec.get("skill"),
                                                        sec.get("beats") or [], instr, subject, max_tokens)
                    edited.append(sid)
                out = f"edited: {', '.join(ids) or 'no matching section'}"
        except Exception as e:
            out = f"edit failed: {str(e)[:120]}"
        tool_outputs.append({"type": "function_call_output", "call_id": c["call_id"], "output": out})

    new_score = _merge_sections(sections, title=score.get("meta", {}).get("title", "Lecture"),
                                flow=flow, subject=subject)
    follow = respond(tool_outputs, previous_response_id=resp.get("id"),
                                  reasoning_effort="low")
    reply = response_text(follow) or ("Updated section(s): " + ", ".join(sorted(set(edited))))
    return {"score": new_score, "sections": sections, "reply": reply,
            "response_id": follow.get("id"), "edited": sorted(set(edited))}


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 2 and sys.argv[1] == "--lint":
        score = json.loads(Path(sys.argv[2]).read_text())
        errors, warnings = validate_score(score)
        for w in warnings:
            print(f"WARN  {w}")
        for e in errors:
            print(f"ERROR {e}")
        print(f"\n{len(errors)} error(s), {len(warnings)} warning(s)")
        sys.exit(1 if errors else 0)
    src = Path(sys.argv[1]).read_text() if len(sys.argv) > 1 else \
        "Teach factoring x^2 - 9 as a difference of squares."
    score = author(src)
    out = ROOT / "out" / "score.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(score, indent=2))
    print(f"wrote {out} ({len(score['beats'])} beats)")
