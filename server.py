"""Teacher UI: upload raw material + a guidance prompt, and generate a narrated
whiteboard lecture. Run:  python server.py   then open http://localhost:5000

The guidance prompt and the structured options (audience, tone, pace) steer how gpt-5.4
writes the lecture (pacing, which steps to emphasize, tone, depth).
"""
from __future__ import annotations

import sys
import threading
import traceback
import uuid
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory, abort

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
import pipeline  # noqa: E402
import author  # noqa: E402

JOBS_DIR = ROOT / "out" / "jobs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
JOBS: dict[str, dict] = {}      # lightweight, JSON-serializable status per job (polled by /status)
CTX: dict[str, dict] = {}       # heavy refine context per job: {messages, score, opts, chat, refine}
OUTLINES: dict[str, dict] = {}  # storyboard phase per job: {plan, opts, status}

VOICES = [
    ("en-US-AvaMultilingualNeural", "Ava — warm, friendly · word-sync + karaoke (default)"),
    ("en-US-AndrewMultilingualNeural", "Andrew — calm, natural · word-sync"),
    ("en-US-EmmaMultilingualNeural", "Emma — bright, clear · word-sync"),
    ("en-US-Harper:MAI-Voice-2", "Harper — MAI-Voice-2, expressive (no word-sync)"),
    ("en-US-Olivia:MAI-Voice-2", "Olivia — MAI-Voice-2, expressive (no word-sync)"),
    ("en-US-Ethan:MAI-Voice-2", "Ethan — MAI-Voice-2, expressive (no word-sync)"),
    ("en-US-Jasper:MAI-Voice-2", "Jasper — MAI-Voice-2, expressive (no word-sync)"),
]

# Lesson length -> (output-token cap, guidance hint). The hint steers the target duration;
# the cap mainly prevents truncating long lectures (the model stops early when it's done).
LENGTHS = {
    "auto":     (40000, "Make the lecture as long as the material needs to be taught well — "
                        "up to ~15 minutes for rich, multi-section material. Don't shorten artificially."),
    "concise":  (20000, "Keep it concise: about 3-5 minutes, only the essential ideas."),
    "standard": (32000, "Aim for a thorough ~6-10 minute lecture."),
    "in_depth": (56000, "Aim for an in-depth ~12-15 minute lecture: cover every sub-topic with "
                        "worked examples and quick checks for understanding."),
}


def _compose_guidance(form) -> str:
    bits = []
    if form.get("level"):
        bits.append(f"Target audience: {form['level']}.")
    if form.get("tone"):
        bits.append(f"Speaking tone: {form['tone']}.")
    if form.get("pace"):
        bits.append({
            "slow": "Pace: slow and thorough — break ideas into many small beats, pause on key steps.",
            "normal": "Pace: a natural classroom pace.",
            "brisk": "Pace: brisk and focused — keep it tight, only the essential steps.",
        }.get(form["pace"], ""))
    if form.get("highlights"):
        bits.append(f"Make sure to emphasize and highlight: {form['highlights']}.")
    if form.get("prompt"):
        bits.append(form["prompt"].strip())
    return " ".join(b for b in bits if b)


def _run_job(job_id: str, material_path: Path, opts: dict):
    job = JOBS[job_id]

    def progress(stage, msg):
        job["stage"] = stage
        job["message"] = msg

    try:
        res = pipeline.run(material_path, JOBS_DIR / job_id,
                           subject=opts["subject"], guidance=opts["guidance"], voice=opts["voice"],
                           tts_backend=opts["tts"], flow=opts["flow"],
                           ingest_mode=opts["ingest_mode"], max_tokens=opts["max_tokens"],
                           author_mode=opts.get("author_mode", "2stage"),
                           progress=progress)
        CTX[job_id] = {"messages": res["messages"], "score": res["score"], "opts": opts,
                       "sections": res.get("sections"), "response_id": None,
                       "chat": [], "refine": {"done": True, "rev": 0}}
        job["beats"] = len(res["score"].get("beats", []))
        job["done"] = True
        job["url"] = f"/lecture/{job_id}/"
    except Exception as e:
        job["error"] = f"{type(e).__name__}: {e}"
        job["trace"] = traceback.format_exc()[-1500:]
        job["done"] = True


@app.route("/generate", methods=["POST"])
def generate():
    f = request.files.get("material")
    if not f or not f.filename:
        return jsonify({"error": "Please choose a material file."}), 400
    job_id = uuid.uuid4().hex[:12]
    jdir = JOBS_DIR / job_id
    jdir.mkdir(parents=True, exist_ok=True)
    mat_path = jdir / ("material" + Path(f.filename).suffix.lower())
    f.save(str(mat_path))

    cap, length_hint = LENGTHS.get(request.form.get("length", "auto"), LENGTHS["auto"])
    guidance = _compose_guidance(request.form)
    guidance = (guidance + " " + length_hint).strip() if guidance else length_hint
    opts = {
        "guidance": guidance,
        "voice": request.form.get("voice") or VOICES[0][0],
        "tts": request.form.get("tts", "speech"),
        "flow": request.form.get("flow", "page"),
        "ingest_mode": request.form.get("ingest_mode", "image"),
        "subject": request.form.get("subject") or None,
        "max_tokens": cap,
        "author_mode": request.form.get("author_mode", "2stage"),
    }
    JOBS[job_id] = {"stage": "queued", "message": "starting…", "done": False,
                    "error": None, "url": None, "guidance": opts["guidance"]}
    threading.Thread(target=_run_job, args=(job_id, mat_path, opts), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
def status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    return jsonify(job)


# ---------------- storyboard review: Stage-1 outline, edit, then generate ----------------
def _common_opts(form) -> dict:
    cap, length_hint = LENGTHS.get(form.get("length", "auto"), LENGTHS["auto"])
    guidance = _compose_guidance(form)
    guidance = (guidance + " " + length_hint).strip() if guidance else length_hint
    return {"guidance": guidance, "voice": form.get("voice") or VOICES[0][0],
            "tts": form.get("tts", "speech"), "flow": form.get("flow", "page"),
            "ingest_mode": form.get("ingest_mode", "image"),
            "subject": form.get("subject") or None, "max_tokens": cap}


def _run_outline(job_id: str, material_path: Path, opts: dict):
    ol = OUTLINES[job_id]

    def progress(stage, msg):
        ol["status"]["stage"] = stage
        ol["status"]["message"] = msg

    try:
        plan = pipeline.plan_outline(material_path, subject=opts["subject"],
                                     guidance=opts["guidance"], ingest_mode=opts["ingest_mode"],
                                     progress=progress)
        ol["plan"] = plan
        ol["status"].update(done=True, message="outline ready")
    except Exception as e:
        ol["status"].update(done=True, error=f"{type(e).__name__}: {e}")
        ol["status"]["trace"] = traceback.format_exc()[-1200:]


@app.route("/outline", methods=["POST"])
def make_outline():
    f = request.files.get("material")
    if not f or not f.filename:
        return jsonify({"error": "Please choose a material file."}), 400
    job_id = uuid.uuid4().hex[:12]
    jdir = JOBS_DIR / job_id
    jdir.mkdir(parents=True, exist_ok=True)
    mat_path = jdir / ("material" + Path(f.filename).suffix.lower())
    f.save(str(mat_path))
    opts = _common_opts(request.form)
    opts["material_path"] = str(mat_path)
    OUTLINES[job_id] = {"plan": None, "opts": opts, "response_id": None,
                        "status": {"stage": "queued", "message": "starting…", "done": False,
                                   "error": None}}
    threading.Thread(target=_run_outline, args=(job_id, mat_path, opts), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/outline_status/<job_id>")
def outline_status(job_id):
    ol = OUTLINES.get(job_id)
    if not ol:
        return jsonify({"error": "unknown outline"}), 404
    return jsonify({**ol["status"], "plan": ol.get("plan")})


@app.route("/outline_refine/<job_id>", methods=["POST"])
def outline_refine(job_id):
    ol = OUTLINES.get(job_id)
    if not ol:
        return jsonify({"error": "unknown outline"}), 404
    body = request.json or {}
    plan = body.get("plan") or ol.get("plan")
    msg = (body.get("message") or "").strip()
    if not plan or not msg:
        return jsonify({"error": "need plan + message"}), 400
    try:
        revised, rid = author.refine_outline(plan, msg, subject=ol["opts"]["subject"],
                                             prev_response_id=ol.get("response_id"))
        ol["plan"] = revised
        ol["response_id"] = rid
        return jsonify({"plan": revised})
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


def _run_from_outline(job_id: str, plan: dict, opts: dict):
    job = JOBS[job_id]

    def progress(stage, msg):
        job["stage"] = stage
        job["message"] = msg

    try:
        res = pipeline.run_from_outline(plan, JOBS_DIR / job_id, subject=opts["subject"],
                                        guidance=opts["guidance"], voice=opts["voice"],
                                        tts_backend=opts["tts"], flow=opts["flow"],
                                        max_tokens=opts["max_tokens"], progress=progress)
        CTX[job_id] = {"messages": None, "score": res["score"], "opts": opts,
                       "sections": res.get("sections"), "response_id": None,
                       "chat": [], "refine": {"done": True, "rev": 0}}
        job["beats"] = len(res["score"].get("beats", []))
        job["done"] = True
        job["url"] = f"/lecture/{job_id}/"
    except Exception as e:
        job["error"] = f"{type(e).__name__}: {e}"
        job["trace"] = traceback.format_exc()[-1500:]
        job["done"] = True


@app.route("/generate_from_outline/<job_id>", methods=["POST"])
def generate_from_outline(job_id):
    ol = OUTLINES.get(job_id)
    if not ol:
        return jsonify({"error": "unknown outline — please start over"}), 404
    plan = (request.json or {}).get("plan") or ol.get("plan")
    if not plan or not plan.get("sections"):
        return jsonify({"error": "empty outline"}), 400
    ol["plan"] = plan
    opts = ol["opts"]
    JOBS[job_id] = {"stage": "queued", "message": "starting…", "done": False,
                    "error": None, "url": None, "guidance": opts["guidance"]}
    threading.Thread(target=_run_from_outline, args=(job_id, plan, opts), daemon=True).start()
    return jsonify({"ok": True})


# ---------------- feedback chat: multi-turn refinement of a generated lecture ----------------
def _run_feedback(job_id: str, feedback: str, images: list | None = None):
    ctx = CTX[job_id]
    rf = ctx["refine"]
    opts = ctx["opts"]

    def progress(stage, msg):
        rf["stage"] = stage
        rf["message"] = msg

    def _invalidate_export():
        mp4 = JOBS_DIR / job_id / "lecture.mp4"
        if mp4.exists():
            mp4.unlink()
        if job_id in JOBS:
            JOBS[job_id].pop("export", None)

    try:
        if ctx.get("sections"):
            # 2-stage job → multimodal agentic editor (decides which section(s) to change)
            res = pipeline.agent_refine(JOBS_DIR / job_id, ctx["score"], ctx["sections"], feedback,
                                        images=images, subject=opts["subject"], voice=opts["voice"],
                                        flow=opts["flow"], tts_backend=opts["tts"],
                                        prev_response_id=ctx.get("response_id"),
                                        max_tokens=opts["max_tokens"], progress=progress)
            ctx["score"] = res["score"]
            ctx["sections"] = res["sections"]
            ctx["response_id"] = res.get("response_id")
            reply = (res.get("reply") or "Done.").strip()
            if res.get("edited"):
                beats = len(res["score"].get("beats", []))
                ctx["chat"].append({"role": "assistant",
                                    "text": reply + f"\n(reloading the preview — {beats} beats)"})
                _invalidate_export()
                rf["beats"] = beats
                rf["rev"] = rf.get("rev", 0) + 1        # signals the UI to reload the player
            else:
                # a clarifying question or no-op — surface the reply, don't reload
                ctx["chat"].append({"role": "assistant", "text": reply})
            rf["stage"] = "done"
            rf["message"] = "updated"
            rf["done"] = True
            return

        # single-shot job → conversational refine of the whole score
        res = pipeline.refine(JOBS_DIR / job_id, ctx["messages"], ctx["score"], feedback,
                              subject=opts["subject"], voice=opts["voice"], flow=opts["flow"],
                              tts_backend=opts["tts"], max_tokens=opts["max_tokens"],
                              progress=progress)
        ctx["messages"] = res["messages"]
        ctx["score"] = res["score"]
        beats = len(res["score"].get("beats", []))
        ctx["chat"].append({"role": "assistant",
                            "text": f"Done — revised lecture has {beats} beats. Reloading the preview."})
        _invalidate_export()
        rf["beats"] = beats
        rf["rev"] = rf.get("rev", 0) + 1
        rf["stage"] = "done"
        rf["message"] = "updated"
        rf["done"] = True
    except Exception as e:
        ctx["chat"].append({"role": "assistant", "text": f"Sorry — revision failed: {e}"})
        rf["error"] = f"{type(e).__name__}: {e}"
        rf["done"] = True


@app.route("/feedback/<job_id>", methods=["POST"])
def feedback(job_id):
    ctx = CTX.get(job_id)
    if not ctx:
        return jsonify({"error": "lecture not ready for feedback yet"}), 404
    body = request.json if request.is_json else None
    msg = ((body.get("message") if body else request.form.get("message")) or "").strip()
    images = (body.get("images") if body else None) or []
    images = [u for u in images if isinstance(u, str) and u.startswith("data:")][:4]
    if not msg and not images:
        return jsonify({"error": "empty feedback"}), 400
    rf = ctx.get("refine") or {}
    if rf and not rf.get("done"):
        return jsonify({"error": "a revision is already in progress"}), 409
    ctx["chat"].append({"role": "user", "text": msg or "(screenshot)", "img": bool(images)})
    ctx["refine"] = {"stage": "queued", "message": "starting…", "done": False,
                     "error": None, "rev": rf.get("rev", 0)}
    threading.Thread(target=_run_feedback, args=(job_id, msg, images), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/feedback_status/<job_id>")
def feedback_status(job_id):
    ctx = CTX.get(job_id)
    if not ctx:
        return jsonify({"error": "unknown job"}), 404
    return jsonify({"refine": ctx.get("refine"), "chat": ctx.get("chat", [])})


def _run_export(job_id: str, backend: str):
    jdir = JOBS_DIR / job_id
    exp = JOBS[job_id]["export"]
    try:
        exp["message"] = "starting video export — this can take a few minutes for long lectures…"
        import export_mp4
        export_mp4.export(jdir, progress=lambda msg: exp.__setitem__("message", msg))
        exp["file"] = "lecture.mp4"
        exp["done"] = True
        exp["message"] = "ready"
    except Exception as e:
        exp["error"] = f"{type(e).__name__}: {e}"
        exp["trace"] = traceback.format_exc()[-1200:]
        exp["done"] = True


@app.route("/export/<job_id>", methods=["POST"])
def export_video(job_id):
    jdir = JOBS_DIR / job_id
    # Resilient to server restarts (in-memory JOBS is lost): export only needs the rendered
    # files on disk, so recreate a minimal job entry if the lecture exists.
    if not (jdir / "index.html").exists() or not (jdir / "audio_manifest.json").exists():
        return jsonify({"error": "lecture not found — please regenerate"}), 404
    job = JOBS.get(job_id)
    if job is None:
        job = JOBS[job_id] = {"done": True, "error": None, "url": f"/lecture/{job_id}/"}
    if job.get("error") or not job.get("done"):
        return jsonify({"error": "lecture not ready"}), 400
    backend = request.args.get("backend", "web")
    exp = job.get("export")
    if exp and not exp.get("done") and exp.get("backend") == backend:
        return jsonify({"ok": True})  # already running
    job["export"] = {"backend": backend, "done": False, "error": None,
                     "file": None, "message": "starting…"}
    threading.Thread(target=_run_export, args=(job_id, backend), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/export_status/<job_id>")
def export_status(job_id):
    job = JOBS.get(job_id)
    if not job or not job.get("export"):
        return jsonify({"error": "no export"}), 404
    return jsonify(job["export"])


@app.route("/download/<job_id>/<backend>")
def download(job_id, backend):
    fname = "lecture.mp4"
    jdir = JOBS_DIR / job_id
    if not (jdir / fname).exists():
        abort(404)
    return send_from_directory(jdir, fname, as_attachment=True,
                               download_name=f"lecture-{backend}-{job_id}.mp4")


@app.route("/lecture/<job_id>/")
def lecture_index(job_id):
    return send_from_directory(JOBS_DIR / job_id, "index.html")


@app.route("/lecture/<job_id>/<path:fname>")
def lecture_file(job_id, fname):
    d = JOBS_DIR / job_id
    if not (d / fname).resolve().is_relative_to(d.resolve()):
        abort(403)
    return send_from_directory(d, fname)


@app.route("/")
def index():
    voice_opts = "".join(f'<option value="{v}">{label}</option>' for v, label in VOICES)
    labels = {"math": "Mathematics", "biology": "Biology", "english": "English",
              "physics": "Physics", "chemistry": "Chemistry"}
    subs = ['<option value="">Any subject (auto)</option>'] + [
        f'<option value="{s}">{labels.get(s, s.title())}</option>' for s in author.list_subjects()]
    return PAGE.replace("__VOICES__", voice_opts).replace("__SUBJECTS__", "".join(subs))


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lecture Studio</title>
<style>
 :root{--ac:#2b6cb0}
 *{box-sizing:border-box} body{margin:0;font-family:system-ui,sans-serif;background:#0f1115;color:#e7ebf0}
 header{padding:18px 26px;border-bottom:1px solid #222833} header h1{margin:0;font-size:20px}
 header span{color:#8b95a3;font-size:13px}
 .wrap{display:grid;grid-template-columns:380px 1fr;gap:0;height:calc(100vh - 59px)}
 form{padding:22px;overflow:auto;border-right:1px solid #222833}
 label{display:block;margin:14px 0 6px;font-size:13px;color:#aeb7c2}
 input[type=file],textarea,select,input[type=text]{width:100%;background:#171b22;border:1px solid #2b3340;
   color:#e7ebf0;border-radius:8px;padding:10px;font-size:14px;font-family:inherit}
 textarea{min-height:84px;resize:vertical}
 .row{display:flex;gap:10px} .row>div{flex:1}
 .hint{font-size:12px;color:#7b8593;margin-top:4px}
 button{margin-top:18px;width:100%;background:var(--ac);color:#fff;border:0;border-radius:8px;
   padding:13px;font-size:15px;font-weight:600;cursor:pointer} button:disabled{background:#3a4250}
 .stage{position:relative;background:#0b0d11;display:flex;flex-direction:column}
 #frame{width:100%;flex:1 1 auto;border:0;display:none;background:#fbfbf7}
 .status{margin:auto;max-width:520px;text-align:center;padding:20px}
 .toolbar{display:none;gap:10px;align-items:center;padding:9px 14px;background:#11151c;
   border-bottom:1px solid #222833;z-index:5}
 .toolbar button{margin:0;width:auto;padding:8px 14px;font-size:13px;font-weight:500;background:#2f9e44}
 .toolbar button:disabled{background:#3a4250}
 .toolbar .lbl{color:#8b95a3;font-size:12px;margin-right:2px}
 .toolbar #dlstatus{color:#8fb7e0;font-size:12px}
 .bar{height:6px;background:#222833;border-radius:3px;overflow:hidden;margin:16px 0}
 .bar>div{height:100%;width:0;background:var(--ac);transition:width .4s}
 .steps{display:flex;justify-content:space-between;font-size:12px;color:#6b7480}
 .steps b{color:var(--ac)} .err{color:#ff8080;white-space:pre-wrap;text-align:left;font-size:12px}
 .tag{display:inline-block;background:#171b22;border:1px solid #2b3340;border-radius:999px;
   padding:3px 10px;font-size:12px;color:#aeb7c2;margin:2px 4px 2px 0;cursor:pointer}
 .tag:hover{border-color:var(--ac)}
 .chat{display:none;flex-direction:column;border-top:1px solid #222833;background:#0d1015;
   height:230px;flex:0 0 auto}
 .chat .msgs{flex:1 1 auto;overflow:auto;padding:12px 14px;display:flex;flex-direction:column;gap:8px}
 .chat .m{max-width:82%;padding:8px 11px;border-radius:12px;font-size:13px;line-height:1.4;white-space:pre-wrap}
 .chat .m.user{align-self:flex-end;background:var(--ac);color:#fff;border-bottom-right-radius:3px}
 .chat .m.bot{align-self:flex-start;background:#1a1f29;color:#cfd6df;border:1px solid #262d39;border-bottom-left-radius:3px}
 .chat .m.work{align-self:flex-start;color:#8fb7e0;font-style:italic;background:transparent;border:0}
 .fbrow{display:flex;gap:8px;padding:10px 12px;border-top:1px solid #1c2230;background:#11151c}
 .fbrow input[type=text]{flex:1} .fbrow button{margin:0;width:auto;padding:9px 16px;font-size:13px}
 #fbattach{padding:9px 12px!important;background:#1c2230} #fbattach:hover{background:#252c3a}
 #fbthumbs{display:none;gap:6px;flex-wrap:wrap;padding:6px 12px 0}
 #fbthumbs span{position:relative;display:inline-block}
 #fbthumbs img{height:46px;border-radius:6px;border:1px solid #2a2f3a;display:block}
 #fbthumbs .thx{position:absolute;top:-7px;right:-7px;width:18px;height:18px;padding:0;margin:0;
   border-radius:50%;background:#c0392b;color:#fff;font-size:12px;line-height:18px;border:none}
 .fbhint{padding:6px 14px 0;color:#6b7480;font-size:11px}
 .sb{display:none;flex-direction:column;flex:1 1 auto;min-height:0;background:#0d1015}
 .sb h3{margin:14px 18px 2px;color:#cfd6df;font-weight:600;font-size:16px}
 .sb .sbhint{color:#7b8593;font-size:12px;margin:0 18px 10px}
 #sbsections{flex:1 1 auto;overflow:auto;padding:0 18px}
 .sec{background:#141922;border:1px solid #232a36;border-radius:10px;padding:10px 13px;margin-bottom:10px}
 .sechead{display:flex;gap:8px;align-items:center;margin-bottom:6px}
 .sechead .secnum{background:var(--ac);color:#fff;font-size:12px;font-weight:700;border-radius:50%;
   width:20px;height:20px;display:flex;align-items:center;justify-content:center;flex:0 0 auto}
 .sechead .sectitle{flex:1;font-weight:600;font-size:14px;color:#e7ebf0}
 .sechead .skill{color:#6b7480;font-size:11px;white-space:nowrap}
 .beat{display:flex;gap:7px;align-items:flex-start;margin:3px 0 3px 6px;font-size:13px;color:#cfd6df;line-height:1.4}
 .beat .dot{color:#4a5563} .beat .bsay{flex:1}
 .vis{color:#5b6470;font-size:11px;margin:0 0 5px 24px}
 .sbactions{display:flex;gap:10px;align-items:center;padding:11px 16px;border-top:1px solid #222833;background:#11151c}
 .sbactions button{margin:0;width:auto;padding:11px 18px;background:#2f9e44}
 .sbactions button:disabled{background:#3a4250}
 .ckrow{display:flex;align-items:center;gap:8px;margin:14px 0 4px;color:#aeb7c2;font-size:13px}
 .ckrow input{width:auto}
</style></head><body>
<header><h1>📐 Lecture Studio <span>· voice-narrated whiteboard from your material</span></h1></header>
<div class="wrap">
 <form id="f">
   <label>Subject</label>
   <select name="subject">__SUBJECTS__</select>
   <label>Material (image, PDF, Word, or text)</label>
   <input type="file" name="material" id="mat" accept=".png,.jpg,.jpeg,.webp,.pdf,.docx,.md,.txt" required>
   <div class="hint">Images & PDFs are read with gpt-5.4 vision (diagrams & equations preserved).</div>

   <label>Guidance prompt — tell the AI how to teach it</label>
   <textarea name="prompt" id="prompt" placeholder="e.g. Build intuition first. Go slowly through the difference-of-squares step and circle the two squares. Encouraging tone, like a friendly tutor."></textarea>
   <div id="examples"></div>

   <label>Key points to emphasize / highlight (optional)</label>
   <input type="text" name="highlights" placeholder="e.g. why the cross terms cancel">

   <div class="row">
     <div><label>Audience</label><select name="level">
       <option>Elementary (K–5)</option><option>Middle school (6–8)</option>
       <option selected>High school (9–12)</option><option>AP / Honors</option>
       <option>College / intro</option></select></div>
     <div><label>Pace</label><select name="pace">
       <option value="slow">Slow & thorough</option><option value="normal" selected>Normal</option>
       <option value="brisk">Brisk</option></select></div>
   </div>
   <div class="row">
     <div><label>Tone</label><select name="tone">
       <option>warm & encouraging</option><option>calm & patient</option>
       <option>energetic & enthusiastic</option><option>formal & precise</option><option>playful</option>
     </select></div>
     <div><label>Board</label><select name="flow">
       <option value="page" selected>Paged</option><option value="scroll">Endless scroll</option></select></div>
   </div>
   <div class="row">
     <div><label>Authoring</label><select name="author_mode">
       <option value="2stage" selected>2-stage — plan then draw (faster, recommended)</option>
       <option value="single">Single-shot</option></select></div>
     <div><label>Lesson length</label><select name="length">
       <option value="auto" selected>Auto — match the material</option>
       <option value="concise">Concise (~3–5 min)</option>
       <option value="standard">Standard (~6–10 min)</option>
       <option value="in_depth">In-depth (~12–15 min)</option></select></div>
   </div>
   <label>Narrator voice</label>
   <select name="voice">__VOICES__</select>
   <label>How to read the material</label>
   <select name="ingest_mode">
     <option value="image">Send image/PDF straight to the AI — best for graphs &amp; diagrams</option>
     <option value="vision">Transcribe to text first (vision)</option>
     <option value="text">Plain text extraction</option>
   </select>

   <label class="ckrow"><input type="checkbox" name="review" id="review" checked>
     Review &amp; edit the plan before generating (2-stage only)</label>
   <button type="submit" id="go">Generate lecture ▶</button>
 </form>

 <div class="stage">
   <div class="toolbar" id="toolbar">
     <span class="lbl">Download:</span>
     <button id="dlweb">⬇ Video (MP4)</button>
     <span id="dlstatus"></span>
   </div>
   <iframe id="frame" allow="autoplay"></iframe>
   <div class="status" id="status">
     <h2 style="color:#aeb7c2;font-weight:500">Your lecture will appear here</h2>
     <p style="color:#6b7480">Upload material, describe how you want it taught, and hit Generate.</p>
   </div>
   <div class="sb" id="sb">
     <h3>📝 Review &amp; edit your plan</h3>
     <div class="sbhint">Here's the plan. Tell the AI what to change (or generate as-is).</div>
     <div id="sbsections"></div>
     <div class="msgs" id="sbmsgs" style="flex:0 0 auto;max-height:130px"></div>
     <form class="fbrow" id="sbform">
       <input id="sbinput" type="text" autocomplete="off"
         placeholder="Ask the AI: 'merge the last two sections' · 'add a section on completing the square' · 'shorten the intro'">
       <button type="submit" id="sbsend">Ask AI</button>
     </form>
     <div class="sbactions">
       <button id="sbgen">✓ Generate lecture from this plan</button>
       <span id="sbstat" style="color:#8b95a3;font-size:12px"></span>
     </div>
   </div>
   <div class="chat" id="chat">
     <div class="fbhint">💬 Refine this lecture — tell the AI what to change (multi-turn). Attach or paste (Ctrl+V) a screenshot of the part you mean. It revises and reloads the preview.</div>
     <div class="msgs" id="msgs"></div>
     <div id="fbthumbs"></div>
     <form class="fbrow" id="fbform">
       <button type="button" id="fbattach" title="Attach a screenshot">📎</button>
       <input id="fbinput" type="text" autocomplete="off"
         placeholder="e.g. circle the squares in the factoring step · enlarge the graph · (paste a screenshot)">
       <button type="submit" id="fbsend">Send</button>
       <input id="fbfile" type="file" accept="image/*" multiple style="display:none">
     </form>
   </div>
 </div>
</div>
<script>
const EX=["Build intuition before the formula.","Go slow and pause after each step.",
  "Emphasize the key pattern and circle it.","Friendly tutor tone for a struggling student.",
  "Keep it brisk — just the essential steps."];
document.getElementById("examples").innerHTML =
  EX.map(e=>`<span class="tag">${e}</span>`).join("");
document.querySelectorAll("#examples .tag").forEach(t=>t.onclick=()=>{
  const p=document.getElementById("prompt"); p.value=(p.value?p.value+" ":"")+t.textContent;});

const STEPS=["ingest","author","tts","render","done"];
const f=document.getElementById("f"), statusEl=document.getElementById("status"),
      frame=document.getElementById("frame"), go=document.getElementById("go");
f.onsubmit=async ev=>{
  ev.preventDefault();
  if(!document.getElementById("mat").files.length){return;}
  const review = document.getElementById("review").checked && f.author_mode.value==="2stage";
  go.disabled=true; frame.style.display="none"; document.getElementById("chat").style.display="none";
  document.getElementById("sb").style.display="none"; document.getElementById("toolbar").style.display="none";
  statusEl.style.display="block"; window.__t0=Date.now();
  if(review){
    render({stage:"queued",message:"planning the outline…"});
    const res=await fetch("/outline",{method:"POST",body:new FormData(f)});
    const j=await res.json();
    if(j.error){ render({error:j.error}); go.disabled=false; return; }
    pollOutline(j.job_id); return;
  }
  render({stage:"queued",message:"uploading…"});
  const res=await fetch("/generate",{method:"POST",body:new FormData(f)});
  const j=await res.json();
  if(j.error){ render({error:j.error}); go.disabled=false; return; }
  poll(j.job_id);
};
function pct(stage){ const i=STEPS.indexOf(stage); return i<0?5:Math.round((i+0.5)/STEPS.length*100); }
function render(s){
  if(s.error){ statusEl.innerHTML=`<h3>Something went wrong</h3><div class="err">${s.error}\n${s.trace||""}</div>`; return; }
  const el=Math.round((Date.now()-(window.__t0||Date.now()))/1000);
  const note = s.stage==="author" ? '<div style="color:#7b8593;font-size:12px;margin-top:6px">gpt-5.4 is reasoning — this step takes ~1–2 min.</div>' : '';
  statusEl.innerHTML=`<h2 style="color:#cfd6df;font-weight:500">${s.message||s.stage||""}</h2>
    <div class="bar"><div style="width:${pct(s.stage)}%"></div></div>
    <div class="steps">${STEPS.map(x=>`<span ${x===s.stage?'style="color:var(--ac)"':''}>${x}</span>`).join("")}</div>
    <div style="color:#8b95a3;font-size:12px;margin-top:8px">⏱ ${el}s elapsed</div>${note}`;
}
async function poll(id){
  const r=await fetch("/status/"+id); const s=await r.json();
  if(s.error){ render(s); go.disabled=false; return; }
  render(s);
  if(s.done && s.url){ statusEl.style.display="none"; frame.style.display="block";
    frame.src=s.url+"?autoplay=1"; go.disabled=false;
    window.curJob=id; document.getElementById("toolbar").style.display="flex";
    document.getElementById("dlstatus").textContent=""; initChat(); return; }
  setTimeout(()=>poll(id), 900);
}

// ---------------- storyboard (Stage-1 review + edit) ----------------
const sb=document.getElementById("sb"), sbsections=document.getElementById("sbsections"),
      sbform=document.getElementById("sbform"), sbinput=document.getElementById("sbinput"),
      sbsend=document.getElementById("sbsend"), sbgen=document.getElementById("sbgen"),
      sbstat=document.getElementById("sbstat"), sbmsgs=document.getElementById("sbmsgs");
let PLAN=null;
function esc2(s){return (s||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
async function pollOutline(id){
  const r=await fetch("/outline_status/"+id); const s=await r.json();
  if(s.error){ render(s); go.disabled=false; return; }
  render({stage:s.stage,message:s.message});
  if(s.done && s.plan){ window.__outlineJob=id; PLAN=s.plan; statusEl.style.display="none";
    sb.style.display="flex"; sbmsgs.innerHTML=""; renderSB(); go.disabled=false; return; }
  if(s.done && s.error){ render(s); go.disabled=false; return; }
  setTimeout(()=>pollOutline(id),900);
}
function renderSB(){
  const secs=PLAN.sections||[];
  sbsections.innerHTML = secs.map((s,si)=>`
    <div class="sec">
      <div class="sechead">
        <span class="secnum">${si+1}</span>
        <span class="sectitle">${esc2(s.title||"")}</span>
        <span class="skill">${esc2((((s.skill||"").split("/").slice(-2)[0])||""))}</span>
      </div>
      ${(s.beats||[]).map((b)=>`
        <div class="beat"><span class="dot">•</span><span class="bsay">${esc2(b.say||"")}</span></div>${b.visual?`<div class="vis">🎬 ${esc2(b.visual)}</div>`:""}`).join("")}
    </div>`).join("");
  const nb=secs.reduce((n,s)=>n+(s.beats||[]).length,0);
  sbstat.textContent=`${secs.length} section(s) · ${nb} beats`;
}
sbform.onsubmit=async ev=>{ ev.preventDefault();
  const msg=sbinput.value.trim(); if(!msg||sbsend.disabled)return; sbinput.value=""; sbsend.disabled=true;
  sbmsgs.innerHTML+=`<div class="m user">${esc2(msg)}</div><div class="m bot work" id="sbwork">✍️ revising the plan…</div>`;
  sbmsgs.scrollTop=sbmsgs.scrollHeight;
  try{
    const r=await fetch("/outline_refine/"+window.__outlineJob,{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify({plan:PLAN,message:msg})});
    const j=await r.json(); const w=document.getElementById("sbwork"); if(w)w.remove();
    if(j.error){ sbmsgs.innerHTML+=`<div class="m bot">⚠ ${esc2(j.error)}</div>`; }
    else { PLAN=j.plan; renderSB(); sbmsgs.innerHTML+=`<div class="m bot">Updated the plan ✓</div>`; }
  }catch(e){ const w=document.getElementById("sbwork"); if(w)w.remove(); }
  sbsend.disabled=false; sbmsgs.scrollTop=sbmsgs.scrollHeight;
};
sbgen.onclick=async ()=>{
  sbgen.disabled=true; sbstat.textContent="starting generation…";
  const r=await fetch("/generate_from_outline/"+window.__outlineJob,{method:"POST",
    headers:{"Content-Type":"application/json"},body:JSON.stringify({plan:PLAN})});
  const j=await r.json();
  if(j.error){ sbstat.textContent="✗ "+j.error; sbgen.disabled=false; return; }
  sb.style.display="none"; statusEl.style.display="block"; window.__t0=Date.now(); go.disabled=true;
  poll(window.__outlineJob);
};

const dlweb=document.getElementById("dlweb"), dlstatus=document.getElementById("dlstatus");
dlweb.onclick=()=>exportVideo("web");
async function exportVideo(backend){
  if(!window.curJob) return;
  dlweb.disabled=true; window.__ex0=Date.now();
  dlstatus.textContent = "starting export…";
  await fetch("/export/"+window.curJob+"?backend="+backend, {method:"POST"});
  pollExport(window.curJob, backend);
}
async function pollExport(id, backend){
  const r=await fetch("/export_status/"+id); const s=await r.json();
  if(s.error){ dlstatus.textContent="✗ "+s.error; dlweb.disabled=false; return; }
  const el=Math.round((Date.now()-(window.__ex0||Date.now()))/1000);
  if(s.done){
    // file is ready — trigger the browser download. We can't detect when the browser finishes
    // saving, so show a terminal message rather than a stuck "downloading…".
    const a=document.createElement("a"); a.href="/download/"+id+"/"+backend; a.download="";
    document.body.appendChild(a); a.click(); a.remove();
    dlstatus.textContent="✓ saved to your browser's Downloads";
    dlweb.disabled=false; return;
  }
  dlstatus.textContent=`${s.message||"working…"}  ·  ${el}s`;
  setTimeout(()=>pollExport(id, backend), 1500);
}

// ---------------- feedback chat: refine the lecture multi-turn (text + screenshots) ----------------
const chat=document.getElementById("chat"), msgs=document.getElementById("msgs"),
      fbform=document.getElementById("fbform"), fbinput=document.getElementById("fbinput"),
      fbsend=document.getElementById("fbsend"), fbattach=document.getElementById("fbattach"),
      fbfile=document.getElementById("fbfile"), fbthumbs=document.getElementById("fbthumbs");
let fbBusy=false, pendingImages=[], fbLastRev=0;
function esc(s){ return (s||"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c])); }
function renderChat(items, working){
  msgs.innerHTML = (items||[]).map(m=>`<div class="m ${m.role==='user'?'user':'bot'}">${m.img?'📷 ':''}${esc(m.text)}</div>`).join("")
    + (working?`<div class="m work">✍️ ${esc(working)}</div>`:"");
  msgs.scrollTop = msgs.scrollHeight;
}
function renderThumbs(){
  fbthumbs.innerHTML = pendingImages.map((u,i)=>
    `<span><img src="${u}"><button type="button" class="thx" data-i="${i}">×</button></span>`).join("");
  fbthumbs.style.display = pendingImages.length ? "flex" : "none";
  fbthumbs.querySelectorAll(".thx").forEach(b=>b.onclick=()=>{ pendingImages.splice(+b.dataset.i,1); renderThumbs(); });
}
function addImageFile(file){
  if(!file || !file.type || !file.type.startsWith("image/")) return;
  const rd=new FileReader();
  rd.onload=()=>{ const img=new Image(); img.onload=()=>{
    const max=1024, sc=Math.min(1, max/Math.max(img.width,img.height));
    const c=document.createElement("canvas");
    c.width=Math.max(1,Math.round(img.width*sc)); c.height=Math.max(1,Math.round(img.height*sc));
    c.getContext("2d").drawImage(img,0,0,c.width,c.height);
    pendingImages.push(c.toDataURL("image/jpeg",0.85)); renderThumbs();
  }; img.src=rd.result; };
  rd.readAsDataURL(file);
}
fbattach.onclick=()=>fbfile.click();
fbfile.onchange=()=>{ [...fbfile.files].forEach(addImageFile); fbfile.value=""; };
fbinput.addEventListener("paste", ev=>{
  const items=(ev.clipboardData||{}).items||[];
  let got=false;
  for(const it of items){ if(it.type && it.type.startsWith("image/")){ addImageFile(it.getAsFile()); got=true; } }
  if(got) ev.preventDefault();
});
function initChat(){ chat.style.display="flex"; fbBusy=false; fbsend.disabled=false;
  pendingImages=[]; renderThumbs(); renderChat([], ""); fbLastRev=0; }
function reloadPreview(){ if(window.curJob) frame.src="/lecture/"+window.curJob+"/?autoplay=1&t="+Date.now(); }
function resetDownload(){ dlweb.disabled=false; dlstatus.textContent=""; }
fbform.onsubmit=async ev=>{
  ev.preventDefault();
  const msg=fbinput.value.trim();
  if((!msg && !pendingImages.length) || fbBusy || !window.curJob) return;
  fbBusy=true; fbsend.disabled=true;
  const imgs=pendingImages.slice(); fbinput.value=""; pendingImages=[]; renderThumbs();
  const r=await fetch("/feedback/"+window.curJob,{method:"POST",
    headers:{"Content-Type":"application/json"},body:JSON.stringify({message:msg,images:imgs})});
  const j=await r.json();
  if(j.error){ fbBusy=false; fbsend.disabled=false; alert(j.error); return; }
  pollFeedback(window.curJob);
};
async function pollFeedback(id){
  const r=await fetch("/feedback_status/"+id); const s=await r.json();
  if(s.error){ fbBusy=false; fbsend.disabled=false; return; }
  const rf=s.refine||{};
  renderChat(s.chat, rf.done?"":((rf.stage||"working")+(rf.message?" · "+rf.message:"")));
  if(rf.done){
    if(!rf.error && (rf.rev||0)!==fbLastRev){ fbLastRev=rf.rev||0; reloadPreview(); resetDownload(); }
    fbBusy=false; fbsend.disabled=false; return; }
  setTimeout(()=>pollFeedback(id), 900);
}
</script></body></html>"""


if __name__ == "__main__":
    print("Lecture Studio → http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, threaded=True)
