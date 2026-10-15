"""List Cartesia voices to pick Linda's voice ID (set CARTESIA_VOICE_ID in .env).

Usage: python scripts/list_cartesia_voices.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

KEY = os.environ.get("CARTESIA_API_KEY", "")


def main():
    if not KEY:
        sys.exit("Set CARTESIA_API_KEY in .env first.")
    with httpx.Client(timeout=30) as c:
        r = c.get("https://api.cartesia.ai/voices", headers={"X-API-Key": KEY})
        r.raise_for_status()
        for v in r.json():
            print(f"{v.get('id')}  {v.get('name','')!r:28} lang={v.get('language','?')} "
                  f"gender={v.get('gender','?')}")


if __name__ == "__main__":
    main()
