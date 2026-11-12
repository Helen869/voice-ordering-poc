"""Unit tests for per-call bridge behaviour (live/bridge/call.py): cancellation and notes."""

import asyncio
from types import SimpleNamespace as NS

from live.bridge.call import CallBridge, build_live_config, load_system_instruction, new_session_id


class FakeRouter:
    session_id = "live-test"

    def __init__(self, result=None, gate=None):
        self.calls = []
        self.result = result or {"state": "submitted", "confirmation_number": "GW-1042"}
        self.gate = gate  # asyncio.Event: hold the call "in flight" until set

    async def call(self, name, args):
        self.calls.append((name, args))
        if self.gate:
            await self.gate.wait()
        return self.result


class FakeSession:
    def __init__(self):
        self.tool_responses = []
        self.texts = []

    async def send_tool_response(self, *, function_responses):
        self.tool_responses.extend(function_responses)

    async def send_realtime_input(self, *, text=None, **_):
        self.texts.append(text)


def msg(**fields):
    base = dict(session_resumption_update=None, tool_call=None,
                tool_call_cancellation=None, server_content=None)
    base.update(fields)
    return NS(**base)


def tool_call(*calls):
    return msg(tool_call=NS(function_calls=[NS(id=i, name=n, args=a) for i, n, a in calls]))


def cancel(*ids):
    return msg(tool_call_cancellation=NS(ids=list(ids)))


def content(model_turn=None, turn_complete=False, interrupted=False):
    return msg(server_content=NS(model_turn=model_turn, turn_complete=turn_complete,
                                 interrupted=interrupted))


async def drain(bridge):
    await asyncio.wait_for(bridge._calls.join(), 1)


def test_tool_calls_run_in_order_and_respond():
    async def go():
        router, session = FakeRouter(result={"subtotal": 1}), FakeSession()
        bridge = CallBridge(router)
        bridge.attach(session)
        worker = asyncio.create_task(bridge.run_tools())
        await bridge.on_message(tool_call(("1", "update_order", {"a": 1}), ("2", "get_order", None)))
        await drain(bridge)
        worker.cancel()
        return router, session

    router, session = asyncio.run(go())
    assert [c[0] for c in router.calls] == ["update_order", "get_order"]
    assert [r["id"] for r in session.tool_responses] == ["1", "2"]


def test_cancelled_before_start_is_never_executed():
    async def go():
        gate = asyncio.Event()
        router, session = FakeRouter(gate=gate), FakeSession()
        bridge = CallBridge(router)
        bridge.attach(session)
        worker = asyncio.create_task(bridge.run_tools())
        await bridge.on_message(tool_call(("1", "get_order", None), ("2", "update_order", {})))
        await asyncio.sleep(0)  # call 1 now in flight
        await bridge.on_message(cancel("2"))
        gate.set()
        await drain(bridge)
        worker.cancel()
        return router, session

    router, session = asyncio.run(go())
    assert [c[0] for c in router.calls] == ["get_order"]
    assert [r["id"] for r in session.tool_responses] == ["1"]


def test_cancelled_in_flight_submit_success_sends_note_not_response():
    async def go():
        gate = asyncio.Event()
        session = FakeSession()
        bridge = CallBridge(FakeRouter(gate=gate))
        bridge.attach(session)
        worker = asyncio.create_task(bridge.run_tools())
        await bridge.on_message(tool_call(("s1", "submit_order", None)))
        await asyncio.sleep(0)
        await bridge.on_message(content(model_turn=NS(parts=[])))  # model speaking again
        await bridge.on_message(cancel("s1"))
        gate.set()
        await drain(bridge)
        held = list(session.texts)  # model busy: note held
        await bridge.on_message(content(turn_complete=True))
        worker.cancel()
        return session, held

    session, held = asyncio.run(go())
    assert session.tool_responses == []
    assert held == []
    assert session.texts == [
        "System note: the order was submitted, confirmation number GW-1042. Tell the caller."
    ]


def test_cancelled_submit_that_failed_sends_nothing():
    async def go():
        gate = asyncio.Event()
        session = FakeSession()
        bridge = CallBridge(FakeRouter(result={"error": {"code": "backend_unavailable"}}, gate=gate))
        bridge.attach(session)
        worker = asyncio.create_task(bridge.run_tools())
        await bridge.on_message(tool_call(("s1", "submit_order", None)))
        await asyncio.sleep(0)
        await bridge.on_message(cancel("s1"))
        gate.set()
        await drain(bridge)
        await bridge.on_message(content(turn_complete=True))
        worker.cancel()
        return session

    session = asyncio.run(go())
    assert session.tool_responses == [] and session.texts == []


def test_cancellation_after_submit_response_sent_triggers_note():
    async def go():
        session = FakeSession()
        bridge = CallBridge(FakeRouter())
        bridge.attach(session)
        worker = asyncio.create_task(bridge.run_tools())
        await bridge.on_message(tool_call(("s1", "submit_order", None)))
        await drain(bridge)
        await bridge.on_message(cancel("s1"))  # model idle -> sent immediately
        await bridge.on_message(cancel("s1"))  # duplicate: no second note
        worker.cancel()
        return session

    session = asyncio.run(go())
    assert len(session.tool_responses) == 1
    assert len(session.texts) == 1 and "GW-1042" in session.texts[0]


def test_session_id_and_resumption_handle_survive_reconnect():
    bridge = CallBridge(FakeRouter())
    asyncio.run(bridge.on_message(msg(session_resumption_update=NS(resumable=True, new_handle="h1"))))
    asyncio.run(bridge.on_message(msg(session_resumption_update=NS(resumable=False, new_handle=None))))
    bridge.attach(FakeSession())  # new Live session after reconnect
    assert bridge.resumption_handle == "h1"
    assert bridge.session_id == "live-test"
    assert build_live_config(bridge.resumption_handle)["session_resumption"] == {"handle": "h1"}


def test_new_session_ids_are_unique():
    assert new_session_id() != new_session_id()
    assert new_session_id().startswith("live-")


def test_system_instruction_excludes_file_preamble():
    text = load_system_instruction()
    assert text.startswith("## Goal")
    assert "Everything below the line" not in text


# -- connection handling: fail fast on 4xx, retry otherwise -------------------------
import logging

import pytest
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

import live.bridge.call as call_mod
from live.bridge.call import LIVE_API_VERSION, LiveHandshakeError, handshake_rejection


def rejected(status, body=b""):
    return InvalidStatus(Response(status, "x", Headers(), body))


class FailingClient:
    """client.aio.live.connect(...) raises ``exc`` on every attempt."""

    def __init__(self, exc):
        self.attempts = 0
        client = self

        class _Conn:
            async def __aenter__(self):
                client.attempts += 1
                raise exc

            async def __aexit__(self, *a):
                return False

        self.aio = NS(live=NS(connect=lambda **kw: _Conn()))


@pytest.fixture()
def no_sleep(monkeypatch):
    async def instant(_):
        return None
    monkeypatch.setattr(call_mod.asyncio, "sleep", instant)


def test_handshake_rejection_reads_status_and_body():
    assert handshake_rejection(rejected(404)) == (404, "<empty>")
    assert handshake_rejection(rejected(403, b'{"error": "denied"}')) == (403, '{"error": "denied"}')
    assert handshake_rejection(rejected(503)) is None
    assert handshake_rejection(ConnectionError("reset")) is None


def test_4xx_handshake_fails_fast_with_body_in_error(no_sleep):
    client = FailingClient(rejected(404, b"model not found"))
    with pytest.raises(LiveHandshakeError, match=r"HTTP 404 \(model=m\); response body: model not found"):
        asyncio.run(CallBridge(FakeRouter()).run(client, "m", audio=None))
    assert client.attempts == 1


def test_other_connection_errors_retry_up_to_limit(no_sleep):
    client = FailingClient(rejected(503))
    with pytest.raises(InvalidStatus):
        asyncio.run(CallBridge(FakeRouter()).run(client, "m", audio=None))
    assert client.attempts == call_mod.MAX_RECONNECTS


def test_reconnect_log_mentions_handle_only_when_present(no_sleep, caplog):
    caplog.set_level(logging.INFO, logger="live_bridge")
    with pytest.raises(ConnectionError):
        asyncio.run(CallBridge(FakeRouter()).run(FailingClient(ConnectionError("x")), "m", audio=None))
    assert "no resumption handle yet" in caplog.text
    assert "with resumption handle" not in caplog.text

    caplog.clear()
    bridge = CallBridge(FakeRouter())
    bridge.resumption_handle = "h1"
    with pytest.raises(ConnectionError):
        asyncio.run(bridge.run(FailingClient(ConnectionError("x")), "m", audio=None))
    assert "with resumption handle" in caplog.text


def test_live_client_uses_v1beta_by_default(monkeypatch):
    pytest.importorskip("google.genai")
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-key-for-test")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    client = call_mod.make_live_client()
    assert LIVE_API_VERSION == "v1beta"
    assert client._api_client._http_options.api_version == "v1beta"


# -- diagnostics and clean shutdown ---------------------------------------------------
from array import array

from live.bridge.call import PumpStats, message_kinds, pcm16_peak


def pcm(*samples):
    return array("h", samples).tobytes()


def test_pcm16_peak():
    assert pcm16_peak(pcm(0, 100, -2000, 5)) == 2000
    assert pcm16_peak(b"") == 0


def test_message_kinds():
    m = msg(tool_call=NS(function_calls=[]), server_content=NS(
        model_turn=NS(parts=[]), turn_complete=True, interrupted=False,
        output_transcription=NS(text="hi")))
    assert message_kinds(m) == ["tool_call", "model_turn", "turn_complete", "output_transcription"]
    assert message_kinds(msg()) == ["<empty>"]


def test_pump_stats_snapshot_resets():
    s = PumpStats()
    s.mic(pcm(10, -300))
    s.model_audio(b"\x00" * 480)
    s.message(content(turn_complete=True))
    assert s.snapshot() == {"mic_chunks": 1, "mic_peak": 300, "model_chunks": 1,
                            "model_bytes": 480, "kinds": {"turn_complete": 1}}
    assert s.snapshot()["mic_chunks"] == 0


class ScriptedSession(FakeSession):
    """receive() yields ``script`` once, then blocks like an idle socket."""

    def __init__(self, script=()):
        super().__init__()
        self.script = list(script)
        self.audio_sent = []

    async def send_realtime_input(self, *, audio=None, text=None, **_):
        if audio is not None:
            self.audio_sent.append(audio["data"])
        if text is not None:
            self.texts.append(text)

    async def receive(self):
        while self.script:
            yield self.script.pop(0)
        await asyncio.Event().wait()


class ScriptedAudio:
    def __init__(self, chunks=()):
        self.chunks = list(chunks)
        self.played = []

    async def read(self):
        if self.chunks:
            return self.chunks.pop(0)
        await asyncio.Event().wait()

    async def play(self, data):
        self.played.append(data)

    async def clear(self):
        pass


class OkClient:
    def __init__(self, session):
        self.session = session
        self.closed = False
        client = self

        class _Conn:
            async def __aenter__(self):
                return client.session

            async def __aexit__(self, *a):
                client.closed = True
                return False

        self.aio = NS(live=NS(connect=lambda **kw: _Conn()))


def live_msg(audio=None, go_away=False):
    parts = [NS(inline_data=NS(data=audio))] if audio else []
    return NS(server_content=NS(model_turn=NS(parts=parts) if audio else None,
                                turn_complete=False, interrupted=False),
              tool_call=None, tool_call_cancellation=None, session_resumption_update=None,
              go_away=NS(time_left="1s") if go_away else None)


def test_pump_records_stats_and_plays_audio():
    async def go():
        session = ScriptedSession([live_msg(audio=b"\x01\x00" * 240), live_msg(go_away=True)])
        audio = ScriptedAudio([pcm(0, 1234)])
        bridge = CallBridge(FakeRouter())
        bridge.stats = PumpStats()
        ended = await bridge._pump(session, audio)
        return ended, bridge.stats.snapshot(), session, audio

    ended, snap, session, audio = asyncio.run(go())
    assert ended is False  # go_away -> reconnect
    assert snap["mic_chunks"] == 1 and snap["mic_peak"] == 1234
    assert snap["model_chunks"] == 1 and snap["model_bytes"] == 480
    assert snap["kinds"]["go_away"] == 1 and snap["kinds"]["model_turn"] == 1
    assert audio.played == [b"\x01\x00" * 240]
    assert session.audio_sent == [pcm(0, 1234)]


def test_cancelling_run_awaits_every_task_and_closes_session():
    async def go():
        client = OkClient(ScriptedSession())
        loop_errors = []
        asyncio.get_running_loop().set_exception_handler(lambda l, c: loop_errors.append(c))
        call = asyncio.create_task(CallBridge(FakeRouter()).run(client, "m", ScriptedAudio()))
        await asyncio.sleep(0.05)  # upstream, downstream and tool worker all running
        call.cancel()  # what Ctrl+C does to the main task
        await asyncio.gather(call, return_exceptions=True)
        leftover = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        return call, client, leftover, loop_errors

    call, client, leftover, loop_errors = asyncio.run(go())
    assert call.cancelled()
    assert leftover == []
    assert client.closed
    assert loop_errors == []


def test_harness_parse_device():
    pytest.importorskip("sounddevice")
    from live.bridge.local_harness import parse_device

    assert parse_device(None) is None
    assert parse_device("3") == 3
    assert parse_device("Headset") == "Headset"


# -- language -------------------------------------------------------------------------
def test_live_config_sets_speech_language_by_default():
    cfg = build_live_config(None)
    assert cfg["speech_config"] == {"language_code": "en-US"}


def test_live_config_language_override_and_disable():
    assert build_live_config(None, language="en-GB")["speech_config"] == {"language_code": "en-GB"}
    assert "speech_config" not in build_live_config(None, language=None)
    assert "speech_config" not in build_live_config(None, language="")


def test_bridge_passes_language_to_config():
    async def go():
        seen = []
        client = OkClient(ScriptedSession())
        connect = client.aio.live.connect
        client.aio.live.connect = lambda **kw: (seen.append(kw["config"]), connect(**kw))[1]
        call = asyncio.create_task(
            CallBridge(FakeRouter(), language="en-AU").run(client, "m", ScriptedAudio()))
        await asyncio.sleep(0.05)
        call.cancel()
        await asyncio.gather(call, return_exceptions=True)
        return seen

    assert asyncio.run(go())[0]["speech_config"] == {"language_code": "en-AU"}


# -- one tool response per toolCall message -------------------------------------------
class CountingSession(FakeSession):
    def __init__(self):
        super().__init__()
        self.batches = []

    async def send_tool_response(self, *, function_responses):
        self.batches.append([r["id"] for r in function_responses])
        await super().send_tool_response(function_responses=function_responses)


def test_multi_call_message_gets_one_tool_response_in_order():
    async def go():
        session = CountingSession()
        bridge = CallBridge(FakeRouter(result={"subtotal": 1}))
        bridge.attach(session)
        worker = asyncio.create_task(bridge.run_tools())
        await bridge.on_message(tool_call(("a", "update_order", {}), ("b", "update_order", {}),
                                          ("c", "update_order", {})))
        assert not bridge.tools_idle
        await drain(bridge)
        worker.cancel()
        return session, bridge

    session, bridge = asyncio.run(go())
    assert session.batches == [["a", "b", "c"]]  # not [["a"], ["b"], ["c"]]
    assert bridge.tools_idle


def test_call_cancelled_mid_batch_is_left_out_of_the_response():
    async def go():
        gate = asyncio.Event()
        session = CountingSession()
        router = FakeRouter(result={"subtotal": 1}, gate=gate)
        bridge = CallBridge(router)
        bridge.attach(session)
        worker = asyncio.create_task(bridge.run_tools())
        await bridge.on_message(tool_call(("a", "get_order", {}), ("b", "update_order", {})))
        await asyncio.sleep(0)  # "a" in flight
        await bridge.on_message(cancel("a"))
        gate.set()
        await drain(bridge)
        worker.cancel()
        return session

    assert asyncio.run(go()).batches == [["b"]]


# -- confirmation-number guard --------------------------------------------------------
from live.bridge.call import NO_NUMBER_NOTE, spoken_confirmation_numbers, wrong_number_note

PLACED = {"state": "submitted", "confirmation_number": "GW-1046"}


def agent_said(text, turn_complete=False):
    return msg(server_content=NS(model_turn=NS(parts=[]), turn_complete=turn_complete,
                                 interrupted=False, output_transcription=NS(text=text)))


async def placed_bridge():
    """A bridge whose order was just submitted as GW-1046."""
    session = FakeSession()
    bridge = CallBridge(FakeRouter(result=PLACED))
    bridge.attach(session)
    worker = asyncio.create_task(bridge.run_tools())
    await bridge.on_message(tool_call(("s1", "submit_order", None)))
    await drain(bridge)
    return bridge, session, worker


def test_spoken_confirmation_numbers():
    assert spoken_confirmation_numbers("Your confirmation number is GW-1046. Thanks") == [("1046", 35)]
    assert spoken_confirmation_numbers("G W 1 0 4 6 and GW-12345") == [("1046", 11), ("12345", 24)]
    assert spoken_confirmation_numbers("That's $10.46 total") == []


def test_wrong_number_triggers_correction_note_after_the_turn():
    async def go():
        bridge, session, worker = await placed_bridge()
        await bridge.on_message(agent_said("Order placed! Your confirmation number is GW-1042. "))
        held = list(session.texts)  # model still speaking: note waits
        await bridge.on_message(agent_said("Thanks for calling!", turn_complete=True))
        worker.cancel()
        return held, session.texts

    held, texts = asyncio.run(go())
    assert held == []
    assert texts == [wrong_number_note("GW-1046")]
    assert texts[0] == ("System note: You said the wrong confirmation number. "
                        "Correct yourself: the confirmation number is GW-1046.")


def test_correct_number_sends_nothing():
    async def go():
        bridge, session, worker = await placed_bridge()
        await bridge.on_message(agent_said("Your confirmation number is GW-10"))
        await bridge.on_message(agent_said("46. Thanks!", turn_complete=True))  # split across chunks
        worker.cancel()
        return session.texts

    assert asyncio.run(go()) == []


def test_number_split_across_chunks_is_read_whole():
    async def go():
        bridge, session, worker = await placed_bridge()
        await bridge.on_message(agent_said("Your number is GW-104"))
        await bridge.on_message(agent_said("6"))  # still GW-1046: must not judge "104"/"1046" early
        await bridge.on_message(agent_said("5.", turn_complete=True))  # actually GW-10465
        worker.cancel()
        return session.texts

    assert asyncio.run(go()) == [wrong_number_note("GW-1046")]


def test_same_wrong_number_corrected_once():
    async def go():
        bridge, session, worker = await placed_bridge()
        await bridge.on_message(agent_said("It's GW-1042, yes GW-1042.", turn_complete=True))
        await bridge.on_message(agent_said("Again, GW-1042.", turn_complete=True))
        worker.cancel()
        return session.texts

    assert asyncio.run(go()) == [wrong_number_note("GW-1046")]


def test_early_number_checked_when_result_arrives():
    async def go(early):
        session = FakeSession()
        bridge = CallBridge(FakeRouter(result=PLACED))
        bridge.attach(session)
        worker = asyncio.create_task(bridge.run_tools())
        await bridge.on_message(agent_said(f"Your confirmation number is {early}. "))  # before submit
        await bridge.on_message(tool_call(("s1", "submit_order", None)))
        await drain(bridge)
        await bridge.on_message(content(turn_complete=True))
        worker.cancel()
        return session.texts

    assert asyncio.run(go("GW-5555")) == [wrong_number_note("GW-1046")]
    assert asyncio.run(go("GW-1046")) == []  # lucky guess: same number, nothing to correct


def test_number_spoken_with_no_order_placed_gets_no_number_note():
    async def go():
        session = FakeSession()
        bridge = CallBridge(FakeRouter())
        bridge.attach(session)
        await bridge.on_message(agent_said("Your confirmation number is GW-1042.", turn_complete=True))
        return session.texts

    assert asyncio.run(go()) == [NO_NUMBER_NOTE]


def test_live_config_always_has_output_transcription():
    assert build_live_config(None)["output_audio_transcription"] == {}


def test_awaiting_reply_until_model_output_after_tool_response():
    async def go():
        session = FakeSession()
        bridge = CallBridge(FakeRouter(result={"subtotal": 1}))
        bridge.attach(session)
        worker = asyncio.create_task(bridge.run_tools())
        states = [bridge.awaiting_reply]
        await bridge.on_message(tool_call(("1", "search_menu", {})))
        await drain(bridge)
        states.append(bridge.awaiting_reply)          # response sent, no reply yet
        await bridge.on_message(content(turn_complete=True))
        states.append(bridge.awaiting_reply)          # turn_complete is not a reply
        await bridge.on_message(content(model_turn=NS(parts=[])))
        states.append(bridge.awaiting_reply)          # model speaks: answered
        worker.cancel()
        return states

    assert asyncio.run(go()) == [False, True, True, False]
