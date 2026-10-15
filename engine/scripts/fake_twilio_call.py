"""Local end-to-end tester: pretends to be Twilio Media Streams against /media.

Streams a WAV file (any rate — converted in-script to mulaw/8000 frames),
plays back the agent's audio into out.wav, and prints turn timing reported by
the server (via Langfuse it would be traced; here you watch the console).

Usage:
    python scripts/fake_twilio_call.py --url ws://localhost:8000/media \
        --input myvoice.wav --output out.wav
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import wave
from pathlib import Path


def load_wav_as_mulaw_frames(path: Path, frame_bytes: int = 320):
    import audioop  # deprecated in 3.13; fine on 3.12 (see requirements note)

    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        channels = w.getnchannels()
        width = w.getsampwidth()
        pcm = w.readframes(w.getnframes())
    if channels > 1:
        pcm = audioop.tomono(pcm, width, 0.5, 0.5)
    if width != 2:
        pcm = audioop.lin2lin(pcm, width, 2)
    if rate != 8000:
        pcm, _ = audioop.ratecv(pcm, 2, 1, rate, 8000, None)
    ulaw = audioop.lin2ulaw(pcm, 2)
    return [ulaw[i: i + frame_bytes] for i in range(0, len(ulaw), frame_bytes)]


async def run(url: str, wav_in: Path, wav_out: Path):
    import websockets

    frames = load_wav_as_mulaw_frames(wav_in)
    print(f"streaming {len(frames)} mulaw frames ({len(frames) * 20}ms) from {wav_in}")
    out_pcm = bytearray()
    async with websockets.connect(url, max_size=None) as ws:
        await ws.send(json.dumps({"event": "connected", "protocol": "media"}))
        await ws.send(json.dumps({
            "event": "start",
            "start": {
                "streamSid": "MZfake0000000000000000000000001",
                "callSid": "CAfake0000000000000000000000000001",
                "tracks": ["inbound"],
                "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
                "customParameters": {"first_name": "Test"},
            },
        }))
        # stream input while concurrently receiving the reply
        async def stream_in():
            for f in frames:
                await ws.send(json.dumps({
                    "event": "media",
                    "media": {"payload": base64.b64encode(f).decode()},
                    "streamSid": "MZfake0000000000000000000000001",
                }))
                await asyncio.sleep(0.02)
            # trailing silence so endpointing fires
            silence = b"\xff" * 320
            for _ in range(25):
                await ws.send(json.dumps({
                    "event": "media",
                    "media": {"payload": base64.b64encode(silence).decode()},
                    "streamSid": "MZfake0000000000000000000000001",
                }))
                await asyncio.sleep(0.02)

        async def recv():
            import audioop
            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    ev = msg.get("event")
                    if ev == "media":
                        pcm = audioop.ulaw2lin(base64.b64decode(msg["media"]["payload"]), 2)
                        out_pcm.extend(pcm)
                    elif ev == "clear":
                        out_pcm.clear()
                    elif ev == "mark":
                        print("[mark]", msg.get("mark", {}).get("name"))
            except Exception:
                pass

        in_task = asyncio.create_task(stream_in())
        recv_task = asyncio.create_task(recv())
        await in_task
        await asyncio.sleep(6)          # let the agent finish its reply
        recv_task.cancel()
        await ws.send(json.dumps({"event": "stop", "streamSid": "MZfake0000000000000000000000001"}))

    if out_pcm:
        with wave.open(str(wav_out), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(bytes(out_pcm))
        print(f"agent audio written to {wav_out} ({len(out_pcm) / 16000:.1f}s)")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="ws://localhost:8000/media")
    p.add_argument("--input", required=True)
    p.add_argument("--output", default="out.wav")
    a = p.parse_args()
    asyncio.run(run(a.url, Path(a.input), Path(a.output)))
