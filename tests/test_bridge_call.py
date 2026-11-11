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
