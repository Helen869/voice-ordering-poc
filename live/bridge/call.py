"""One phone call bridged to the Gemini Live API.

``CallBridge`` owns the call's ``session_id`` (generated once, when the call
connects) and keeps it across Live session resumption and reconnects. It runs
the model's function calls one at a time, in order, through ``ToolRouter``.

Barge-in: when the server sends ``toolCallCancellation``, no tool response is
sent for those ids. Calls that have not started are skipped entirely. A
cancelled submit_order that succeeds anyway is reported to the model with a
text "System note" on its next turn, so the caller still hears the
confirmation number.

The phone side is abstracted as ``AudioPort``; a telephony adapter (e.g. 8 kHz
mu-law <-> 16 kHz / 24 kHz PCM) is not implemented here.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any, Optional, Protocol

from live.bridge.tools import ToolRouter, is_successful_submit, load_declarations

logger = logging.getLogger("live_bridge")

SYSTEM_INSTRUCTION_PATH = Path(__file__).resolve().parent.parent / "system-instruction.md"
INPUT_AUDIO_MIME = "audio/pcm;rate=16000"
MAX_RECONNECTS = 5


def new_session_id() -> str:
    return f"live-{uuid.uuid4()}"


def load_system_instruction(path: Path = SYSTEM_INSTRUCTION_PATH) -> str:
    """Return the instruction text: everything after the first '---' line."""
    lines = path.read_text(encoding="utf-8").splitlines()
    try:
        start = lines.index("---") + 1
    except ValueError:
        start = 0
    return "\n".join(lines[start:]).strip()


def build_live_config(resumption_handle: Optional[str]) -> dict:
    return {
        "response_modalities": ["AUDIO"],
        "system_instruction": load_system_instruction(),
        "tools": [{"function_declarations": list(load_declarations().values())}],
        "session_resumption": {"handle": resumption_handle},
    }


def submit_note(confirmation_number: str) -> str:
    return (
        f"System note: the order was submitted, confirmation number "
        f"{confirmation_number}. Tell the caller."
    )


class AudioPort(Protocol):
    async def read(self) -> Optional[bytes]:
        """Next 16 kHz PCM chunk from the caller, or None when the call has ended."""

    async def play(self, pcm: bytes) -> None:
        """Play model audio (24 kHz PCM) to the caller."""

    async def clear(self) -> None:
        """Drop queued playback (caller barged in)."""


class CallBridge:
    def __init__(self, router: ToolRouter) -> None:
        self.router = router
        self.resumption_handle: Optional[str] = None
        self._session: Any = None  # current Live session; replaced on reconnect
        self._calls: asyncio.Queue = asyncio.Queue()
        self._cancelled: set[str] = set()
        self._sent_submits: dict[str, str] = {}  # call id -> confirmation, response already sent
        self._notes: list[str] = []
        self._noted: set[str] = set()  # confirmation numbers already noted
        self._model_idle = True

    @property
    def session_id(self) -> str:
        return self.router.session_id

    def attach(self, session: Any) -> None:
        self._session = session

    # -- server messages ----------------------------------------------------
    async def on_message(self, msg: Any) -> None:
        if getattr(msg, "session_resumption_update", None):
            upd = msg.session_resumption_update
            if upd.resumable and upd.new_handle:
                self.resumption_handle = upd.new_handle
        if getattr(msg, "tool_call", None):
            for fc in msg.tool_call.function_calls or []:
                self._calls.put_nowait(fc)
        if getattr(msg, "tool_call_cancellation", None):
            ids = msg.tool_call_cancellation.ids or []
            self._cancelled.update(ids)
            for call_id in ids:
                # Response already sent but the server discarded it: re-tell via note.
                if call_id in self._sent_submits:
                    self._queue_note(self._sent_submits.pop(call_id))
            await self._maybe_flush_notes()
        content = getattr(msg, "server_content", None)
        if content:
            if content.model_turn:
                self._model_idle = False
            if content.turn_complete or content.interrupted:
                self._model_idle = True
                await self._maybe_flush_notes()

    # -- tool execution (one at a time, in order) ---------------------------
    async def run_tools(self) -> None:
        while True:
            fc = await self._calls.get()
            try:
                await self._execute(fc)
            except Exception:  # never let one call kill the worker
                logger.exception("tool call %s failed", getattr(fc, "name", "?"))
            finally:
                self._calls.task_done()

    async def _execute(self, fc: Any) -> None:
        if fc.id in self._cancelled:
            logger.info("skipping cancelled call %s (%s)", fc.id, fc.name)
            return
        result = await self.router.call(fc.name, dict(fc.args or {}))
        submitted = fc.name == "submit_order" and is_successful_submit(result)
        if fc.id in self._cancelled:
            if submitted:
                self._queue_note(result["confirmation_number"])
                await self._maybe_flush_notes()
            return
        await self._session.send_tool_response(
            function_responses=[{"id": fc.id, "name": fc.name, "response": result}]
        )
        if submitted:
            self._sent_submits[fc.id] = result["confirmation_number"]

    # -- system notes -------------------------------------------------------
    def _queue_note(self, confirmation_number: str) -> None:
        if confirmation_number not in self._noted:
            self._noted.add(confirmation_number)
            self._notes.append(submit_note(confirmation_number))

    async def _maybe_flush_notes(self) -> None:
        if not self._model_idle or self._session is None:
            return
        while self._notes:
            # Realtime text, not send_client_content: the SDK warns against
            # interleaving client_content with realtime audio input.
            await self._session.send_realtime_input(text=self._notes.pop(0))

    # -- connection loop ----------------------------------------------------
    async def run(self, client: Any, model: str, audio: AudioPort) -> None:
        """Bridge until the call ends, resuming the Live session on go_away/drops."""
        worker = asyncio.create_task(self.run_tools())
        failures = 0
        try:
            while True:
                try:
                    config = build_live_config(self.resumption_handle)
                    async with client.aio.live.connect(model=model, config=config) as session:
                        self.attach(session)
                        failures = 0
                        if await self._pump(session, audio):
                            return  # caller hung up
                except Exception:
                    failures += 1
                    logger.exception("Live session dropped (%d/%d)", failures, MAX_RECONNECTS)
                    if failures >= MAX_RECONNECTS:
                        raise
                    await asyncio.sleep(min(0.5 * failures, 2.0))
                logger.info("reconnecting call %s with resumption handle", self.session_id)
        finally:
            worker.cancel()

    async def _pump(self, session: Any, audio: AudioPort) -> bool:
        """Run one Live session. True = call ended; False = reconnect needed."""

        async def upstream() -> None:
            while (chunk := await audio.read()) is not None:
                await session.send_realtime_input(
                    audio={"data": chunk, "mime_type": INPUT_AUDIO_MIME}
                )

        async def downstream() -> None:
            while True:  # receive() stops after each turn_complete
                async for msg in session.receive():
                    content = msg.server_content
                    if content and content.interrupted:
                        await audio.clear()
                    if content and content.model_turn:
                        for part in content.model_turn.parts or []:
                            if part.inline_data and part.inline_data.data:
                                await audio.play(part.inline_data.data)
                    await self.on_message(msg)
                    if msg.go_away:
                        return

        up = asyncio.create_task(upstream())
        down = asyncio.create_task(downstream())
        done, pending = await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            task.result()  # re-raise connection errors -> reconnect
        return up in done
