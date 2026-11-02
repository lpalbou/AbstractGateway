"""Streamed speech (`POST /runs/{id}/voice/tts/stream`) that never touches the event loop's time.

R13.1 (2026-10-04): the event loop must stay responsive while text-to-speech
runs next to a generating model. The rules this module implements:

- Admission is bounded. Each stream holds one permit of the gateway's voice
  synthesis semaphore (``ABSTRACTGATEWAY_VOICE_MAX_CONCURRENCY``) from its
  engine setup until its engine thread has finished — including after the
  client left — so a wedged engine can never accumulate threads past the
  bound. A request past the bound waits one second, then gets a 503 with a
  sentence (back-pressure, not a pile-up).
- Engine work is strictly off the loop: the setup (``stream_voice``, which may
  wait on the engine's lock while the model generates) runs on a worker
  thread; events are pulled by ONE feeder thread per admitted stream and
  handed to the loop through an asyncio queue. The response body is an async
  generator: the loop only awaits the queue (it yields between every chunk
  and holds no worker thread while it waits). The feeder blocks when the
  client reads slower than the engine speaks (a bounded buffer).
- A setup that does not return within ``QUEUE_NOTICE_S`` starts the response
  anyway with a ``{"type": "queued", "message": …}`` line, so the client can
  say why speech has not started instead of showing a silent spinner.
- A gap longer than the TTS watchdog ends the stream with a terminal error
  line (never a silent forever-stream).
- The line is never silent for long (2026-11-01, watch "Voice unavailable"):
  while the engine has sent nothing for ``KEEPALIVE_S``, the body writes a
  ``{"type": "keepalive"}`` line, so a client's idle timer measures the link
  and not the synthesis. Keep-alives do not feed the watchdog: it still fires
  only after ``timeout_s`` of ENGINE silence.
- Clean-up runs whether the body ran, stopped half-way, or never started:
  the engine's iterator is closed on the feeder thread (so the runtime
  records the synthesis as cancelled) and the permit returns when the engine
  thread is done.
- ``stats`` (optional, the caller's dict) is filled in place with what the
  stream did — lines, audio lines/bytes, keep-alives and how it ended — so
  the route can hand it to the audit log, which is written after the body.
"""

from __future__ import annotations

from .work_pools import off_loop
import asyncio
import json
import threading
from typing import Any, AsyncIterator, Callable, Dict, Optional

from starlette.responses import StreamingResponse

# Seconds the engine setup may take before the response starts with a
# "queued" line (TTFA is unaffected when the engine is free: the setup of a
# free engine returns in milliseconds).
QUEUE_NOTICE_S = 1.0
# Events buffered between the engine and a slow client before the engine
# thread waits (back-pressure).
BUFFER_EVENTS = 16
# Seconds without a line before a keep-alive line is written (<= 0 disables).
KEEPALIVE_S = 10.0
QUEUED_MESSAGE = (
    "Waiting for the voice engine: it is busy (another reply is being read or the model is using the machine). "
    "Speech starts as soon as it is free."
)


def _jsonl(event: Dict[str, Any]) -> bytes:
    return (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


class _Permit:
    """One admission permit; released exactly once, always on the loop thread."""

    def __init__(self, sem: asyncio.Semaphore, loop: asyncio.AbstractEventLoop) -> None:
        self._sem = sem
        self._loop = loop
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            self._sem.release()
        except Exception:
            pass

    def release_threadsafe(self) -> None:
        try:
            self._loop.call_soon_threadsafe(self.release)
        except RuntimeError:  # the loop is closed: nothing is waiting for the permit any more
            self._released = True


class _PermitStreamingResponse(StreamingResponse):
    """A StreamingResponse that runs its clean-up even when the body never started."""

    def __init__(self, content: AsyncIterator[bytes], *, finalize: Callable[[], None], **kwargs: Any) -> None:
        super().__init__(content, **kwargs)
        self._finalize = finalize

    async def __call__(self, scope, receive, send):  # noqa: ANN001 - ASGI signature
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._finalize()


def _close_quietly(events: Any) -> None:
    close = getattr(events, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def new_stream_stats() -> Dict[str, Any]:
    """The counters ``open_voice_stream`` fills (a fresh dict; pass it as ``stats``)."""
    return {"lines": 0, "audio_lines": 0, "audio_bytes": 0, "keepalives": 0, "ended": None}


async def open_voice_stream(
    *,
    setup: Callable[[], Any],
    request_id: str,
    timeout_s: float,
    sem: asyncio.Semaphore,
    max_concurrency: int,
    on_idle_timeout: Callable[[], str],
    setup_error: Callable[[BaseException], Exception],
    busy_error: Callable[[int], Exception],
    encode: Callable[[Dict[str, Any]], bytes] = _jsonl,
    keepalive_s: Optional[float] = None,
    stats: Optional[Dict[str, Any]] = None,
) -> StreamingResponse:
    """Admit, start the engine setup off the loop, and return the streaming response.

    ``setup()`` returns the engine's event iterator (blocking; runs on a worker
    thread). ``on_idle_timeout()`` (blocking; worker thread) names and cancels
    the stuck synthesis and returns the sentence for the terminal error line.
    ``setup_error``/``busy_error`` build the HTTP errors raised before the
    response starts. ``keepalive_s`` overrides ``KEEPALIVE_S``; ``stats`` is
    filled in place (see ``new_stream_stats``) and is complete once the
    response has finished, including when the client left.
    """

    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(sem.acquire(), timeout=1.0)
    except asyncio.TimeoutError:
        raise busy_error(max_concurrency) from None
    permit = _Permit(sem, loop)
    setup_task: "asyncio.Future[Any]" = asyncio.ensure_future(off_loop("watchers", setup))
    try:
        await asyncio.wait({setup_task}, timeout=QUEUE_NOTICE_S)
    except BaseException:
        setup_task.add_done_callback(lambda t: _after_abandoned_setup(t, permit))
        raise
    if setup_task.done():
        exc = setup_task.exception()
        if exc is not None:
            permit.release()
            raise setup_error(exc)

    keepalive = KEEPALIVE_S if keepalive_s is None else float(keepalive_s)
    counters: Dict[str, Any] = stats if stats is not None else new_stream_stats()
    for key, value in new_stream_stats().items():
        counters.setdefault(key, value)
    state: Dict[str, Any] = {"feeder": None, "stop": threading.Event(), "finalized": False}

    def _finalize() -> None:
        if state["finalized"]:
            return
        state["finalized"] = True
        state["stop"].set()
        if counters.get("ended") is None:
            counters["ended"] = "disconnected"  # the body was stopped before a terminal line went out
        if state["feeder"] is not None:
            return  # the feeder closes the engine iterator and returns the permit when it ends
        if not setup_task.done():
            setup_task.add_done_callback(lambda t: _after_abandoned_setup(t, permit))
            return
        if not setup_task.cancelled() and setup_task.exception() is None:
            _close_quietly(setup_task.result())
        permit.release()

    def _emit(event: Dict[str, Any]) -> bytes:
        """Encode one line and count it (audio bytes are the WAV payload, before base64)."""
        counters["lines"] += 1
        kind = str(event.get("type") or "")
        if kind == "audio":
            counters["audio_lines"] += 1
            audio = event.get("audio")
            size = len(audio) if isinstance(audio, (bytes, bytearray)) else event.get("size_bytes")
            try:
                counters["audio_bytes"] += int(size or 0)
            except Exception:
                pass
        elif kind == "keepalive":
            counters["keepalives"] += 1
        elif kind in ("done", "error", "cancelled"):
            counters["ended"] = "watchdog" if event.get("watchdog_timeout") else kind
        return encode(event)

    def _idle_event(detail: str) -> Dict[str, Any]:
        return {"type": "error", "ok": False, "request_id": request_id, "error": detail, "watchdog_timeout": True}

    def _keepalive_event() -> Dict[str, Any]:
        return {"type": "keepalive", "ok": True, "request_id": request_id}

    def _deadline() -> Optional[float]:
        return loop.time() + timeout_s if timeout_s > 0 else None

    def _wait_s(deadline: Optional[float]) -> Optional[float]:
        """How long to wait for the next event: until the next keep-alive or the watchdog, whichever is first."""
        remaining = None if deadline is None else max(0.0, deadline - loop.time())
        if keepalive > 0:
            return keepalive if remaining is None or remaining > keepalive else remaining
        return remaining

    def _expired(deadline: Optional[float]) -> bool:
        return deadline is not None and loop.time() >= deadline - 1e-6

    async def _body() -> AsyncIterator[bytes]:
        try:
            if not setup_task.done():
                yield _emit({"type": "queued", "ok": True, "request_id": request_id, "message": QUEUED_MESSAGE})
                deadline = _deadline()
                while not setup_task.done():
                    try:
                        await asyncio.wait_for(asyncio.shield(setup_task), timeout=_wait_s(deadline))
                    except asyncio.TimeoutError:
                        if _expired(deadline):
                            yield _emit(_idle_event(await off_loop("light", on_idle_timeout)))
                            return
                        yield _emit(_keepalive_event())
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        break  # the setup's failure is read from the task below
                if setup_task.cancelled():
                    yield _emit({"type": "error", "ok": False, "request_id": request_id, "error": "TTS stream setup was cancelled."})
                    return
                exc = setup_task.exception()
                if exc is not None:
                    yield _emit({"type": "error", "ok": False, "request_id": request_id, "error": f"TTS stream setup failed: {exc}"})
                    return
            events = setup_task.result()

            queue: "asyncio.Queue[tuple[str, Any]]" = asyncio.Queue()
            credits = threading.Semaphore(BUFFER_EVENTS)
            stop: threading.Event = state["stop"]

            def _post(item: "tuple[str, Any]") -> None:
                try:
                    loop.call_soon_threadsafe(queue.put_nowait, item)
                except RuntimeError:
                    stop.set()

            def _feed() -> None:
                try:
                    for event in events:
                        while not credits.acquire(timeout=0.25):
                            if stop.is_set():
                                return
                        if stop.is_set():
                            return
                        _post(("event", event))
                    _post(("end", None))
                except Exception as exc:  # noqa: BLE001 - surfaced as the stream's terminal line
                    _post(("error", exc))
                finally:
                    _close_quietly(events)
                    permit.release_threadsafe()

            feeder = threading.Thread(target=_feed, name=f"tts-stream-feed-{request_id[:8]}", daemon=True)
            state["feeder"] = feeder
            feeder.start()
            deadline = _deadline()  # the watchdog counts ENGINE silence; keep-alives never reset it
            while True:
                try:
                    kind, value = await asyncio.wait_for(queue.get(), timeout=_wait_s(deadline))
                except asyncio.TimeoutError:
                    if _expired(deadline):
                        stop.set()
                        yield _emit(_idle_event(await off_loop("light", on_idle_timeout)))
                        return
                    yield _emit(_keepalive_event())
                    continue
                deadline = _deadline()
                if kind == "end":
                    if counters.get("ended") is None:
                        counters["ended"] = "ended"  # the engine ended without a terminal line
                    return
                if kind == "error":
                    yield _emit({"type": "error", "ok": False, "request_id": request_id, "error": str(value)})
                    return
                credits.release()
                if isinstance(value, dict):
                    value.setdefault("request_id", request_id)
                    yield _emit(value)
                else:
                    yield _emit({"type": "event", "request_id": request_id, "value": str(value)})
        finally:
            _finalize()

    return _PermitStreamingResponse(
        _body(),
        finalize=_finalize,
        media_type="application/x-ndjson",
        headers={"X-Content-Type-Options": "nosniff"},
    )


def _after_abandoned_setup(task: "asyncio.Future[Any]", permit: _Permit) -> None:
    """The client left while the engine setup was still waiting: when it
    returns, close its (unstarted) iterator and give the permit back."""
    try:
        if not task.cancelled() and task.exception() is None:
            _close_quietly(task.result())
    except Exception:
        pass
    permit.release()
