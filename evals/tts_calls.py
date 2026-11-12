"""Synthetic-voice calls through the real bridge (a small preview of layer 3).

    python -m evals.tts_calls [--cases ex2_missing_required_modifier,...] [--calls 2]

The caller lines of the chosen cases (evals/cases.yaml, synthetic) are spoken by
Windows text-to-speech (System.Speech), streamed at real-time pace into
CallBridge.run() against the real Live model, with the in-process backend. The
simulated caller waits until the agent's reply has finished playing (audio
duration, not arrival time) before saying the next line.

Measures the guessed-answer guard in audio mode, with ground truth:
- **false block**: a call refused although a caller line had started playing
  after the agent's question (the caller was answering);
- how fast each caller-speech signal (mic energy, server voice activity, input
  transcript) fired after a caller line started;
- audio cuts and the final order.

Windows only (System.Speech). Synthetic data only: safe for a free-tier key.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import subprocess
import sys
import tempfile
import time
import uuid
import wave
from pathlib import Path
from typing import Optional

import httpx
import yaml
from dotenv import load_dotenv

from app.main import app, order_service
from evals.run import CASES_PATH, REPO_ROOT, key_suffix, select_api_key
from live.bridge.call import LIVE_API_VERSION, CallBridge, make_live_client
from live.bridge.tools import HttpBackend, ToolRouter

IN_RATE = 16_000
CHUNK_MS = 20
CHUNK_BYTES = IN_RATE * 2 * CHUNK_MS // 1000
OUT_BYTES_PER_S = 24_000 * 2
LEAD_SILENCE_S = 0.6
REPLY_QUIET_S = 1.2       # agent reply counts as finished after this much quiet
REPLY_TIMEOUT_S = 25.0    # caller gives up waiting and says the next line anyway
CALL_TIMEOUT_S = 150.0


def synthesize(text: str, voice: str, cache: Path) -> bytes:
    """16 kHz mono 16-bit PCM of ``text`` from Windows TTS (cached per voice/text)."""
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / (hashlib.sha1(f"{voice}|{text}".encode()).hexdigest()[:16] + ".wav")
    if not path.exists():
        ps = (
            "Add-Type -AssemblyName System.Speech;"
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
            f"$s.SelectVoice('{voice}');"
            "$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000,"
            "[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,"
            "[System.Speech.AudioFormat.AudioChannel]::Mono);"
            f"$s.SetOutputToWaveFile('{path}', $f);"
            "$s.Speak([Console]::In.ReadToEnd()); $s.Dispose()"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], input=text, text=True,
                       check=True, capture_output=True)
    with wave.open(str(path), "rb") as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (IN_RATE, 1, 2)
        return w.readframes(w.getnframes())


class TtsCaller:
    """AudioPort: plays caller lines in turn, silence otherwise, in real time."""

    def __init__(self, lines: list[bytes], bridge: CallBridge, log: list) -> None:
        self.lines, self.bridge, self.log = lines, bridge, log
        self.loop = asyncio.get_event_loop()
        self.index = -1                  # line being spoken; -1 = lead-in
        self.offset = 0
        self.state = "lead"
        self.state_since = self.loop.time()
        self.played_until = 0.0          # when the agent's audio finishes playing
        self.agent_audio_since_line = False
        self.next_tick = self.loop.time()
        self.done = False

    def _now(self) -> float:
        return self.loop.time()

    async def read(self) -> Optional[bytes]:
        self.next_tick += CHUNK_MS / 1000
        await asyncio.sleep(max(0.0, self.next_tick - self._now()))
        now = self._now()
        if self.done:
            return None
        if self.state == "lead" and now - self.state_since >= LEAD_SILENCE_S:
            self._start_line(0)
        elif self.state == "waiting" and self._agent_finished(now):
            if self.index + 1 < len(self.lines):
                self._start_line(self.index + 1)
            else:
                self.log.append((now, "hangup", {}))
                self.done = True
                return None
        if self.state == "speaking":
            line = self.lines[self.index]
            chunk = line[self.offset:self.offset + CHUNK_BYTES]
            self.offset += CHUNK_BYTES
            if self.offset >= len(line):
                self.state, self.state_since = "waiting", now
                self.agent_audio_since_line = False
                self.log.append((now, "caller_line_end", {"line": self.index}))
            return chunk.ljust(CHUNK_BYTES, b"\x00")
        return b"\x00" * CHUNK_BYTES

    def _start_line(self, index: int) -> None:
        self.index, self.offset, self.state = index, 0, "speaking"
        self.log.append((self._now(), "caller_line_start", {"line": index}))

    def _agent_finished(self, now: float) -> bool:
        waited = now - self.state_since
        if waited >= REPLY_TIMEOUT_S:
            self.log.append((now, "caller_gave_up_waiting", {"line": self.index}))
            return True
        return (self.agent_audio_since_line and now >= self.played_until + REPLY_QUIET_S
                and self.bridge.tools_idle and not self.bridge.awaiting_reply)

    async def play(self, pcm: bytes) -> None:
        now = self._now()
        self.played_until = max(self.played_until, now) + len(pcm) / OUT_BYTES_PER_S
        if self.state == "waiting":
            self.agent_audio_since_line = True

    async def clear(self) -> None:
        self.played_until = self._now()


def analyze_call(log: list) -> dict:
    starts = [(t, f["line"]) for t, k, f in log if k == "caller_line_start"]
    questions = [t for t, k, _ in log if k == "question"]
    blocks = []
    for t, kind, f in log:
        if kind != "guess_blocked":
            continue
        q = max((x for x in questions if x <= t), default=None)
        answering = [line for ts, line in starts if q is not None and q <= ts <= t]
        blocks.append({"t": round(t, 2), "name": f["name"], "false": bool(answering),
                       "since_question_s": f.get("since_question_s"), "since_signal_s": f.get("since_signal_s")})
    latency: dict[str, list[float]] = {}
    for ts, _ in starts:
        for signal in ("energy", "vad", "transcript"):
            first = min((t for t, k, f in log if k == "caller_speech" and f["signal"] == signal and t >= ts),
                        default=None)
            if first is not None and first - ts < 10:
                latency.setdefault(signal, []).append(round(first - ts, 2))
    return {"blocks": blocks, "latency": latency,
            "cuts": sum(1 for _, k, _ in log if k == "audio_cut"),
            "gave_up": sum(1 for _, k, _ in log if k == "caller_gave_up_waiting")}


async def one_call(client, case: dict, voice: str, model: str, cache: Path) -> dict:
    lines_text = [s["say"] for s in case["steps"]]
    lines = [synthesize(text, voice, cache) for text in lines_text]
    session_id = f"tts-{case['id']}-{uuid.uuid4().hex[:8]}"
    http = HttpBackend("http://backend", transport=httpx.ASGITransport(app=app), timeout=10)
    bridge = CallBridge(ToolRouter(http, session_id))
    log: list = []
    loop = asyncio.get_event_loop()
    bridge.on_guard_event = lambda kind, **f: log.append((loop.time(), kind, f))
    caller = TtsCaller(lines, bridge, log)
    started = time.monotonic()
    error = None
    try:
        await asyncio.wait_for(bridge.run(client, model, caller), CALL_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - report, don't crash the batch
        error = f"{type(exc).__name__}: {exc}"
    finally:
        await http.aclose()
    order = order_service.get_order(session_id).model_dump()
    result = analyze_call(log)
    result.update({"case": case["id"], "voice": voice, "seconds": round(time.monotonic() - started, 1),
                   "error": error, "items": [(i["item_id"], i["quantity"], i["modifiers"]) for i in order["items"]],
                   "expected": case.get("final", {}).get("items"), "lines": lines_text})
    return result


async def main(args: argparse.Namespace) -> int:
    if sys.platform != "win32":
        print("Needs Windows text-to-speech (System.Speech).", file=sys.stderr)
        return 2
    selected = select_api_key()
    if not selected:
        print("Set GEMINI_EVAL_API_KEY or GEMINI_API_KEY.", file=sys.stderr)
        return 2
    print(f"API key: {selected[0]} ({key_suffix(selected[1])})  model={args.model}")
    client = make_live_client(args.api_version, api_key=selected[1])
    cases = {c["id"]: c for c in yaml.safe_load(CASES_PATH.read_text(encoding="utf-8"))["cases"]}
    cache = Path(tempfile.gettempdir()) / "voice-ordering-tts"
    totals = {"calls": 0, "blocks": 0, "false": 0, "cuts": 0}
    latencies: dict[str, list[float]] = {}
    for cid in args.cases.split(","):
        for n in range(args.calls):
            voice = ("Microsoft Zira Desktop", "Microsoft David Desktop")[n % 2]
            r = await one_call(client, cases[cid], voice, args.model, cache)
            totals["calls"] += 1
            totals["blocks"] += len(r["blocks"])
            totals["false"] += sum(b["false"] for b in r["blocks"])
            totals["cuts"] += r["cuts"]
            for k, v in r["latency"].items():
                latencies.setdefault(k, []).extend(v)
            print(f"\n{cid} call {n + 1} ({voice.split()[1]}, {r['seconds']}s){'  ERROR ' + r['error'] if r['error'] else ''}")
            print(f"   order: {r['items']}  expected: {r['expected']}")
            print(f"   guard blocks: {len(r['blocks'])} (false: {sum(b['false'] for b in r['blocks'])})  "
                  f"audio cuts: {r['cuts']}  caller gave up waiting: {r['gave_up']}")
            for b in r["blocks"]:
                print(f"     {'FALSE' if b['false'] else 'true '} block {b['name']}: {b['since_question_s']}s after "
                      f"the question; since caller signals: {b['since_signal_s']}")
            print(f"   caller-speech signal latency after line start: {r['latency']}")
    print(f"\nTOTAL calls={totals['calls']} guard blocks={totals['blocks']} false blocks={totals['false']} "
          f"audio cuts={totals['cuts']}")
    for k, v in sorted(latencies.items()):
        v.sort()
        print(f"   {k:10} fired after {len(v)} caller lines; median {v[len(v) // 2]}s, max {v[-1]}s")
    missing = {"energy", "vad", "transcript"} - set(latencies)
    if missing:
        print(f"   never fired: {', '.join(sorted(missing))}")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Synthetic-voice calls through the real bridge.")
    p.add_argument("--cases", default="ex2_missing_required_modifier,ex4_reference_resolution,"
                                       "ex6_full_order_confirm_submit,unclear_item_never_guess")
    p.add_argument("--calls", type=int, default=2, help="calls per case (alternating voices)")
    p.add_argument("--model", default="gemini-3.8-live")
    p.add_argument("--api-version", default=LIVE_API_VERSION)
    return p.parse_args()


if __name__ == "__main__":
    import logging

    logging.basicConfig(level=logging.WARNING, force=True)
    load_dotenv(REPO_ROOT / ".env")
    sys.exit(asyncio.run(main(parse_args())))
