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
