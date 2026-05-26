#!/usr/bin/env python3
"""Issue a fresh agent key from the command line.

The plaintext is printed to stdout; only the SHA-256 hash is stored.

    python -m scripts.issue_agent_key MyAgent --vendor anthropic
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fango.auth import create_agent
from fango.db import bootstrap


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Issue a IO.Fango agent key.")
    parser.add_argument("name", help="Unique agent display name")
    parser.add_argument("--vendor", default=None, help="Vendor tag (anthropic/openai/...)")
    args = parser.parse_args(argv)

    bootstrap()
    try:
        agent, key = create_agent(name=args.name, vendor=args.vendor)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"agent: {agent.name}  id={agent.id}  vendor={agent.vendor}")
    print("key (save now — shown once):")
    print(key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
