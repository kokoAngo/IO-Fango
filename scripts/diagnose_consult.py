#!/usr/bin/env python3
"""Diagnose why fango_consult returns "システムが混雑しています".

That message is the consult engine's degraded/fallback text. It appears when the
Gemini engine can't run — missing key, missing SDK, bad key, or a failing API
call (quota / region block / network). This script reproduces the *exact* code
path the consult tool uses and prints which condition is hit, so you don't have
to guess from logs.

Run it ON THE SERVER, from the repo root, with the same .env the service uses:

    .venv/bin/python scripts/diagnose_consult.py

It loads .env itself (via python-dotenv) so the result matches what a freshly
(re)started service would see — independent of how your shell is set up.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> str:
    env = ROOT / ".env"
    if not env.exists():
        return f"no .env at {env} (so the service has no secrets unless exported)"
    try:
        from dotenv import load_dotenv  # python-dotenv, already a dependency
        load_dotenv(env, override=False)
        return f"loaded {env}"
    except Exception as exc:  # pragma: no cover
        return f"could NOT load {env}: {exc!r}"


def main() -> int:
    print("=== fango_consult diagnostic ===")
    print(_load_env())

    key = os.environ.get("GEMINI_API_KEY") or ""
    model = os.environ.get("FANGO_CONSULT_MODEL", "gemini-2.5-flash")
    print(f"GEMINI_API_KEY present: {bool(key)}  (len={len(key)})")
    print(f"FANGO_CONSULT_MODEL: {model}")

    # 1) SDK installed?
    try:
        import google.genai  # noqa: F401
        print("google-genai import: OK")
    except Exception as exc:
        print(f"google-genai import: FAILED → {exc!r}")
        print("\nDIAGNOSIS: the SDK is missing in this venv. Fix:")
        print("  .venv/bin/pip install google-genai   # then restart the service")
        return 1

    # 2) Engine construction (this is where degraded mode is decided).
    from fango.consult.engine import GeminiEngine
    eng = GeminiEngine(model=model, api_key=key or None)
    print(f"engine._degraded: {eng._degraded}")
    if eng._degraded:
        print("\nDIAGNOSIS: engine is degraded at construction — almost always a")
        print("missing/empty GEMINI_API_KEY in THIS environment. If .env has it,")
        print("the running service wasn't restarted after you set it, or it reads")
        print("env a different way than this script. Restart with .env loaded.")
        return 1

    # 3) Live call — the real thing. Surfaces quota / region / auth errors.
    print("\nrunning a live extract_intent call …")
    try:
        r = eng.extract_intent([], "飯田橋 神楽坂で2LDK、家賃20万以内")
    except Exception as exc:  # the engine itself catches, but be safe
        print(f"live call raised: {exc!r}")
        return 1
    print(f"  state: {r.state}")
    print(f"  ask_back: {(r.ask_back or '')[:80]}")
    print(f"  criteria_delta: {getattr(r, 'criteria_delta', None)}")

    busy = (r.ask_back or "").startswith("システムが混雑")
    if busy or r.state == "asking" and not getattr(r, "criteria_delta", None):
        print("\nDIAGNOSIS: engine constructed fine but the API CALL failed — so this")
        print("is quota / region / key-permission / network. The real error was")
        print("logged by the engine as 'extract_intent LLM call failed: ...'.")
        print("Check the service log for that line; common cases:")
        print("  400 User location is not supported  → server region is Gemini-blocked")
        print("  429 RESOURCE_EXHAUSTED              → quota / rate limit")
        print("  403 PERMISSION_DENIED / API key …   → key invalid or API not enabled")
        return 1

    print("\nDIAGNOSIS: consult engine is HEALTHY here. If the live service still")
    print("returns 'busy', the running process differs from this script's env —")
    print("restart it so it picks up the same .env.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
