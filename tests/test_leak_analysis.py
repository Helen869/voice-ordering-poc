"""Unit tests for evals/leak_analysis.py (synthetic event logs, no API)."""

from evals.leak_analysis import analyze

S = 48_000  # bytes per second of 24 kHz 16-bit audio


def run_with(*events, agent="", passed=True):
    evs = [dict(e, seq=i, step=e.get("step", 0)) for i, e in enumerate(events)]
    return {"steps": [{"say": "x", "agent": agent, "first_audio_s": 1.0}], "events": evs,
            "checks": {}, "passed": passed}


def audio(secs):
    return {"type": "audio", "bytes": int(secs * S)}


def text(t):
    return {"type": "agent_text", "text": t}


def clean_run():
    return run_with(text("Sure, which protein?"), audio(4.4), agent="Sure, which protein?")


def test_unvoiced_and_voiced_leaks_and_mitigation():
    unvoiced = run_with(audio(4.4), text("Which protein?"), text("<context>{}</context>"),
                        agent="Which protein?<context>{}</context>")
    voiced = run_with(audio(4.4), text("Which protein? ```python"), audio(1.0), text(" df.sum()"), audio(12.0),
                      agent="Which protein? ```python df.sum()")
    report = {"meta": {}, "runs": {"ex2": [clean_run(), clean_run(), unvoiced, voiced]}}
    result = analyze(report)
    assert (result["leak_turns"], result["voiced"], result["unvoiced"]) == (2, 1, 1)
    v = next(x for x in result["leaks"] if x["voiced"])
    assert v["excess_s"] == 13.0                   # 17.4 s vs clean 4.4 s
    assert v["cut_s"] == 13.0                      # audio after the marker chunk
    assert v["leaked_cut_s"] == 13.0 and v["leaked_played_s"] == 0.0
    u = next(x for x in result["leaks"] if x["voiced"] is False)
    assert u["cut_s"] == 0.0 and u["normal_speech_cut_s"] == 0.0


def test_reports_without_audio_are_skipped():
    old = {"meta": {}, "runs": {"ex2": [{"steps": [{"say": "x", "agent": "{}"}], "events": [], "checks": {}}]}}
    assert analyze(old)["runs"] == 0


def test_bullet_reasoning_is_a_marker_and_unmarked_long_audio_is_reported():
    bullets = run_with(audio(4.4), text("Which protein? \n *   *Step 1: Check*"), audio(7.0),
                       agent="Which protein? \n *   *Step 1: Check*")
    unmarked = run_with(text("Which protein? One shrimp fried rice, $12.49."), audio(9.0),
                        agent="Which protein? One shrimp fried rice, $12.49.")
    report = {"meta": {}, "runs": {"ex2": [clean_run() for _ in range(10)] + [bullets, unmarked]}}
    result = analyze(report)
    assert result["leak_turns"] == 1 and result["voiced"] == 1       # bullets caught, 7 s extra
    assert [x["run"] for x in result["unmarked_long_audio"]] == [12]  # no marker, 4.6 s extra


def test_one_long_clean_turn_does_not_hide_a_voiced_leak():
    long_clean = run_with(text("Which protein?"), audio(11.5), agent="Which protein?")
    leak = run_with(audio(4.4), text("Which protein? ```"), audio(4.7), agent="Which protein? ```")
    report = {"meta": {}, "runs": {"ex2": [clean_run() for _ in range(10)] + [long_clean, leak]}}
    assert analyze(report)["voiced"] == 1  # p90 spread, not max: the 11.5 s outlier doesn't mask it
