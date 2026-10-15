"""Provision a Twilio number and point it at this server.

1. Buys a US local number capable of Voice ($1.15/month).
2. Sets its inbound voice webhook to https://PUBLIC_HOST/twiml (POST+GET).
3. Prints the number + the TwiML it will serve.

Requires: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN in .env.
Usage:  python scripts/provision_twilio_number.py [--area-or-code 312] [--host calls.example.com]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
AUTH = (SID, TOKEN)
BASE = "https://api.twilio.com/2010-04-01/Accounts"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--area-or-code", default="312", help="preferred area code")
    p.add_argument("--host", required=True, help="public HTTPS host, e.g. calls.example.com")
    args = p.parse_args()
    if not SID or not TOKEN:
        sys.exit("Set TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN in .env first.")

    with httpx.Client(auth=AUTH, timeout=30) as c:
        # 1. search + buy
        r = c.get(f"{BASE}/{SID}/AvailablePhoneNumbers/US/Local.json",
                  params={"AreaCode": args.area_or_code, "VoiceEnabled": "true", "Limit": 1})
        numbers = r.json().get("available_phone_numbers", [])
        if not numbers:
            sys.exit(f"No numbers found for area code {args.area_or_code}.")
        target = numbers[0]["phone_number"]
        print(f"buying {target} ...")
        r = c.post(f"{BASE}/{SID}/IncomingPhoneNumbers.json",
                   data={"PhoneNumber": target, "FriendlyName": "Dialux SDR - Linda"})
        r.raise_for_status()
        sid = r.json()["sid"]
        print(f"purchased (sid={sid})")

        # 2. wire the voice webhook
        voice_url = f"https://{args.host}/twiml"
        r = c.post(f"{BASE}/{SID}/IncomingPhoneNumbers/{sid}.json",
                   data={"VoiceUrl": voice_url, "VoiceMethod": "POST"})
        r.raise_for_status()
        print(f"voice webhook -> {voice_url}")
        print("\nDONE. Call the number; Linda answers (begin_message) and the full")
        print("9-state machine runs on your server. Remove the number anytime in the")
        print("Twilio console (Active Numbers) to stop the $1.15/mo charge.")


if __name__ == "__main__":
    main()
