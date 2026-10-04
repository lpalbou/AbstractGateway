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
- Clean-up runs whether the body ran, stopped half-way, or never started:
  the engine's iterator is closed on the feeder thread (so the runtime
  records the synthesis as cancelled) and the permit returns when the engine
  thread is done.
"""

from __future__ import annotations

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
) -> StreamingResponse:
    """Admit, start the engine setup off the loop, and return the streaming response.

    ``setup()`` returns the engine's event iterator (blocking; runs on a worker
    thread). ``on_idle_timeout()`` (blocking; worker thread) names and cancels
    the stuck synthesis and returns the sentence for the terminal error line.
    ``setup_error``/``busy_error`` build the HTTP errors raised before the
    response starts.
    """

    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(sem.acquire(), timeout=1.0)
    except asyncio.TimeoutError:
        raise busy_error(max_concurrency) from None
    permit = _Permit(sem, loop)
    setup_task: "asyncio.Future[Any]" = asyncio.ensure_future(asyncio.to_thread(setup))
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

    state: Dict[str, Any] = {"feeder": None, "stop": threading.Event(), "finalized": False}

    def _finalize() -> None:
        if state["finalized"]:
            return
        state["finalized"] = True
        state["stop"].set()
        if state["feeder"] is not None:
            return  # the feeder closes the engine iterator and returns the permit when it ends
        if not setup_task.done():
            setup_task.add_done_callback(lambda t: _after_abandoned_setup(t, permit))
            return
        if not setup_task.cancelled() and setup_task.exception() is None:
            _close_quietly(setup_task.result())
        permit.release()

    def _idle_line(detail: str) -> bytes:
        return encode({"type": "error", "ok": False, "request_id": request_id, "error": detail, "watchdog_timeout": True})

    async def _body() -> AsyncIterator[bytes]:
        try:
            if not setup_task.done():
                yield encode({"type": "queued", "ok": True, "request_id": request_id, "message": QUEUED_MESSAGE})
                try:
                    if timeout_s > 0:
                        await asyncio.wait_for(asyncio.shield(setup_task), timeout=timeout_s)
                    else:
                        await asyncio.shield(setup_task)
                except asyncio.TimeoutError:
                    yield _idle_line(await asyncio.to_thread(on_idle_timeout))
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - the setup's failure is the stream's terminal line
                    yield encode({"type": "error", "ok": False, "request_id": request_id, "error": f"TTS stream setup failed: {exc}"})
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
            while True:
                try:
                    if timeout_s > 0:
                        kind, value = await asyncio.wait_for(queue.get(), timeout=timeout_s)
                    else:
                        kind, value = await queue.get()
                except asyncio.TimeoutError:
                    stop.set()
                    yield _idle_line(await asyncio.to_thread(on_idle_timeout))
                    return
                if kind == "end":
                    return
                if kind == "error":
                    yield encode({"type": "error", "ok": False, "request_id": request_id, "error": str(value)})
                    return
                credits.release()
                if isinstance(value, dict):
                    value.setdefault("request_id", request_id)
                    yield encode(value)
                else:
                    yield encode({"type": "event", "request_id": request_id, "value": str(value)})
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
