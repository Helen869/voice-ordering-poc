"""Layer-1 evals for the Live agent: caller turns as text, real Live model.

    python -m evals.run [--runs 3] [--threshold 0.8] [--cases ex1_normal_order,...]

Each run of a case gets a fresh Live session, its own session_id and the
in-process FastAPI backend. Function calls go through the real CallBridge and
ToolRouter. Deterministic checks (evals/checks.py) score every run; the
report gives pass rates per case and per check, and is saved as JSON under
evals/reports/. Exit code: 0 = every case at or above --threshold,
1 = some case below it, 2 = could not start (key, handshake, bad cases file).

Not part of the pytest suite: it calls the real API and costs money.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
import yaml
from dotenv import load_dotenv

from app.main import app, order_service
from evals.checks import evaluate, run_passed
from live.bridge.call import (
    DEFAULT_LANGUAGE,
    load_system_instruction,
    LIVE_API_VERSION,
    CallBridge,
    build_live_config,
    handshake_rejection,
    make_live_client,
)
from live.bridge.tools import BackendUnavailable, HttpBackend, ToolRouter

REPO_ROOT = Path(__file__).resolve().parent.parent
CASES_PATH = REPO_ROOT / "evals" / "cases.yaml"
REPORTS_DIR = REPO_ROOT / "evals" / "reports"
TRACKED_FILES = ("live/system-instruction.md", "live/function_declarations.json", "evals/cases.yaml")

logger = logging.getLogger("evals")


# ---------------------------------------------------------------------------
# Test doubles around the real bridge pieces
# ---------------------------------------------------------------------------
class SwitchableBackend:
    """Real in-process backend that a case can switch "down" between turns."""

    def __init__(self, inner: HttpBackend) -> None:
        self.inner = inner
        self.down = False

    async def request(self, method, path, *, params=None, json=None):
        if self.down:
            raise BackendUnavailable("eval: backend switched down")
        return await self.inner.request(method, path, params=params, json=json)


class EventLog:
    """Ordered record of everything in a run (seq = order of occurrence)."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self._t0 = time.monotonic()

    def add(self, type_: str, step: int, **fields: Any) -> None:
        self.events.append({"seq": len(self.events), "t": round(time.monotonic() - self._t0, 3),
                            "type": type_, "step": step, **fields})


class LoggingSession:
    """Live session proxy given to the bridge only: logs its tool responses and
    System notes (caller turns are sent on the raw session and logged as "caller")."""

    def __init__(self, session: Any, log: EventLog, state: "TurnState") -> None:
        self._session, self._log, self._state = session, log, state

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    async def send_tool_response(self, *, function_responses):
        self._log.add("tool_response_sent", self._state.step,
                      ids=[r["id"] for r in function_responses], names=[r["name"] for r in function_responses])
        await self._session.send_tool_response(function_responses=function_responses)

    async def send_realtime_input(self, **kwargs):
        if kwargs.get("text") is not None and "audio" not in kwargs:
            self._log.add("system_note_sent", self._state.step, text=kwargs["text"])
        await self._session.send_realtime_input(**kwargs)


class RecordingRouter:
    """Wraps ToolRouter; records every call with the caller turn it happened in."""

    def __init__(self, router: ToolRouter, log: Optional[EventLog] = None) -> None:
        self._router = router
        self._log = log
        self.step = 0
        self.calls: list[dict] = []

    @property
    def session_id(self) -> str:
        return self._router.session_id

    async def call(self, name: str, args: Optional[dict]) -> dict:
        result = await self._router.call(name, args)
        self.calls.append({"step": self.step, "name": name, "args": dict(args or {}), "result": result})
        if self._log:
            self._log.add("tool_result", self.step, name=name, args=dict(args or {}), result=result)
        return result


# ---------------------------------------------------------------------------
# One run of one case
# ---------------------------------------------------------------------------
class TurnState:
    def __init__(self, n_steps: int) -> None:
        self.step = 0
        self.agent = [""] * n_steps


async def _read_loop(session: Any, bridge: CallBridge, state: TurnState, events: asyncio.Queue,
                     log: EventLog) -> None:
    try:
        while True:  # receive() stops after each completed turn
            async for msg in session.receive():
                sc = msg.server_content
                if sc and sc.output_transcription and sc.output_transcription.text:
                    state.agent[state.step] += sc.output_transcription.text
                    log.add("agent_text", state.step, text=sc.output_transcription.text)
                if msg.tool_call:
                    fcs = msg.tool_call.function_calls or []
                    log.add("tool_call", state.step, ids=[f.id for f in fcs], names=[f.name for f in fcs])
                if msg.tool_call_cancellation:
                    log.add("tool_call_cancellation", state.step, ids=list(msg.tool_call_cancellation.ids or []))
                if sc and (sc.turn_complete or sc.interrupted):
                    log.add("turn_complete" if sc.turn_complete else "interrupted", state.step)
                if getattr(msg, "usage_metadata", None):  # token usage, for cost estimates
                    log.add("usage", state.step,
                            usage=msg.usage_metadata.model_dump(exclude_none=True, mode="json"))
                # Same order as the live pump: the bridge sees this message's
                # transcript before its audio would be played, so an audio cut
                # (leak marker) also stops the audio in this message.
                muted = bridge.audio_muted
                await bridge.on_message(msg)
                muted = muted or bridge.audio_muted
                if sc and sc.model_turn:
                    for part in sc.model_turn.parts or []:
                        if part.inline_data and part.inline_data.data:  # 24 kHz 16-bit PCM
                            log.add("audio", state.step, bytes=len(part.inline_data.data),
                                    forwarded=not muted)
                        if part.text:  # not speech: logged apart, never scored as speech
                            log.add("model_text", state.step, text=part.text, thought=bool(part.thought))
                if sc and sc.turn_complete:
                    events.put_nowait("turn_complete")
                elif msg.tool_call or (sc and sc.model_turn):
                    events.put_nowait("activity")
    except Exception as exc:  # surfaced to the step loop
        events.put_nowait(exc)


async def _next_event(events: asyncio.Queue, timeout: float) -> str:
    event = await asyncio.wait_for(events.get(), timeout)
    if isinstance(event, Exception):
        raise event
    return event


async def _wait_for_activity(events: asyncio.Queue, timeout: float) -> None:
    """Until the agent starts answering (audio or a function call)."""
    while await _next_event(events, timeout) not in ("activity", "turn_complete"):
        pass


async def _wait_turn_done(events: asyncio.Queue, bridge: CallBridge, timeout: float, settle: float) -> None:
    """Until a turn completes with no function call pending, the model has
    answered every tool response sent so far, and nothing follows within
    ``settle`` seconds (late turns, e.g. after a System note, are kept).

    A turn_complete can arrive together with a tool_call, before the model
    has spoken its reply to the tool response; that turn is not done."""
    deadline = time.monotonic() + timeout
    completed = False  # last event was turn_complete
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError
        try:
            event = await _next_event(events, min(settle, remaining) if completed else remaining)
        except asyncio.TimeoutError:
            # events.empty(): the reader updates the bridge before queueing the
            # event, so "answered" can be visible while its event is still queued.
            if (completed and events.empty() and bridge.tools_idle
                    and not getattr(bridge, "awaiting_reply", False)):
                return  # quiet after a completed turn: done
            continue  # still waiting: a call is running, or its response is unanswered
        completed = event == "turn_complete"


OUTPUT_BYTES_PER_SECOND = 24_000 * 2  # Live audio output: 24 kHz, 16-bit mono PCM


def step_audio_stats(events: list[dict], step: int) -> dict:
    """Spoken audio, latency and non-speech text parts for one caller turn."""
    said = next((e["t"] for e in events if e["type"] == "caller" and e["step"] == step), None)
    audio = [e for e in events if e["type"] == "audio" and e["step"] == step]
    parts = [e for e in events if e["type"] == "model_text" and e["step"] == step]
    return {
        "audio_seconds": round(sum(e["bytes"] for e in audio) / OUTPUT_BYTES_PER_SECOND, 2),
        "first_audio_s": round(audio[0]["t"] - said, 2) if audio and said is not None else None,
        "model_text_parts": len(parts),
        "thought_parts": sum(1 for e in parts if e["thought"]),
    }


MAX_REASK_REPLIES = 2
_ASKS_TO_PLACE = re.compile(r"\bplace\b[^?.!]*\border\b[^?.!]*\?", re.IGNORECASE)


def asks_to_place_order(agent_text: str) -> bool:
    """'Would you like me to place the order?' and similar."""
    return bool(_ASKS_TO_PLACE.search(agent_text))


async def run_case(client: Any, case: dict, args: argparse.Namespace) -> dict:
    steps = [dict(s) for s in case["steps"]]  # may grow with re-ask replies
    session_id = f"eval-{case['id']}-{uuid.uuid4().hex[:8]}"
    http = HttpBackend("http://backend", transport=httpx.ASGITransport(app=app), timeout=10)
    backend = SwitchableBackend(http)
    log = EventLog()
    recorder = RecordingRouter(ToolRouter(backend, session_id), log)
    bridge = CallBridge(recorder, language=args.language or None)
    bridge.on_guard_event = lambda kind, **fields: log.add(kind, state.step, **fields)
    state = TurnState(len(steps))
    events: asyncio.Queue = asyncio.Queue()
    config = build_live_config(None, {"output_audio_transcription": {}}, args.language or None)
    if args.system_instruction:
        config["system_instruction"] = load_system_instruction(Path(args.system_instruction))
    config.update(args.live_config_dict)  # experiment arms, e.g. thinking_config
    error: Optional[str] = None
    cancelled = False
    started = time.monotonic()

    def begin(index: int) -> None:
        spec = steps[index]
        if spec.get("backend") in ("down", "up"):
            backend.down = spec["backend"] == "down"
        state.step = recorder.step = index
        log.add("caller", index, text=spec["say"])
        bridge.caller_input()  # text mode: the bridge has no input transcript

    try:
        async with asyncio.timeout(args.session_timeout):
            async with client.aio.live.connect(model=args.model, config=config) as session:
                bridge.attach(LoggingSession(session, log, state))  # bridge sends only
                worker = asyncio.create_task(bridge.run_tools())
                reader = asyncio.create_task(_read_loop(session, bridge, state, events, log))
                try:
                    i = 0
                    while i < len(steps):
                        begin(i)
                        await session.send_realtime_input(text=steps[i]["say"])
                        if i + 1 < len(steps) and steps[i + 1].get("barge_in"):
                            await _wait_for_activity(events, args.step_timeout)
                            i += 1
                            begin(i)
                            await session.send_realtime_input(text=steps[i]["say"])
                        try:
                            await _wait_turn_done(events, bridge, args.step_timeout, args.settle)
                        except asyncio.TimeoutError:
                            raise RuntimeError(f"timeout waiting for the agent at step {i + 1}") from None
                        i += 1
                    # Conditional caller reply: the agent asks again whether to
                    # place the order (e.g. after the bridge refused a submit).
                    reply = case.get("reask_reply")
                    replies = 0
                    while (reply and replies < MAX_REASK_REPLIES
                           and asks_to_place_order(state.agent[-1])
                           and not any(c["name"] == "submit_order" and "error" not in c["result"]
                                       for c in recorder.calls)):
                        replies += 1
                        steps.append({"say": reply, "explicit_yes": True, "auto_reply": True})
                        state.agent.append("")
                        begin(len(steps) - 1)
                        await session.send_realtime_input(text=reply)
                        try:
                            await _wait_turn_done(events, bridge, args.step_timeout, args.settle)
                        except asyncio.TimeoutError:
                            raise RuntimeError(f"timeout waiting for the agent at re-ask reply {replies}") from None
                finally:
                    cancelled = bool(getattr(bridge, "_cancelled", None))
                    for task in (reader, worker):
                        task.cancel()
                    await asyncio.gather(reader, worker, return_exceptions=True)
    except TimeoutError:
        error = f"session timeout after {args.session_timeout:.0f}s"
    except Exception as exc:
        rejected = handshake_rejection(exc)
        error = (f"handshake HTTP {rejected[0]}: {rejected[1]}" if rejected
                 else f"{type(exc).__name__}: {exc}")
    finally:
        await http.aclose()
    elapsed = time.monotonic() - started
    if error and elapsed > args.session_timeout * SLEEP_FACTOR:
        # Far beyond the timeout: the event loop was not running (laptop asleep).
        error = (f"{SLEEP_PREFIX} run took {elapsed:.0f}s, session timeout is "
                 f"{args.session_timeout:.0f}s (was: {error})")

    run = {
        "session_id": session_id,
        "steps": [
            {"say": spec["say"], "explicit_yes": bool(spec.get("explicit_yes")),
             "auto_reply": bool(spec.get("auto_reply")),
             "agent": state.agent[i],
             **step_audio_stats(log.events, i),
             "calls": [{k: c[k] for k in ("name", "args", "result")} for c in recorder.calls if c["step"] == i]}
            for i, spec in enumerate(steps)
        ],
        "final_order": order_service.get_order(session_id).model_dump(),
        "tool_call_cancellation_seen": cancelled,
        "audio_cuts": bridge.audio_cuts,
        "guesses_blocked": bridge.guesses_blocked,
        "events": log.events,
        "seconds": round(time.monotonic() - started, 1),
        "error": error,
    }
    run["checks"] = evaluate(case, run)
    run["passed"] = run_passed(run["checks"])
    return run


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def _git(*cmd: str) -> str:
    try:
        return subprocess.run(["git", *cmd], cwd=REPO_ROOT, capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def git_info() -> dict:
    files = {}
    for rel in TRACKED_FILES:
        dirty = subprocess.run(["git", "diff", "--quiet", "HEAD", "--", rel], cwd=REPO_ROOT).returncode != 0
        files[rel] = {
            "blob": _git("hash-object", rel),            # hash of the file as evaluated
            "last_commit": _git("log", "-1", "--format=%h", "--", rel),
            "uncommitted_changes": dirty,
        }
    return {"head": _git("rev-parse", "--short", "HEAD"), "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
            "files": files}


def summarize(cases: list[dict], results: dict[str, list[dict]]) -> tuple[dict, dict]:
    per_case = {}
    per_check: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for case in cases:
        runs = results[case["id"]]
        if not runs:  # every run hit an infrastructure error, or never started
            continue
        case_checks: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for run in runs:
            for name, res in run["checks"].items():
                buckets = [per_check[name]]
                if res.get("counted", True):
                    buckets.append(case_checks[name])
                for bucket in buckets:
                    bucket[0] += res["passed"]
                    bucket[1] += 1
        lat = sorted(s["first_audio_s"] for r in runs for s in r["steps"]
                     if s.get("first_audio_s") is not None)
        per_case[case["id"]] = {
            "first_audio_median_s": lat[len(lat) // 2] if lat else None,
            "first_audio_p90_s": lat[min(len(lat) - 1, int(len(lat) * 0.9))] if lat else None,
            "pass_rate": sum(r["passed"] for r in runs) / len(runs),
            "passed_runs": sum(r["passed"] for r in runs),
            "runs": len(runs),
            "checks": {k: f"{v[0]}/{v[1]}" for k, v in case_checks.items()},
        }
    return per_case, {k: {"passed": v[0], "applicable": v[1], "rate": v[0] / v[1]} for k, v in per_check.items()}


def print_report(meta: dict, per_case: dict, per_check: dict, results: dict, threshold: float) -> None:
    prompt = meta["git"]["files"]["live/system-instruction.md"]
    print(f"\nLive agent evals  model={meta['model']}  api={meta['api_version']}  "
          f"runs/case={meta['runs']}  threshold={threshold:.0%}")
    print(f"prompt live/system-instruction.md  blob={prompt['blob'][:12]}  "
          f"last_commit={prompt['last_commit']}{'  (UNCOMMITTED CHANGES)' if prompt['uncommitted_changes'] else ''}"
          f"  HEAD={meta['git']['head']}\n")
    width = max((len(c) for c in per_case), default=4)
    print(f"{'case':<{width}}  pass   rate   failing checks")
    for cid, s in per_case.items():
        failing = [f"{k} {v}" for k, v in s["checks"].items() if v.split("/")[0] != v.split("/")[1]]
        flag = "" if s["pass_rate"] >= threshold else "  <-- below threshold"
        print(f"{cid:<{width}}  {s['passed_runs']}/{s['runs']}  {s['pass_rate']:>5.0%}   "
              f"{', '.join(failing) or '-'}{flag}")
    timed = {cid: s for cid, s in per_case.items() if s.get("first_audio_median_s") is not None}
    if timed:
        print(f"\n{'latency: caller turn -> first audio':<{width}}  median   p90")
        for cid, s in timed.items():
            print(f"{cid:<{width}}  {s['first_audio_median_s']:5.2f}s  {s['first_audio_p90_s']:5.2f}s")
    print(f"\n{'check (all runs, counted or not)':<32} pass rate")
    for name, s in per_check.items():
        print(f"{name:<32} {s['passed']}/{s['applicable']}  {s['rate']:.0%}")
    failures = [(cid, n, run) for cid, runs in results.items() for n, run in enumerate(runs, 1) if not run["passed"]]
    if failures:
        print("\nfailures:")
        for cid, n, run in failures:
            bad = "; ".join(f"{k}: {v['detail']}" for k, v in run["checks"].items()
                            if not v["passed"] and v.get("counted", True))
            print(f"  {cid} run {n}: {bad}")
    info = [(cid, n, k, v["detail"]) for cid, runs in results.items() for n, run in enumerate(runs, 1)
            for k, v in run["checks"].items() if not v["passed"] and not v.get("counted", True)]
    warnings = [(cid, n, k, v["detail"]) for cid, runs in results.items() for n, run in enumerate(runs, 1)
                for k, v in run["checks"].items() if v.get("warning")]
    if warnings:
        print(f"\nwarnings ({len(warnings)}, not failures):")
        for cid, n, k, detail in warnings:
            print(f"  {cid} run {n}: {k}: {detail}")
    if info:
        print("\nnot counted for these cases (only_checks), for information:")
        for cid, n, k, detail in info:
            print(f"  {cid} run {n}: {k}: {detail}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def load_cases(path: Path, only: Optional[str]) -> list[dict]:
    cases = yaml.safe_load(path.read_text(encoding="utf-8"))["cases"]
    if only:
        wanted = set(only.split(","))
        unknown = wanted - {c["id"] for c in cases}
        if unknown:
            raise ValueError(f"unknown case id(s): {', '.join(sorted(unknown))}")
        cases = [c for c in cases if c["id"] in wanted]
    return cases


# Rate-limit / quota / billing failures are infrastructure errors, not agent
# failures: such runs are not scored, and repeated ones stop the whole run.
_QUOTA_ERROR = re.compile(
    r"\b429\b|RESOURCE_EXHAUSTED|quota|rate.?limit|credits are depleted|billing", re.IGNORECASE)
MAX_CONSECUTIVE_QUOTA_ERRORS = 2


SLEEP_PREFIX = "suspected machine sleep:"
SLEEP_FACTOR = 1.5  # a run this many times longer than the session timeout


def is_sleep_error(error: Optional[str]) -> bool:
    return bool(error) and error.startswith(SLEEP_PREFIX)


def is_quota_error(error: Optional[str]) -> bool:
    return bool(error) and bool(_QUOTA_ERROR.search(error))


# Evals use their own key when set (e.g. a separate free-tier key for synthetic data).
EVAL_KEY_VARS = ("GEMINI_EVAL_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")


def select_api_key(env: Optional[dict] = None) -> Optional[tuple[str, str]]:
    """(variable name, key) of the first non-empty key in EVAL_KEY_VARS."""
    env = os.environ if env is None else env
    for name in EVAL_KEY_VARS:
        if (env.get(name) or "").strip():
            return name, env[name].strip()
    return None


def key_suffix(key: str) -> str:
    return f"...{key[-4:]}" if len(key) >= 8 else "...(short key)"


async def preflight(client: Any, model: str) -> None:
    async with client.aio.live.connect(model=model, config={"response_modalities": ["AUDIO"]}):
        pass


async def main(args: argparse.Namespace) -> int:
    try:
        cases = load_cases(Path(args.cases_file), args.cases)
    except (OSError, ValueError, KeyError, yaml.YAMLError) as exc:
        print(f"Cannot load cases: {exc}", file=sys.stderr)
        return 2
    selected = select_api_key()
    if selected is None:
        print("Set GEMINI_EVAL_API_KEY or GEMINI_API_KEY, e.g. in .env.", file=sys.stderr)
        return 2
    key_var, api_key = selected
    print(f"API key: {key_var} ({key_suffix(api_key)})")
    client = make_live_client(args.api_version, api_key=api_key)
    try:
        await preflight(client, args.model)
    except Exception as exc:
        rejected = handshake_rejection(exc)
        detail = f"HTTP {rejected[0]}, body: {rejected[1]}" if rejected else f"{type(exc).__name__}: {exc}"
        print(f"Cannot open a Live session ({detail}).", file=sys.stderr)
        return 2

    from google import genai

    meta = {
        "started_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": args.model, "api_version": args.api_version, "language": args.language or "auto",
        "api_key_var": key_var, "api_key_suffix": key_suffix(api_key), "concurrency": args.concurrency,
        "system_instruction": args.system_instruction or "live/system-instruction.md",
        "system_instruction_blob": _git("hash-object", args.system_instruction) if args.system_instruction else None,
        "live_config": args.live_config_dict,
        "google_genai": genai.__version__, "runs": args.runs, "threshold": args.threshold,
        "git": git_info(),
    }
    sem = asyncio.Semaphore(args.concurrency)
    results: dict[str, list[dict]] = {c["id"]: [] for c in cases}
    infra: list[dict] = []  # runs lost to rate-limit / quota errors (not scored)
    stop = {"streak": 0, "reason": None, "not_started": 0}

    async def one(case: dict, n: int) -> None:
        async with sem:
            if stop["reason"]:
                stop["not_started"] += 1
                return
            run = await run_case(client, case, args)
            if is_sleep_error(run["error"]):
                infra.append({"case": case["id"], "run": n, "error": run["error"]})
                print(f"  SLEEP {case['id']} run {n}/{args.runs}: {run['error'][:150]}", flush=True)
                return
            if is_quota_error(run["error"]):
                stop["streak"] += 1
                infra.append({"case": case["id"], "run": n, "error": run["error"]})
                print(f"  QUOTA {case['id']} run {n}/{args.runs}: {run['error'][:150]}", flush=True)
                if stop["streak"] >= MAX_CONSECUTIVE_QUOTA_ERRORS:
                    stop["reason"] = run["error"]
                return
            stop["streak"] = 0
            results[case["id"]].append(run)
            status = "PASS" if run["passed"] else "FAIL"
            print(f"  {status}  {case['id']} run {n}/{args.runs}  ({run['seconds']}s)", flush=True)

    print(f"Running {len(cases)} case(s) x {args.runs} with concurrency {args.concurrency}...")
    await asyncio.gather(*(one(c, n) for c in cases for n in range(1, args.runs + 1)))

    per_case, per_check = summarize(cases, results)
    print_report(meta, per_case, per_check, results, args.threshold)
    if infra:
        print(f"\nnot scored, infrastructure errors (quota / machine sleep) ({len(infra)}):")
        for item in infra:
            print(f"  {item['case']} run {item['run']}: {item['error'][:150]}")

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORTS_DIR / f"report-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps({"meta": meta, "cases": per_case, "checks": per_check, "runs": results,
                               "quota_errors": infra, "stopped": stop},
                              indent=2, default=str), encoding="utf-8")
    shown = out.relative_to(REPO_ROOT) if out.is_relative_to(REPO_ROOT) else out
    print(f"\nreport: {shown}")
    if stop["reason"]:
        print(f"STOPPED: {MAX_CONSECUTIVE_QUOTA_ERRORS} rate-limit/quota errors in a row; "
              f"{stop['not_started']} run(s) not started. Last error: {stop['reason'][:200]}")
        return 2
    below = [cid for cid, s in per_case.items() if s["pass_rate"] < args.threshold]
    if below:
        print(f"FAILED: {len(below)} case(s) below {args.threshold:.0%}: {', '.join(below)}")
        return 1
    print(f"OK: every case at or above {args.threshold:.0%}")
    return 0


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Layer-1 Live agent evals (text input, real model).")
    p.add_argument("--runs", type=int, default=3, help="runs per case (default 3)")
    p.add_argument("--threshold", type=float, default=0.8,
                   help="minimum pass rate per case, 0-1 (default 0.8; with 3 runs = 3/3)")
    p.add_argument("--cases", help="comma-separated case ids (default: all)")
    p.add_argument("--cases-file", default=str(CASES_PATH))
    p.add_argument("--concurrency", type=int, default=3,
                   help="Live sessions at once (default 3; use 1 on a free-tier key)")
    p.add_argument("--model", default="gemini-3.8-live")
    p.add_argument("--api-version", default=LIVE_API_VERSION)
    p.add_argument("--language", default=DEFAULT_LANGUAGE)
    p.add_argument("--step-timeout", type=float, default=60.0, help="seconds per caller turn")
    p.add_argument("--session-timeout", type=float, default=300.0,
                   help="seconds per case run (whole Live session) before it fails (default 300)")
    p.add_argument("--settle", type=float, default=2.0,
                   help="quiet seconds after a turn before the next caller turn")
    p.add_argument("--verbose", action="store_true", help="show bridge logs (tool calls etc.)")
    p.add_argument("--system-instruction", metavar="PATH",
                   help="use this prompt file instead of live/system-instruction.md (experiments)")
    p.add_argument("--live-config", metavar="JSON", default="{}",
                   help='extra Live config merged in, e.g. \'{"thinking_config": {"thinking_budget": 0}}\'')
    args = p.parse_args(argv)
    try:
        args.live_config_dict = json.loads(args.live_config)
        assert isinstance(args.live_config_dict, dict)
    except (ValueError, AssertionError):
        p.error("--live-config must be a JSON object")
    if args.runs < 1 or args.concurrency < 1 or not 0 <= args.threshold <= 1:
        p.error("--runs and --concurrency must be >= 1, --threshold between 0 and 1")
    return args


if __name__ == "__main__":
    cli = parse_args()
    # force=True: importing app.main already configured the root logger.
    logging.basicConfig(level=logging.INFO if cli.verbose else logging.WARNING,
                        format="%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S", force=True)
    for noisy in ("httpx", "voice_ordering"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    load_dotenv(REPO_ROOT / ".env")
    sys.exit(asyncio.run(main(cli)))
