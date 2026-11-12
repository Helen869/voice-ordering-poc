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
import re
import uuid
from array import array
from collections import Counter
from pathlib import Path
from typing import Any, Optional, Protocol

from live.bridge.tools import (
    INVALID_ARGUMENTS,
    ToolRouter,
    error_result,
    is_successful_submit,
    load_declarations,
)

logger = logging.getLogger("live_bridge")

SYSTEM_INSTRUCTION_PATH = Path(__file__).resolve().parent.parent / "system-instruction.md"
INPUT_AUDIO_MIME = "audio/pcm;rate=16000"
MAX_RECONNECTS = 5
# The SDK's default api_version ("v1" in google-genai 3.1) has no Live endpoint:
# the WebSocket handshake fails with HTTP 404 and an empty body.
LIVE_API_VERSION = "v1beta"
DEFAULT_LANGUAGE = "en-US"


class LiveHandshakeError(RuntimeError):
    """The Live API rejected the WebSocket handshake with a 4xx (not retryable)."""


_CONTENT_KINDS = (
    "model_turn", "turn_complete", "interrupted", "generation_complete",
    "input_transcription", "output_transcription", "waiting_for_input",
)
_MESSAGE_KINDS = (
    "setup_complete", "tool_call", "tool_call_cancellation", "go_away",
    "session_resumption_update", "usage_metadata", "voice_activity",
    "voice_activity_detection_signal",
)


def message_kinds(msg: Any) -> list[str]:
    """Names of the fields set on a LiveServerMessage, e.g. ['turn_complete']."""
    kinds = [k for k in _MESSAGE_KINDS if getattr(msg, k, None)]
    content = getattr(msg, "server_content", None)
    if content:
        kinds += [k for k in _CONTENT_KINDS if getattr(content, k, None)]
    return kinds or ["<empty>"]


def pcm16_peak(chunk: bytes) -> int:
    samples = array("h")
    samples.frombytes(chunk[: len(chunk) - len(chunk) % 2])
    return max(max(samples), -min(samples)) if samples else 0


class PumpStats:
    """Counters for --debug diagnostics; read and reset once per second."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.mic_chunks = 0
        self.mic_peak = 0
        self.model_chunks = 0
        self.model_bytes = 0
        self.kinds: Counter[str] = Counter()

    def mic(self, chunk: bytes) -> None:
        self.mic_chunks += 1
        self.mic_peak = max(self.mic_peak, pcm16_peak(chunk))

    def model_audio(self, data: bytes) -> None:
        self.model_chunks += 1
        self.model_bytes += len(data)

    def message(self, msg: Any) -> None:
        self.kinds.update(message_kinds(msg))

    def snapshot(self) -> dict:
        snap = {
            "mic_chunks": self.mic_chunks, "mic_peak": self.mic_peak,
            "model_chunks": self.model_chunks, "model_bytes": self.model_bytes,
            "kinds": dict(self.kinds),
        }
        self.reset()
        return snap


def new_session_id() -> str:
    return f"live-{uuid.uuid4()}"


def make_live_client(api_version: str = LIVE_API_VERSION, api_key: Optional[str] = None) -> Any:
    """Live API client. Without ``api_key`` the SDK reads GEMINI_API_KEY /
    GOOGLE_API_KEY from the environment."""
    from google import genai

    if api_key:
        return genai.Client(api_key=api_key, http_options={"api_version": api_version})
    return genai.Client(http_options={"api_version": api_version})


def handshake_rejection(exc: BaseException) -> Optional[tuple[int, str]]:
    """(status, body) if ``exc`` is a 4xx WebSocket handshake rejection, else None."""
    resp = getattr(exc, "response", None)
    status = getattr(resp, "status_code", None)
    if not isinstance(status, int) or not 400 <= status < 500:
        return None
    body = getattr(resp, "body", None) or b""
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    return status, body.strip() or "<empty>"


def load_system_instruction(path: Path = SYSTEM_INSTRUCTION_PATH) -> str:
    """Return the instruction text: everything after the first '---' line."""
    lines = path.read_text(encoding="utf-8").splitlines()
    try:
        start = lines.index("---") + 1
    except ValueError:
        start = 0
    return "\n".join(lines[start:]).strip()


def build_live_config(
    resumption_handle: Optional[str],
    extra: Optional[dict] = None,
    language: Optional[str] = DEFAULT_LANGUAGE,
) -> dict:
    """Live session config. ``language`` sets speech_config.language_code, which
    governs the agent's speech synthesis; None or "" leaves it to the model."""
    config: dict = {
        "response_modalities": ["AUDIO"],
        "system_instruction": load_system_instruction(),
        "tools": [{"function_declarations": list(load_declarations().values())}],
        # The confirmation-number and audio-cut guards read the agent's transcript;
        # the guessed-answer guard needs to know when the caller has spoken.
        "output_audio_transcription": {},
        "input_audio_transcription": {},
    }
    if language:
        config["speech_config"] = {"language_code": language}
    config.update(extra or {})
    config["session_resumption"] = {"handle": resumption_handle}
    return config


# Spoken confirmation numbers: "GW-1045", "GW 1045", "G W 1 0 4 5",
# "confirmation number is 1045". Captures the whole number (GW-12345 too).
SPOKEN_CONFIRMATION = re.compile(
    r"\bG\s*\.?\s*-?\s*W\b\s*\.?\s*[-#:]?\s*((?:\d[\s-]?){3,}\d)(?!\d)"
    r"|confirmation number(?: is)?\s*:?\s*((?:\d[\s-]?){3,}\d)(?!\d)",
    re.IGNORECASE,
)


def spoken_confirmation_numbers(text: str) -> list[tuple[str, int]]:
    """(digits, end offset) for every confirmation number in ``text``."""
    return [(re.sub(r"\D", "", m.group(1) or m.group(2)), m.end())
            for m in SPOKEN_CONFIRMATION.finditer(text)]


def wrong_number_note(real: str) -> str:
    return (f"System note: You said the wrong confirmation number. "
            f"Correct yourself: the confirmation number is {real}.")


NO_NUMBER_NOTE = ("System note: no order has been placed yet, so there is no "
                  "confirmation number. Don't give one until the order is placed.")


# Transcript text that is never speech: JSON braces, backticks (code), a markup tag
# (<context>, <no speech>), a markdown rule, bold, or a bullet line ("*   *Step 1").
# Conservative on purpose: normal spoken replies contain none of these.
LEAK_MARKER = re.compile(r"[{}`]|<[a-z][a-z ]*>|^\s*-{3,}\s*$|\*\*|^\s*\*\s",
                         re.IGNORECASE | re.MULTILINE)
MAX_REANSWERS_PER_CALL = 2
# After a cut, send the re-answer note anyway if the turn has not ended by then
# (seen live: a leaking model kept generating and never completed the turn).
REANSWER_GRACE_S = 2.0
REANSWER_NOTE = ("System note: your last reply was cut off because it contained internal text, "
                 "not speech. Briefly answer the caller's last question again, in plain spoken "
                 "words only.")
# Caller speech onset from mic energy (16 kHz 16-bit PCM). Fail-open: noise that
# crosses the threshold only releases the guard early, it never blocks a call.
CALLER_SPEECH_PEAK = 1500          # peak sample level (of 32767) counted as voice
CALLER_SPEECH_MIN_MS = 120         # sustained this long = the caller started speaking
CALLER_SILENCE_RESET_MS = 300      # this much quiet ends the current utterance
INPUT_BYTES_PER_MS = 16_000 * 2 // 1000

UNANSWERED_QUESTION_ERROR = (
    "You asked the caller a question and they have not answered yet. Wait for their "
    "answer; don't assume it. Do not call this function again until the caller speaks.")


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
    def __init__(self, router: ToolRouter, language: Optional[str] = DEFAULT_LANGUAGE,
                 speech_peak: int = CALLER_SPEECH_PEAK,
                 speech_min_ms: int = CALLER_SPEECH_MIN_MS) -> None:
        self.router = router
        self.language = language
        self.resumption_handle: Optional[str] = None
        self._session: Any = None  # current Live session; replaced on reconnect
        self._calls: asyncio.Queue = asyncio.Queue()
        self._cancelled: set[str] = set()
        self._sent_submits: dict[str, str] = {}  # call id -> confirmation, response already sent
        self._notes: list[str] = []
        self._noted: set[str] = set()  # confirmation numbers already noted
        self._model_idle = True
        self.stats: Optional[PumpStats] = None  # set by --debug
        self._pending_tools = 0  # queued or running function calls
        self.tool_responses_sent = 0  # tool responses sent to the model so far
        self._responses_answered = 0  # ...of which the model has replied to
        # Confirmation-number guard
        self._confirmation: Optional[str] = None  # from a successful submit_order
        self._turn_text = ""  # agent transcript of the current turn
        self._turn_scanned = 0  # offset up to which numbers were checked
        self._early: list[str] = []  # numbers spoken before any submit result
        self._corrected: set[str] = set()  # wrong numbers already noted
        # Audio-cut guard: stop forwarding a turn's audio once its transcript leaks.
        self.audio_muted = False
        self._clear_playback = False  # drop audio already queued for the caller
        self.audio_cuts = 0
        self._forced_reanswer: Optional[asyncio.Task] = None
        # Guessed-answer guard: agent asked a question, caller has not spoken since.
        # Released by whichever caller-speech signal comes first: mic energy
        # (immediate), server voice activity, the input transcript (lags), or
        # caller_input() in text mode.
        self._question_pending = False
        self._question_at: Optional[float] = None
        self.guesses_blocked = 0
        self._speech_peak, self._speech_min_ms = speech_peak, speech_min_ms
        self._loud_ms = self._quiet_ms = 0.0
        self._caller_speaking = False
        self.last_caller_signal: dict[str, float] = {}  # signal kind -> loop time
        # Optional hook for guard events (the eval runner logs them).
        self.on_guard_event: Optional[Any] = None

    @property
    def awaiting_reply(self) -> bool:
        """A tool response was sent and the model has produced nothing since
        (no speech, no new function call). Its reply is still to come."""
        return self.tool_responses_sent > self._responses_answered

    @property
    def tools_idle(self) -> bool:
        """True when no function call is queued or running."""
        return self._pending_tools == 0

    @property
    def session_id(self) -> str:
        return self.router.session_id

    def attach(self, session: Any) -> None:
        self._session = session

    def caller_input(self) -> None:
        """The caller said something (text mode, where there is no caller audio)."""
        self._caller_spoke("text")

    def caller_audio(self, chunk: bytes) -> None:
        """Every mic chunk sent upstream: detect the onset of caller speech from
        energy, with no transcript lag."""
        ms = len(chunk) / INPUT_BYTES_PER_MS
        if pcm16_peak(chunk) >= self._speech_peak:
            self._loud_ms += ms
            self._quiet_ms = 0.0
            if not self._caller_speaking and self._loud_ms >= self._speech_min_ms:
                self._caller_speaking = True
                self._caller_spoke("energy")
        else:
            self._quiet_ms += ms
            if self._quiet_ms >= CALLER_SILENCE_RESET_MS:
                self._loud_ms = 0.0
                self._caller_speaking = False

    def _caller_spoke(self, kind: str) -> None:
        now = asyncio.get_running_loop().time()
        previous = self.last_caller_signal.get(kind)
        self.last_caller_signal[kind] = now
        # One event per utterance and signal (transcript chunks arrive ~0.2 s apart).
        if self._question_pending or previous is None or now - previous >= 1.0:
            self._guard_event("caller_speech", signal=kind, released=self._question_pending)
        self._question_pending = False

    def take_clear_request(self) -> bool:
        """True once after an audio cut: queued playback must be dropped."""
        requested, self._clear_playback = self._clear_playback, False
        return requested

    def _guard_event(self, kind: str, **fields: Any) -> None:
        if self.on_guard_event:
            self.on_guard_event(kind, **fields)

    # -- server messages ----------------------------------------------------
    async def on_message(self, msg: Any) -> None:
        if getattr(msg, "session_resumption_update", None):
            upd = msg.session_resumption_update
            if upd.resumable and upd.new_handle:
                self.resumption_handle = upd.new_handle
        if getattr(msg, "tool_call", None) or getattr(getattr(msg, "server_content", None), "model_turn", None):
            self._responses_answered = self.tool_responses_sent  # model output = it moved on
        if getattr(msg, "tool_call", None):
            batch = list(msg.tool_call.function_calls or [])
            if batch:  # one message = one batch = one tool response
                self._pending_tools += len(batch)
                self._calls.put_nowait(batch)
        if getattr(msg, "tool_call_cancellation", None):
            ids = msg.tool_call_cancellation.ids or []
            logger.info("tool calls cancelled by server: %s", ids)
            self._cancelled.update(ids)
            for call_id in ids:
                # Response already sent but the server discarded it: re-tell via note.
                if call_id in self._sent_submits:
                    self._queue_note(self._sent_submits.pop(call_id))
            await self._maybe_flush_notes()
        activity = getattr(msg, "voice_activity", None)
        if activity is not None and "START" in str(getattr(activity, "voice_activity_type", "")):
            self._caller_spoke("vad")
        content = getattr(msg, "server_content", None)
        if content:
            for attr, who in (("input_transcription", "caller"), ("output_transcription", "agent")):
                tr = getattr(content, attr, None)
                if tr and getattr(tr, "text", None):
                    logger.info("[%s] %s", who, tr.text)
                    if who == "caller":
                        self._caller_spoke("transcript")
                    else:
                        self._turn_text += tr.text
                        self._check_spoken_numbers(final=False)
                        if "?" in tr.text:
                            if not self._question_pending:
                                self._question_at = asyncio.get_running_loop().time()
                                self._guard_event("question")
                            self._question_pending = True
                        self._check_leak()
            if content.model_turn:
                self._model_idle = False
            if content.turn_complete or content.interrupted:
                self._check_spoken_numbers(final=True)
                if self._early and not self._confirmation:
                    self._queue_text("no-number", NO_NUMBER_NOTE)
                    self._early.clear()
                self._turn_text, self._turn_scanned = "", 0
                self.audio_muted = False  # the next turn (e.g. the re-answer) is heard again
                if self._forced_reanswer:
                    self._forced_reanswer.cancel()  # the turn ended normally
                    self._forced_reanswer = None
                self._model_idle = True
                await self._maybe_flush_notes()

    # -- tool execution (one at a time, in order) ---------------------------
    async def run_tools(self) -> None:
        while True:
            batch = await self._calls.get()
            try:
                await self._execute_batch(batch)
            except Exception:  # never let one batch kill the worker
                logger.exception("tool batch %s failed", [getattr(fc, "name", "?") for fc in batch])
            finally:
                self._pending_tools -= len(batch)
                self._calls.task_done()

    async def _execute_batch(self, batch: list) -> None:
        """Run one toolCall message's calls in order, then answer them all in ONE
        tool response. Answering call by call lets the model resume after the
        first answer and re-issue the calls still pending (duplicate adds)."""
        responses, submits = [], {}
        for fc in batch:
            if fc.id in self._cancelled:
                logger.info("skipping cancelled call %s (%s)", fc.id, fc.name)
                continue
            args = dict(fc.args or {})
            if fc.name in ("update_order", "submit_order") and self._question_pending:
                self.guesses_blocked += 1
                logger.info("blocked %s(%s): question not answered yet", fc.name, args)
                now = asyncio.get_running_loop().time()
                self._guard_event(
                    "guess_blocked", name=fc.name, args=args,
                    since_question_s=round(now - self._question_at, 2) if self._question_at else None,
                    since_signal_s={k: round(now - t, 2) for k, t in self.last_caller_signal.items()})
                responses.append((fc, error_result(INVALID_ARGUMENTS, UNANSWERED_QUESTION_ERROR)))
                continue
            result = await self.router.call(fc.name, args)
            outcome = result["error"]["code"] if "error" in result else "ok"
            logger.info("tool %s(%s) -> %s", fc.name, args, outcome)
            if fc.name == "submit_order" and is_successful_submit(result):
                submits[fc.id] = result["confirmation_number"]
            responses.append((fc, result))
        live = []
        for fc, result in responses:
            if fc.id in self._cancelled:  # cancelled while the batch ran
                logger.info("not responding to cancelled call %s (%s)", fc.id, fc.name)
                if fc.id in submits:
                    self._set_confirmation(submits[fc.id])
                    self._queue_note(submits.pop(fc.id))
                continue
            live.append({"id": fc.id, "name": fc.name, "response": result})
        if live:
            await self._session.send_tool_response(function_responses=live)
            self.tool_responses_sent += 1
        for number in submits.values():
            self._set_confirmation(number)
        self._sent_submits.update(submits)
        await self._maybe_flush_notes()

    # -- audio-cut guard ------------------------------------------------------
    def _check_leak(self) -> None:
        if self.audio_muted or not LEAK_MARKER.search(self._turn_text):
            return
        self.audio_muted = True
        self._clear_playback = True
        self.audio_cuts += 1
        logger.warning("leak in agent transcript: audio cut for the rest of the turn")
        self._guard_event("audio_cut", text=self._turn_text[-200:])
        if self.audio_cuts <= MAX_REANSWERS_PER_CALL:
            self._queue_text(f"reanswer-{self.audio_cuts}", REANSWER_NOTE)
            self._forced_reanswer = asyncio.get_running_loop().create_task(
                self._reanswer_if_turn_hangs(self.audio_cuts))

    async def _reanswer_if_turn_hangs(self, cut: int) -> None:
        """The muted turn has not ended after the grace period: send the
        re-answer note now (new input interrupts the model's generation)."""
        await asyncio.sleep(REANSWER_GRACE_S)
        if self.audio_muted and self.audio_cuts == cut:
            logger.warning("turn still running %.1fs after an audio cut: sending the re-answer note now",
                           REANSWER_GRACE_S)
            self._guard_event("reanswer_forced")
            await self._maybe_flush_notes(force=True)

    # -- confirmation-number guard -------------------------------------------
    def _set_confirmation(self, number: str) -> None:
        self._confirmation = number
        for digits in self._early:  # spoken before the result existed
            self._handle_spoken(digits)
        self._early.clear()

    def _check_spoken_numbers(self, final: bool) -> None:
        """Check numbers in the agent's transcript. Mid-turn, only numbers
        followed by more text are taken (the next chunk may add digits)."""
        for digits, end in spoken_confirmation_numbers(self._turn_text):
            if end <= self._turn_scanned or (not final and end >= len(self._turn_text)):
                continue
            self._turn_scanned = end
            self._handle_spoken(digits)

    def _handle_spoken(self, digits: str) -> None:
        if self._confirmation is None:
            self._early.append(digits)
            return
        real = re.sub(r"\D", "", self._confirmation)
        if digits != real and digits not in self._corrected:
            self._corrected.add(digits)
            logger.info("agent said wrong confirmation number %s (real %s)", digits, self._confirmation)
            self._queue_text(f"wrong-{digits}", wrong_number_note(self._confirmation))

    # -- system notes -------------------------------------------------------
    def _queue_note(self, confirmation_number: str) -> None:
        self._queue_text(confirmation_number, submit_note(confirmation_number))

    def _queue_text(self, key: str, note: str) -> None:
        """Queue a System note once per key; sent when the model is idle."""
        if key not in self._noted:
            self._noted.add(key)
            self._notes.append(note)

    async def _maybe_flush_notes(self, force: bool = False) -> None:
        if (not self._model_idle and not force) or self._session is None:
            return
        while self._notes:
            # Realtime text, not send_client_content: the SDK warns against
            # interleaving client_content with realtime audio input.
            note = self._notes.pop(0)
            logger.info("sending %r", note)
            await self._session.send_realtime_input(text=note)

    # -- connection loop ----------------------------------------------------
    async def run(
        self, client: Any, model: str, audio: AudioPort, extra_config: Optional[dict] = None
    ) -> None:
        """Bridge until the call ends, resuming the Live session on go_away/drops."""
        worker = asyncio.create_task(self.run_tools())
        failures = 0
        try:
            while True:
                try:
                    config = build_live_config(self.resumption_handle, extra_config, self.language)
                    async with client.aio.live.connect(model=model, config=config) as session:
                        self.attach(session)
                        failures = 0
                        if await self._pump(session, audio):
                            return  # caller hung up
                except Exception as exc:
                    rejected = handshake_rejection(exc)
                    if rejected:
                        status, body = rejected
                        raise LiveHandshakeError(
                            f"Live API rejected the handshake: HTTP {status} "
                            f"(model={model}); response body: {body}"
                        ) from exc
                    failures += 1
                    logger.exception("Live session dropped (%d/%d)", failures, MAX_RECONNECTS)
                    if failures >= MAX_RECONNECTS:
                        raise
                    await asyncio.sleep(min(0.5 * failures, 2.0))
                if self.resumption_handle:
                    logger.info("reconnecting call %s with resumption handle", self.session_id)
                else:
                    logger.info("reconnecting call %s (no resumption handle yet)", self.session_id)
        finally:
            await _cancel_and_wait(worker)

    async def _pump(self, session: Any, audio: AudioPort) -> bool:
        """Run one Live session. True = call ended; False = reconnect needed."""

        async def upstream() -> None:
            while (chunk := await audio.read()) is not None:
                if self.stats:
                    self.stats.mic(chunk)
                self.caller_audio(chunk)
                await session.send_realtime_input(
                    audio={"data": chunk, "mime_type": INPUT_AUDIO_MIME}
                )

        async def downstream() -> None:
            while True:  # receive() stops after each completed turn
                async for msg in session.receive():
                    if self.stats:
                        self.stats.message(msg)
                    content = msg.server_content
                    if content and content.interrupted:
                        await audio.clear()
                    muted = self.audio_muted
                    # Transcript first: a leak marker in this message stops its audio too.
                    await self.on_message(msg)
                    muted = muted or self.audio_muted
                    if self.take_clear_request():
                        await audio.clear()
                    if content and content.model_turn:
                        for part in content.model_turn.parts or []:
                            if part.inline_data and part.inline_data.data:
                                if self.stats:
                                    self.stats.model_audio(part.inline_data.data)
                                if not muted:
                                    await audio.play(part.inline_data.data)
                    if msg.go_away:
                        return

        up = asyncio.create_task(upstream(), name="live-upstream")
        down = asyncio.create_task(downstream(), name="live-downstream")
        try:
            done, _ = await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            # Also runs on Ctrl+C (CancelledError): never leave a task unawaited.
            await _cancel_and_wait(up, down)
        for task in done:
            if not task.cancelled():
                task.result()  # re-raise connection errors -> reconnect
        return up in done


async def _cancel_and_wait(*tasks: asyncio.Task) -> None:
    """Cancel tasks and await them, swallowing their outcome (retrieved, not lost)."""
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
