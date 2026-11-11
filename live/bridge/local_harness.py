"""Local test harness: the laptop mic and speaker stand in for the phone line.

    python -m live.bridge.local_harness [--backend URL] [--model NAME]

Needs GEMINI_API_KEY (or GOOGLE_API_KEY), set in the environment or in a
.env file at the repo root, and the FastAPI backend running locally. Use headphones: with open speakers the model hears itself and
interrupts its own turns. Ctrl+C hangs up.

Mic: 16 kHz mono 16-bit PCM in. Speaker: 24 kHz mono 16-bit PCM out.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Optional

import httpx
import sounddevice as sd
from dotenv import load_dotenv

from live.bridge.call import (
    LIVE_API_VERSION,
    CallBridge,
    LiveHandshakeError,
    make_live_client,
    new_session_id,
)
from live.bridge.tools import HttpBackend, ToolRouter

REPO_ROOT = Path(__file__).resolve().parents[2]

IN_RATE = 16_000
OUT_RATE = 24_000
FRAME_MS = 20


class LaptopAudio:
    """AudioPort backed by the default input/output devices."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._mic_chunks: asyncio.Queue[Optional[bytes]] = asyncio.Queue()
        self._playback = bytearray()
        self._lock = threading.Lock()
        self._mic = sd.RawInputStream(
            samplerate=IN_RATE, channels=1, dtype="int16",
            blocksize=IN_RATE * FRAME_MS // 1000, callback=self._on_mic,
        )
        self._speaker = sd.RawOutputStream(
            samplerate=OUT_RATE, channels=1, dtype="int16",
            blocksize=OUT_RATE * FRAME_MS // 1000, callback=self._on_speaker,
        )

    # sounddevice callbacks run on PortAudio threads.
    def _on_mic(self, indata, frames, time, status) -> None:
        self._loop.call_soon_threadsafe(self._mic_chunks.put_nowait, bytes(indata))

    def _on_speaker(self, outdata, frames, time, status) -> None:
        n = len(outdata)
        with self._lock:
            chunk = bytes(self._playback[:n])
            del self._playback[:n]
        outdata[: len(chunk)] = chunk
        outdata[len(chunk):] = b"\x00" * (n - len(chunk))

    def start(self) -> None:
        self._mic.start()
        self._speaker.start()

    def close(self) -> None:
        for stream in (self._mic, self._speaker):
            stream.stop()
            stream.close()
        self._loop.call_soon_threadsafe(self._mic_chunks.put_nowait, None)

    async def read(self) -> Optional[bytes]:
        return await self._mic_chunks.get()

    async def play(self, pcm: bytes) -> None:
        with self._lock:
            self._playback.extend(pcm)

    async def clear(self) -> None:
        with self._lock:
            self._playback.clear()


logger = logging.getLogger("live_bridge.harness")


def _key_suffix(client) -> str:
    key = getattr(getattr(client, "_api_client", None), "api_key", None) or (
        os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY") or ""
    )
    return f"...{key[-4:]}" if key else "<none>"


async def main(args: argparse.Namespace) -> None:
    if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
        sys.exit("Set GEMINI_API_KEY (or GOOGLE_API_KEY) first.")
    client = make_live_client(args.api_version)
    logger.info(
        "model=%s api_version=%s key=%s", args.model, args.api_version, _key_suffix(client)
    )
    try:
        httpx.get(f"{args.backend}/health", timeout=3).raise_for_status()
    except httpx.HTTPError as exc:
        sys.exit(f"Backend not reachable at {args.backend} ({exc}). Start it first.")

    backend = HttpBackend(args.backend, timeout=args.timeout)
    bridge = CallBridge(ToolRouter(backend, new_session_id()))
    print(f"\nCall started. session_id = {bridge.session_id}")
    print(f"Inspect the order: curl {args.backend}/tools/order/{bridge.session_id}")
    print("Say hello to begin. Ctrl+C hangs up.\n")

    audio = LaptopAudio(asyncio.get_running_loop())
    audio.start()
    try:
        await bridge.run(
            client, args.model, audio,
            extra_config={"input_audio_transcription": {}, "output_audio_transcription": {}},
        )
    finally:
        audio.close()
        await backend.aclose()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--backend", default="http://127.0.0.1:8080", help="FastAPI backend base URL")
    p.add_argument("--model", default="gemini-3.8-live", help="Live API model name")
    p.add_argument("--timeout", type=float, default=5.0, help="backend timeout in seconds")
    p.add_argument(
        "--api-version", default=LIVE_API_VERSION,
        help=f"Live API version (default {LIVE_API_VERSION}; 'v1' has no Live endpoint)",
    )
    return p.parse_args()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # Optional; variables already set in the environment take precedence.
    load_dotenv(REPO_ROOT / ".env")
    try:
        asyncio.run(main(parse_args()))
    except KeyboardInterrupt:
        print("\nHung up.")
    except LiveHandshakeError as exc:
        sys.exit(str(exc))
