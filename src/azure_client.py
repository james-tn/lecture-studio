"""Thin client for the Azure OpenAI v1 endpoint using AAD (az CLI) auth.

No API keys: a bearer token is fetched from the logged-in `az` session and cached
until shortly before expiry.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

import requests


def _load_dotenv() -> None:
    """Load KEY=VALUE lines from a local, untracked `.env` so real resource/endpoint names live on
    the machine, never in this (public) source tree. Existing environment variables take priority."""
    for p in (Path(__file__).resolve().parents[1] / ".env", Path.cwd() / ".env"):
        try:
            for line in p.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        except FileNotFoundError:
            pass


_load_dotenv()

# Endpoint + resource names are read from the environment (see .env.example). The defaults are
# placeholders only — set AZURE_OPENAI_ENDPOINT (and friends) in a local .env or your shell.
ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT",
                          "https://<your-resource>.openai.azure.com/openai/v1").rstrip("/")
RESOURCE = "https://cognitiveservices.azure.com"   # AAD token audience for chat/TTS (generic)

CHAT_MODEL = os.environ.get("AZURE_CHAT_MODEL", "gpt-5.4")   # gpt-5.5 requested but has 0 quota here
TTS_MODEL = os.environ.get("AZURE_TTS_MODEL", "gpt-4o-mini-tts")   # OpenAI TTS (fallback backend)

# Azure Speech (kind=AIServices on the same resource supports keyless AAD Speech).
SPEECH_REGION = os.environ.get("AZURE_SPEECH_REGION", "<region>")
SPEECH_RG = os.environ.get("AZURE_SPEECH_RESOURCE_GROUP", "<resource-group>")
SPEECH_ACCOUNT = os.environ.get("AZURE_SPEECH_ACCOUNT", "<your-resource>")
_sub_cache: dict[str, str] = {}


def _subscription_id() -> str:
    if "id" not in _sub_cache:
        out = subprocess.run(["az", "account", "show", "-o", "json"],
                             capture_output=True, text=True, check=True).stdout
        _sub_cache["id"] = json.loads(out)["id"]
    return _sub_cache["id"]


def speech_auth() -> tuple[str, str]:
    """Return (auth_token_string, region) for the Speech SDK using AAD (no key)."""
    rid = (f"/subscriptions/{_subscription_id()}/resourceGroups/{SPEECH_RG}"
           f"/providers/Microsoft.CognitiveServices/accounts/{SPEECH_ACCOUNT}")
    return f"aad#{rid}#{get_token()}", SPEECH_REGION

_token_cache: dict[str, dict[str, Any]] = {}   # resource -> {"value":..., "exp":...}


def get_token(resource: str = RESOURCE) -> str:
    """Return a cached AAD bearer token for `resource`, refreshing ~2 min before expiry.

    Different Azure surfaces need different audiences: chat/TTS use `cognitiveservices.azure.com`,
    while image generation lives on the Foundry endpoint and needs `ai.azure.com`.
    """
    now = time.time()
    c = _token_cache.get(resource)
    if c and now < c["exp"] - 120:
        return c["value"]
    out = subprocess.run(
        ["az", "account", "get-access-token", "--resource", resource, "-o", "json"],
        capture_output=True, text=True, check=True,
    ).stdout
    data = json.loads(out)
    # Use the token's REAL expiry (`expires_on`, epoch seconds). az often returns a token already
    # partway through its lifetime, so assuming a fixed +50min can serve an expired token -> 401.
    exp = data.get("expires_on")
    try:
        exp = float(exp)
    except (TypeError, ValueError):
        exp = now + 30 * 60
    _token_cache[resource] = {"value": data["accessToken"], "exp": exp}
    return data["accessToken"]


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {get_token()}", "Content-Type": "application/json"}


def _invalidate_token(resource: str = RESOURCE) -> None:
    _token_cache.pop(resource, None)


def chat(messages: list[dict], model: str = CHAT_MODEL, **kwargs) -> str:
    """Chat completion -> assistant message content string. Retries once on 401 with a fresh token."""
    body = {"model": model, "messages": messages, **kwargs}
    for attempt in (1, 2):
        r = requests.post(f"{ENDPOINT}/chat/completions", headers=_headers(),
                          data=json.dumps(body), timeout=600)
        if r.status_code == 401 and attempt == 1:
            _invalidate_token()          # stale/expired token -> force a refresh and retry
            continue
        if r.status_code != 200:
            raise RuntimeError(f"chat {r.status_code}: {r.text[:800]}")
        return r.json()["choices"][0]["message"]["content"]


def respond(input, model: str = CHAT_MODEL, *, instructions: str | None = None,
            tools: list | None = None, tool_choice=None, previous_response_id: str | None = None,
            reasoning_effort: str | None = None, max_output_tokens: int | None = None) -> dict:
    """Call the Responses API (stateful multi-turn + tool calling + multimodal). Returns the
    full response JSON. Retries once on 401."""
    body: dict[str, Any] = {"model": model, "input": input}
    if instructions:
        body["instructions"] = instructions
    if tools:
        body["tools"] = tools
    if tool_choice is not None:
        body["tool_choice"] = tool_choice
    if previous_response_id:
        body["previous_response_id"] = previous_response_id
    if reasoning_effort:
        body["reasoning"] = {"effort": reasoning_effort}
    if max_output_tokens:
        body["max_output_tokens"] = max_output_tokens
    for attempt in (1, 2):
        r = requests.post(f"{ENDPOINT}/responses", headers=_headers(),
                          data=json.dumps(body), timeout=600)
        if r.status_code == 401 and attempt == 1:
            _invalidate_token()
            continue
        if r.status_code != 200:
            raise RuntimeError(f"responses {r.status_code}: {r.text[:800]}")
        return r.json()


def response_tool_calls(resp: dict) -> list[dict]:
    """Function/tool calls in a Responses API result: [{name, arguments(dict), call_id}]."""
    out = []
    for o in resp.get("output", []):
        if o.get("type") == "function_call":
            try:
                args = json.loads(o.get("arguments") or "{}")
            except Exception:
                args = {}
            out.append({"name": o.get("name"), "arguments": args, "call_id": o.get("call_id")})
    return out


def response_text(resp: dict) -> str:
    """Concatenated assistant text from a Responses API result."""
    parts = []
    for o in resp.get("output", []):
        if o.get("type") == "message":
            for c in o.get("content", []):
                if c.get("type") in ("output_text", "text"):
                    parts.append(c.get("text", ""))
    return "".join(parts).strip()


def tts(text: str, voice: str = "alloy", instructions: str | None = None,
        model: str = TTS_MODEL, fmt: str = "mp3") -> bytes:
    """Text-to-speech -> audio bytes. `instructions` steers tone/delivery."""
    body: dict[str, Any] = {"model": model, "input": text, "voice": voice,
                            "response_format": fmt}
    if instructions:
        body["instructions"] = instructions
    r = requests.post(f"{ENDPOINT}/audio/speech", headers=_headers(),
                      data=json.dumps(body), timeout=300)
    if r.status_code != 200:
        raise RuntimeError(f"tts {r.status_code}: {r.text[:500]}")
    return r.content


# Image generation lives on the Foundry (AI Services) endpoint with the `ai.azure.com` audience —
# a different host + token audience than chat/TTS. Two backends:
#   * "flux"      — FLUX.2-pro (Black Forest Labs): fast (~7s) and higher RPM -> the default for
#                   text->image.
#   * "gpt-image" — gpt-image-2 (OpenAI images API): supports image->image edits, but only ~2 RPM.
# Image-to-image grounding always uses gpt-image (FLUX's text path takes no image input).
IMAGE_RESOURCE = "https://ai.azure.com"
IMAGE_BACKEND = os.environ.get("AZURE_IMAGE_BACKEND", "flux").lower()      # "flux" | "gpt-image"

# Foundry (AI Services) host — placeholder default; set AZURE_AI_SERVICES_ENDPOINT in your .env.
AI_SERVICES_HOST = os.environ.get("AZURE_AI_SERVICES_ENDPOINT",
                                  "https://<your-resource>.services.ai.azure.com").rstrip("/")
GPT_IMAGE_MODEL = os.environ.get("AZURE_IMAGE_MODEL", "gpt-image-2")
IMAGE_ENDPOINT = os.environ.get("AZURE_IMAGE_ENDPOINT", f"{AI_SERVICES_HOST}/openai/v1")
FLUX_MODEL = os.environ.get("AZURE_FLUX_MODEL", "FLUX.2-pro")
FLUX_URL = os.environ.get(
    "AZURE_FLUX_ENDPOINT",
    f"{AI_SERVICES_HOST}/providers/blackforestlabs/v1/flux-2-pro?api-version=preview")

# Per-backend request/minute quotas. A process-global limiter paces ALL callers (parallel asset
# stage, concurrent jobs) so we never trip 429s. gpt-image-2 is only 2/min on this resource.
GPT_IMAGE_RPM = int(os.environ.get("AZURE_IMAGE_RPM", "2"))
FLUX_RPM = int(os.environ.get("AZURE_FLUX_RPM", "8"))
IMAGE_RPM = GPT_IMAGE_RPM   # back-compat alias


class _RateLimiter:
    """Allow at most `max_calls` starts per rolling `period` seconds (thread-safe)."""

    def __init__(self, max_calls: int, period: float):
        self.max_calls = max(1, max_calls)
        self.period = period
        self._starts: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._starts and now - self._starts[0] >= self.period:
                    self._starts.popleft()
                if len(self._starts) < self.max_calls:
                    self._starts.append(now)
                    return
                wait = self.period - (now - self._starts[0]) + 0.1
            time.sleep(max(wait, 0.0))


_limiters = {"flux": _RateLimiter(FLUX_RPM, 60.0),
             "gpt-image": _RateLimiter(GPT_IMAGE_RPM, 60.0)}


def rpm_for(backend: str) -> int:
    return GPT_IMAGE_RPM if backend == "gpt-image" else FLUX_RPM


def _img_bytes(x) -> bytes:
    """Accept a data-URL, raw base64, bytes, or file path -> raw image bytes."""
    if isinstance(x, (bytes, bytearray)):
        return bytes(x)
    if isinstance(x, str):
        if x.startswith("data:"):
            return base64.b64decode(x.split(",", 1)[1])
        p = Path(x)
        if p.exists():
            return p.read_bytes()
        return base64.b64decode(x)
    raise ValueError("unsupported image input")


def _data_url(b64: str) -> str:
    try:
        head = base64.b64decode(b64[:4])          # first 4 b64 chars -> 3 bytes (magic)
    except Exception:
        head = b""
    mime = "image/jpeg" if head[:3] == b"\xff\xd8\xff" else "image/png"
    return f"data:{mime};base64,{b64}"


def generate_image(prompt: str, size: str = "1024x1024", images: list | None = None,
                   n: int = 1, backend: str | None = None, max_retries: int = 4) -> str:
    """Text->image (fast FLUX backend by default), or image+prompt->image when `images` is given
    (forces the gpt-image backend, which supports edits). Returns an image data-URL.

    Paced per-backend (FLUX higher RPM; gpt-image ~2/min) and retries on 429 honoring Retry-After.
    `images` is an optional list of reference inputs (data-URLs / bytes / file paths)."""
    backend = "gpt-image" if images else (backend or IMAGE_BACKEND)
    if backend not in _limiters:
        backend = "gpt-image"
    lim = _limiters[backend]
    try:
        w, h = (int(v) for v in str(size).lower().split("x"))
    except Exception:
        w, h = 1024, 1024

    for attempt in range(1, max_retries + 1):
        auth = {"Authorization": f"Bearer {get_token(IMAGE_RESOURCE)}"}
        lim.acquire()
        if backend == "flux":
            r = requests.post(FLUX_URL, headers={**auth, "Content-Type": "application/json"},
                              data=json.dumps({"prompt": prompt, "model": FLUX_MODEL,
                                               "width": w, "height": h, "n": n}), timeout=240)
        elif images:
            files = [("image", (f"ref{i}.png", _img_bytes(im), "image/png"))
                     for i, im in enumerate(images)]
            r = requests.post(f"{IMAGE_ENDPOINT}/images/edits", headers=auth,
                              data={"model": GPT_IMAGE_MODEL, "prompt": prompt,
                                    "size": size, "n": str(n)}, files=files, timeout=240)
        else:
            r = requests.post(f"{IMAGE_ENDPOINT}/images/generations",
                              headers={**auth, "Content-Type": "application/json"},
                              data=json.dumps({"model": GPT_IMAGE_MODEL, "prompt": prompt,
                                               "size": size, "n": n}), timeout=240)
        if r.status_code == 429 and attempt < max_retries:
            wait = float(r.headers.get("retry-after") or r.headers.get("Retry-After") or 30)
            time.sleep(min(wait, 60) + 1)
            continue
        if r.status_code != 200:
            raise RuntimeError(f"image {r.status_code}: {r.text[:200]}")
        d = r.json()["data"][0]
        return _data_url(d["b64_json"]) if d.get("b64_json") else d["url"]
    raise RuntimeError("image generation failed: rate-limit retries exhausted (429)")
    raise RuntimeError("image generation failed: rate-limit retries exhausted (429)")


if __name__ == "__main__":
    # Smoke test
    print("chat:", chat([{"role": "user", "content": "Reply with exactly: OK"}],
                        max_completion_tokens=2000))
    audio = tts("Difference of squares.", voice="sage", instructions="warm teacher tone")
    Path("out").mkdir(exist_ok=True)
    Path("out/_smoke.mp3").write_bytes(audio)
    print("tts bytes:", len(audio))
