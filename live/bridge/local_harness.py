"""Local test harness: the laptop mic and speaker stand in for the phone line.

    python -m live.bridge.local_harness [--debug] [--input-device X] [--output-device Y]

Needs GEMINI_API_KEY (or GOOGLE_API_KEY), set in the environment or in a .env
file at the repo root, and the FastAPI backend running locally. Use
headphones: with open speakers the model hears itself and interrupts its own
turns. Ctrl+C hangs up.

Mic: 16 kHz mono 16-bit PCM in. Speaker: 24 kHz mono 16-bit PCM out.
Audio I/O uses sounddevice callback streams: PortAudio calls them on its own
threads, so nothing here blocks the asyncio event loop. The mic is never muted.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional, Union

import httpx
import sounddevice as sd
from dotenv import load_dotenv

from live.bridge.call import (
    LIVE_API_VERSION,
    CallBridge,
    LiveHandshakeError,
    PumpStats,
    make_live_client,
    new_session_id,
)
from live.bridge.tools import HttpBackend, ToolRouter

REPO_ROOT = Path(__file__).resolve().parents[2]

IN_RATE = 16_000
OUT_RATE = 24_000
FRAME_MS = 20

logger = logging.getLogger("live_bridge.harness")

Device = Union[int, str, None]


def parse_device(value: Optional[str]) -> Device:
    """Device index (e.g. '3') or a name substring (e.g. 'Headset')."""
    if value is None:
        return None
    return int(value) if value.strip().isdigit() else value


def device_name(device: Device, kind: str) -> str:
    info = sd.query_devices(device, kind=kind)
    hostapi = sd.query_hostapis(info["hostapi"])["name"]
    return f"[{info['index']}] {info['name']} ({hostapi})"


class LaptopAudio:
    """AudioPort backed by sounddevice callback streams."""

    def __init__(
        self, loop: asyncio.AbstractEventLoop, input_device: Device = None,
        output_device: Device = None,
    ) -> None:
        self._loop = loop
        self._mic_chunks: asyncio.Queue[Optional[bytes]] = asyncio.Queue()
        self._playback = bytearray()
        self._lock = threading.Lock()
        self.input_overflows = 0
        self.output_underflows = 0
        self._mic = sd.RawInputStream(
            samplerate=IN_RATE, channels=1, dtype="int16", device=input_device,
            blocksize=IN_RATE * FRAME_MS // 1000, callback=self._on_mic,
        )
        self._speaker = sd.RawOutputStream(
            samplerate=OUT_RATE, channels=1, dtype="int16", device=output_device,
            blocksize=OUT_RATE * FRAME_MS // 1000, callback=self._on_speaker,
        )

    # -- PortAudio threads (must not block) --------------------------------
    def _on_mic(self, indata, frames, time_info, status) -> None:
        if status.input_overflow:
            self.input_overflows += 1
        self._loop.call_soon_threadsafe(self._mic_chunks.put_nowait, bytes(indata))

    def _on_speaker(self, outdata, frames, time_info, status) -> None:
        if status.output_underflow:
            self.output_underflows += 1
        n = len(outdata)
        with self._lock:
            chunk = bytes(self._playback[:n])
            del self._playback[:n]
        outdata[: len(chunk)] = chunk
        outdata[len(chunk):] = b"\x00" * (n - len(chunk))

    # -- event loop side -----------------------------------------------------
    def start(self) -> None:
        self._mic.start()
        self._speaker.start()

    def close(self) -> None:
        for stream in (self._mic, self._speaker):
            try:
                stream.abort()  # stop now, don't drain queued playback
                stream.close()
            except sd.PortAudioError:
                pass
        self._loop.call_soon_threadsafe(self._mic_chunks.put_nowait, None)

    @property
    def mic_backlog(self) -> int:
        return self._mic_chunks.qsize()

    @property
    def buffered_ms(self) -> int:
        with self._lock:
            return len(self._playback) * 1000 // (OUT_RATE * 2)

    async def read(self) -> Optional[bytes]:
        return await self._mic_chunks.get()

    async def play(self, pcm: bytes) -> None:
        with self._lock:
            self._playback.extend(pcm)

    async def clear(self) -> None:
        with self._lock:
            self._playback.clear()


async def debug_reporter(stats: PumpStats, audio: LaptopAudio) -> None:
    """Once per second: mic, model audio, server message kinds and loop lag."""
    expected = time.perf_counter() + 1.0
    while True:
        await asyncio.sleep(max(0.0, expected - time.perf_counter()))
        lag_ms = (time.perf_counter() - expected) * 1000  # event-loop blocking shows here
        expected += 1.0
        s = stats.snapshot()
        kinds = " ".join(f"{k}={v}" for k, v in sorted(s["kinds"].items())) or "none"
        logger.info(
            "[debug] mic sent=%d peak=%d/32767 backlog=%d overflows=%d | "
            "model audio chunks=%d (%.2fs) buffered=%dms underflows=%d | "
            "msgs: %s | loop lag=%.0fms",
            s["mic_chunks"], s["mic_peak"], audio.mic_backlog, audio.input_overflows,
            s["model_chunks"], s["model_bytes"] / (OUT_RATE * 2), audio.buffered_ms,
            audio.output_underflows, kinds, lag_ms,
        )


def _key_suffix(client) -> str:
    key = getattr(getattr(client, "_api_client", None), "api_key", None) or (
        os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY") or ""
    )
    return f"...{key[-4:]}" if key else "<none>"


async def main(args: argparse.Namespace) -> None:
    if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
        sys.exit("Set GEMINI_API_KEY (or GOOGLE_API_KEY) first.")
    in_dev, out_dev = parse_device(args.input_device), parse_device(args.output_device)
    try:
        logger.info("input device:  %s", device_name(in_dev, "input"))
        logger.info("output device: %s", device_name(out_dev, "output"))
    except (ValueError, sd.PortAudioError) as exc:
        sys.exit(f"Audio device error: {exc}. Run with --list-devices.")

    client = make_live_client(args.api_version)
    logger.info(
        "model=%s api_version=%s key=%s", args.model, args.api_version, _key_suffix(client)
    )
    try:
        async with httpx.AsyncClient(timeout=3) as http:
            (await http.get(f"{args.backend}/health")).raise_for_status()
    except httpx.HTTPError as exc:
        sys.exit(f"Backend not reachable at {args.backend} ({exc}). Start it first.")

    backend = HttpBackend(args.backend, timeout=args.timeout)
    bridge = CallBridge(ToolRouter(backend, new_session_id()))
    print(f"\nCall started. session_id = {bridge.session_id}")
    print(f"Inspect the order: curl {args.backend}/tools/order/{bridge.session_id}")
    print("Say hello to begin. Ctrl+C hangs up.\n")

    audio = LaptopAudio(asyncio.get_running_loop(), in_dev, out_dev)
    reporter: Optional[asyncio.Task] = None
    if args.debug:
        bridge.stats = PumpStats()
        reporter = asyncio.create_task(debug_reporter(bridge.stats, audio), name="debug")
    audio.start()
    try:
        await bridge.run(
            client, args.model, audio,
            extra_config={"input_audio_transcription": {}, "output_audio_transcription": {}},
        )
    finally:
        # bridge.run has already cancelled and awaited its own tasks and closed
        # the Live session (also on Ctrl+C); stop the rest here.
        if reporter:
            reporter.cancel()
            await asyncio.gather(reporter, return_exceptions=True)
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
    p.add_argument("--input-device", help="mic: device index or name substring")
    p.add_argument("--output-device", help="speaker: device index or name substring")
    p.add_argument("--list-devices", action="store_true", help="list audio devices and exit")
    p.add_argument(
        "--debug", action="store_true",
        help="log mic/model audio, server message kinds and loop lag once per second",
    )
    return p.parse_args()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = parse_args()
    if args.list_devices:
        print(sd.query_devices())
        sys.exit(0)
    # Optional; variables already set in the environment take precedence.
    load_dotenv(REPO_ROOT / ".env")
    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        print("\nHung up.")
    except LiveHandshakeError as exc:
        sys.exit(str(exc))
