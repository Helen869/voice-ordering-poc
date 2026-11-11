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
                await bridge.on_message(msg)
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
    """Until a turn completes with no function call pending and nothing follows
    within ``settle`` seconds (late turns, e.g. after a System note, are kept)."""
    deadline = time.monotonic() + timeout
    completed = False  # last event was turn_complete
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError
        try:
            event = await _next_event(events, min(settle, remaining) if completed else remaining)
        except asyncio.TimeoutError:
            if completed and bridge.tools_idle:
                return  # quiet after a completed turn: done
            continue  # still waiting (a function call is running, or overall deadline)
        completed = event == "turn_complete"


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
    state = TurnState(len(steps))
    events: asyncio.Queue = asyncio.Queue()
    config = build_live_config(None, {"output_audio_transcription": {}}, args.language or None)
    error: Optional[str] = None
    cancelled = False
    started = time.monotonic()

    def begin(index: int) -> None:
        spec = steps[index]
        if spec.get("backend") in ("down", "up"):
            backend.down = spec["backend"] == "down"
        state.step = recorder.step = index
        log.add("caller", index, text=spec["say"])

    try:
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
    except Exception as exc:
        rejected = handshake_rejection(exc)
        error = (f"handshake HTTP {rejected[0]}: {rejected[1]}" if rejected
                 else f"{type(exc).__name__}: {exc}")
    finally:
        await http.aclose()

    run = {
        "session_id": session_id,
        "steps": [
            {"say": spec["say"], "explicit_yes": bool(spec.get("explicit_yes")),
             "auto_reply": bool(spec.get("auto_reply")),
             "agent": state.agent[i],
             "calls": [{k: c[k] for k in ("name", "args", "result")} for c in recorder.calls if c["step"] == i]}
            for i, spec in enumerate(steps)
        ],
        "final_order": order_service.get_order(session_id).model_dump(),
        "tool_call_cancellation_seen": cancelled,
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
        case_checks: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for run in runs:
            for name, res in run["checks"].items():
                buckets = [per_check[name]]
                if res.get("counted", True):
                    buckets.append(case_checks[name])
                for bucket in buckets:
                    bucket[0] += res["passed"]
                    bucket[1] += 1
        per_case[case["id"]] = {
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
    width = max(len(c) for c in per_case)
    print(f"{'case':<{width}}  pass   rate   failing checks")
    for cid, s in per_case.items():
        failing = [f"{k} {v}" for k, v in s["checks"].items() if v.split("/")[0] != v.split("/")[1]]
        flag = "" if s["pass_rate"] >= threshold else "  <-- below threshold"
        print(f"{cid:<{width}}  {s['passed_runs']}/{s['runs']}  {s['pass_rate']:>5.0%}   "
              f"{', '.join(failing) or '-'}{flag}")
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


async def preflight(client: Any, model: str) -> None:
    async with client.aio.live.connect(model=model, config={"response_modalities": ["AUDIO"]}):
        pass


async def main(args: argparse.Namespace) -> int:
    try:
        cases = load_cases(Path(args.cases_file), args.cases)
    except (OSError, ValueError, KeyError, yaml.YAMLError) as exc:
        print(f"Cannot load cases: {exc}", file=sys.stderr)
        return 2
    if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
        print("Set GEMINI_API_KEY (or GOOGLE_API_KEY), e.g. in .env.", file=sys.stderr)
        return 2
    client = make_live_client(args.api_version)
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
        "google_genai": genai.__version__, "runs": args.runs, "threshold": args.threshold,
        "git": git_info(),
    }
    sem = asyncio.Semaphore(args.concurrency)
    results: dict[str, list[dict]] = {c["id"]: [] for c in cases}

    async def one(case: dict, n: int) -> None:
        async with sem:
            run = await run_case(client, case, args)
            results[case["id"]].append(run)
            status = "PASS" if run["passed"] else "FAIL"
            print(f"  {status}  {case['id']} run {n}/{args.runs}  ({run['seconds']}s)", flush=True)

    print(f"Running {len(cases)} case(s) x {args.runs} with concurrency {args.concurrency}...")
    await asyncio.gather(*(one(c, n) for c in cases for n in range(1, args.runs + 1)))

    per_case, per_check = summarize(cases, results)
    print_report(meta, per_case, per_check, results, args.threshold)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORTS_DIR / f"report-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps({"meta": meta, "cases": per_case, "checks": per_check, "runs": results},
                              indent=2, default=str), encoding="utf-8")
    print(f"\nreport: {out.relative_to(REPO_ROOT)}")
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
    p.add_argument("--concurrency", type=int, default=3, help="Live sessions at once (default 3)")
    p.add_argument("--model", default="gemini-3.8-live")
    p.add_argument("--api-version", default=LIVE_API_VERSION)
    p.add_argument("--language", default=DEFAULT_LANGUAGE)
    p.add_argument("--step-timeout", type=float, default=60.0, help="seconds per caller turn")
    p.add_argument("--settle", type=float, default=2.0,
                   help="quiet seconds after a turn before the next caller turn")
    p.add_argument("--verbose", action="store_true", help="show bridge logs (tool calls etc.)")
    args = p.parse_args(argv)
    if args.runs < 1 or not 0 <= args.threshold <= 1:
        p.error("--runs must be >= 1 and --threshold between 0 and 1")
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
