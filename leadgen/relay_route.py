"""Model order for Relay Fast.

claude-sonnet-5 is the primary model for lead-gen LLM work (first touch,
angles, qualify). When it is missing or keeps failing, the same request moves
to claude-haiku-5-5 and then to the other models currently on the gateway;
kimi-k3 is not used. A dead model stays skipped for the rest of the process.
One blip does not retire it. An empty/null completion counts as a failure:
the ladder walks on instead of handing the caller a None.
"""
from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request

PRIMARY = "claude-sonnet-5"
# claude-haiku-5-5 is the in-family backup; grok and the flash models cover a
# full Claude outage on the gateway. kimi-k3 is deliberately not in the ladder.
FALLBACKS = (
    "claude-haiku-5-5",
    "grok-4.7",
    "grok-4.6",
    "grok-4.5",
    "gemini-3.8-flash",
    "deepseek-v4-flash",
    "glm-5.3-flash",
)

_down: set[str] = set()
_strikes: dict[str, int] = {}
_catalog: set[str] | None = None
_loaded = False


def preferred() -> str:
    # Deliberately does NOT read LLM_MODEL: that var belongs to the bot
    # (kimi-k3). Lead-gen scripts get their own override.
    return (os.getenv("LEADGEN_LLM_MODEL") or PRIMARY).strip() or PRIMARY


def reset(catalog: set[str] | None = None, *, loaded: bool = True) -> None:
    """Test hook. Production loads the live catalog on the first chain()."""
    global _catalog, _loaded
    _down.clear()
    _strikes.clear()
    _catalog = catalog
    _loaded = loaded


def _model_missing(body: str) -> bool:
    text = (body or "").lower()
    if "model" not in text:
        return False
    return any(word in text for word in (
        "not found", "no such", "does not exist", "unknown", "not available",
    ))


def note_failure(model: str, status: int, body: str = "") -> bool:
    """Remember a failed call. True means the caller should try the next model.

    Auth and billing errors stop the ladder: another model cannot fix them.
    A missing model is retired immediately. A timeout or a 5xx gets one more
    chance on the next batch before it is retired.
    """
    if status in {401, 402}:
        return False
    permanent = status in {403, 404} or _model_missing(body)
    transient = status in {0, 408, 409, 429, 500, 502, 503, 529}
    if status in {400, 422} and not _model_missing(body):
        return False
    if not permanent and not transient:
        return False
    if permanent:
        _down.add(model)
    else:
        _strikes[model] = _strikes.get(model, 0) + 1
        if _strikes[model] >= 2:
            _down.add(model)
    return True


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(certifi.where())
    except Exception:
        pass
    return ctx


def endpoint() -> tuple[str, str]:
    key = (os.getenv("RELAYFAST_API_KEY") or os.getenv("LLM_API_KEY") or "").strip()
    base = (os.getenv("LLM_BASE_URL") or "https://relay.fast/v1").rstrip("/")
    return key, base


def chat(messages: list[dict], *, max_tokens: int = 400, temperature: float = 0.4,
         timeout: int = 45) -> str:
    """One completion. Walks the model ladder when the current model is down."""
    key, base = endpoint()
    if not key:
        raise RuntimeError("RELAYFAST_API_KEY not set")
    ctx = _ssl_context()
    last = "LLM failed"
    for model in chain():
        body = json.dumps({
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }, ensure_ascii=False).encode()
        req = urllib.request.Request(
            base + "/chat/completions",
            data=body,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode())
            content = payload["choices"][0]["message"]["content"]
            if not (isinstance(content, str) and content.strip()):
                last = f"{model} empty content"
                if note_failure(model, 0, ""):
                    continue
                raise RuntimeError(last)
            return content
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            last = f"{model} {exc.code}"
            if note_failure(model, exc.code, detail):
                continue
            raise
        except (OSError, TimeoutError, json.JSONDecodeError, KeyError, IndexError) as exc:
            last = f"{model} {exc.__class__.__name__}"
            if note_failure(model, 0, ""):
                continue
            raise
    raise RuntimeError(last)


def _ensure_catalog() -> set[str] | None:
    global _catalog, _loaded
    if _loaded:
        return _catalog
    _loaded = True
    key, base = endpoint()
    if not key:
        _catalog = None
        return None
    try:
        req = urllib.request.Request(
            base + "/models",
            headers={"Authorization": f"Bearer {key}"},
        )
        with urllib.request.urlopen(req, timeout=15, context=_ssl_context()) as resp:
            payload = json.loads(resp.read().decode())
        ids = {
            str(item.get("id") or "")
            for item in (payload.get("data") or [])
            if isinstance(item, dict)
        }
        _catalog = {item for item in ids if item} or None
    except (OSError, ValueError, json.JSONDecodeError):
        _catalog = None
    return _catalog


def chain() -> list[str]:
    names: list[str] = []
    for name in (preferred(), *FALLBACKS):
        if name and name not in names and name not in _down:
            names.append(name)
    live = _ensure_catalog()
    if not live:
        return names
    listed = [name for name in names if name in live]
    return listed or names
