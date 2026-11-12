"""Leak analysis across eval reports (experiment arms).

    python -m evals.leak_analysis evals/reports/report-A.json evals/reports/report-B.json ...

For each report (one arm), per caller turn:

- **Leak**: the agent transcript contains a leak marker (JSON braces, code
  fence, markup tag, markdown rule or bold).
- **Voiced or unvoiced**: compares the turn's real audio duration with clean
  turns (no marker) of the same case and step in the same report. A leak turn
  is *voiced* if its audio exceeds the clean median by more than
  max(2 s, (clean p90 - median) + 1 s); otherwise the leaked text was not
  spoken. p90, not max: one long unmarked turn must not hide the others.
- **Unmarked long audio**: a turn with no marker whose audio exceeds the same
  limit. Spoken extra content the marker-based mitigation cannot catch.
- **Simulated mitigation**: stop forwarding audio to the caller from the first
  transcript chunk that contains a marker. Reports how much excess (leaked)
  audio that would cut, how much had already played (the transcript lags the
  audio), and how much normal speech it would wrongly cut.

Needs reports with audio events (written by the runner since audio logging
was added).
"""

from __future__ import annotations

import json
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

BYTES_PER_SECOND = 24_000 * 2
from live.bridge.call import LEAK_MARKER  # the same markers the bridge's audio cut uses
VOICED_MIN_EXCESS_S = 2.0


def turn_records(run: dict) -> list[dict]:
    """Per caller turn: transcript, audio seconds, and mitigation numbers."""
    events = run.get("events") or []
    out = []
    for index, step in enumerate(run["steps"]):
        evs = [e for e in events if e.get("step") == index]
        audio = [e for e in evs if e["type"] == "audio"]
        text, marker_seq = "", None
        for e in evs:
            if e["type"] == "agent_text":
                text += e["text"]
                if marker_seq is None and LEAK_MARKER.search(text):
                    marker_seq = e["seq"]
        if any("forwarded" in e for e in audio):  # bridge guard active: real cuts
            cut = sum(e["bytes"] for e in audio if e.get("forwarded") is False)
        else:  # older reports: simulate a cut after the first marker chunk
            cut = sum(e["bytes"] for e in audio if marker_seq is not None and e["seq"] > marker_seq)
        out.append({
            "step": index,
            "leak": marker_seq is not None,
            "audio_s": sum(e["bytes"] for e in audio) / BYTES_PER_SECOND,
            "cut_s": cut / BYTES_PER_SECOND,
            "first_audio_s": step.get("first_audio_s"),
            "text": step["agent"],
        })
    return out


def analyze(report: dict) -> dict:
    turns: list[tuple[str, int, dict]] = []  # (case, run number, turn)
    runs_total, hallucination_runs, ungrounded_runs, latencies = 0, 0, 0, []
    for cid, runs in report["runs"].items():
        for n, run in enumerate(runs, 1):
            if not any(e["type"] == "audio" for e in run.get("events") or []):
                continue
            runs_total += 1
            checks = run.get("checks", {})
            hallucination_runs += not checks.get("unordered_item_confirmed", {"passed": True})["passed"]
            ungrounded_runs += not checks.get("ungrounded_item_mentioned", {"passed": True})["passed"]
            for t in turn_records(run):
                turns.append((cid, n, t))
                if t["first_audio_s"] is not None:
                    latencies.append(t["first_audio_s"])

    clean: dict[tuple[str, int], list[float]] = defaultdict(list)
    for cid, _, t in turns:
        if not t["leak"]:
            clean[(cid, t["step"])].append(t["audio_s"])

    def limits(base: list[float]) -> tuple[float, float]:
        """(median, excess above which extra audio counts as spoken content)."""
        ordered = sorted(base)
        median = statistics.median(ordered)
        p90 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))]
        return median, max(VOICED_MIN_EXCESS_S, (p90 - median) + 1.0)

    unmarked = []
    for cid, n, t in turns:
        base = clean.get((cid, t["step"]))
        if t["leak"] or not base:
            continue
        median, limit = limits(base)
        if t["audio_s"] - median > limit:
            unmarked.append({"case": cid, "run": n, "step": t["step"] + 1, "audio_s": round(t["audio_s"], 2),
                             "clean_median_s": round(median, 2), "text": t["text"]})

    leaks = []
    for cid, n, t in turns:
        if not t["leak"]:
            continue
        base = clean.get((cid, t["step"]))
        if not base:
            leaks.append({"case": cid, "run": n, "step": t["step"] + 1, "voiced": None,
                          "excess_s": None, "cut_s": t["cut_s"], "text": t["text"]})
            continue
        median, limit = limits(base)
        excess = t["audio_s"] - median
        voiced = excess > limit
        leaked = max(0.0, excess) if voiced else 0.0
        leaks.append({
            "case": cid, "run": n, "step": t["step"] + 1, "voiced": voiced,
            "audio_s": round(t["audio_s"], 2), "clean_median_s": round(median, 2),
            "excess_s": round(excess, 2),
            "cut_s": round(t["cut_s"], 2),
            "leaked_cut_s": round(min(t["cut_s"], leaked), 2),
            "leaked_played_s": round(max(0.0, leaked - t["cut_s"]), 2),
            "normal_speech_cut_s": round(max(0.0, t["cut_s"] - leaked), 2),
            "text": t["text"],
        })

    latencies.sort()
    meta = report.get("meta", {})
    return {
        "arm": {k: meta.get(k) for k in ("model", "system_instruction", "tool_results", "live_config")},
        "runs": runs_total,
        "turns": len(turns),
        "leak_turns": len(leaks),
        "voiced": sum(1 for x in leaks if x["voiced"]),
        "unvoiced": sum(1 for x in leaks if x["voiced"] is False),
        "unclassified": sum(1 for x in leaks if x["voiced"] is None),
        "unmarked_long_audio": unmarked,
        "normal_turns": sum(1 for _, _, t in turns if not t["leak"]),
        "normal_speech_cut_s": round(sum(x.get("normal_speech_cut_s") or 0 for x in leaks), 2),
        "hallucination_runs": hallucination_runs,
        "ungrounded_runs": ungrounded_runs,
        "latency_median_s": latencies[len(latencies) // 2] if latencies else None,
        "latency_p90_s": latencies[min(len(latencies) - 1, int(len(latencies) * 0.9))] if latencies else None,
        "leaks": leaks,
    }


def main(paths: list[str]) -> int:
    for path in paths:
        result = analyze(json.loads(Path(path).read_text(encoding="utf-8")))
        a = result["arm"]
        print(f"\n== {Path(path).name}: model={a['model']} prompt={a['system_instruction']} "
              f"results={a['tool_results']} live_config={a['live_config']}")
        print(f"   runs={result['runs']} turns={result['turns']}  leak turns={result['leak_turns']} "
              f"(voiced {result['voiced']}, unvoiced {result['unvoiced']}, unclassified {result['unclassified']})  "
              f"confirmed-not-ordered runs={result['hallucination_runs']}  "
              f"ungrounded-item runs={result['ungrounded_runs']}  "
              f"latency median={result['latency_median_s']}s p90={result['latency_p90_s']}s")
        print(f"   simulated mitigation: marker fired on {result['leak_turns']} of {result['turns']} turns "
              f"(every one had leaked text); normal speech it would have cut = "
              f"{result['normal_speech_cut_s']}s; unmarked long-audio turns (mitigation can't catch) = "
              f"{len(result['unmarked_long_audio'])}")
        for x in result["unmarked_long_audio"]:
            print(f"   UNMARKED     {x['case']} run {x['run']} step {x['step']}: audio {x['audio_s']}s vs "
                  f"clean {x['clean_median_s']}s: {x['text'][:100]!r}")
        for x in result["leaks"]:
            kind = {True: "VOICED", False: "unvoiced", None: "unclassified"}[x["voiced"]]
            detail = (f"audio {x.get('audio_s')}s vs clean {x.get('clean_median_s')}s (excess {x['excess_s']}s); "
                      f"mitigation cuts {x['cut_s']}s: leaked cut {x.get('leaked_cut_s')}s, "
                      f"leaked already played {x.get('leaked_played_s')}s, "
                      f"normal speech cut {x.get('normal_speech_cut_s')}s")
            print(f"   {kind:12} {x['case']} run {x['run']} step {x['step']}: {detail}")
            print(f"                {x['text'][:110]!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
