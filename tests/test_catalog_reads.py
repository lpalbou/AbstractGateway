"""Concurrent browsers share work; cancellation and failures never poison later reads."""
import asyncio

import pytest

from abstractgateway.catalog_reads import catalog_read


def test_shared_read_survives_disconnected_waiter_and_next_request_is_fresh():
    async def run():
        gate = asyncio.Event()
        calls = []

        async def read():
            calls.append(True)
            await gate.wait()
            return len(calls)

        waiters = [asyncio.create_task(catalog_read(("catalog",), read)) for _ in range(20)]
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        waiters[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiters[0]
        gate.set()
        assert await asyncio.gather(*waiters[1:]) == [1] * 19
        assert await catalog_read(("catalog",), read) == 2
        assert len(calls) == 2

    asyncio.run(run())


def test_distinct_parameters_and_failed_reads_do_not_share_completed_state():
    async def run():
        calls = []

        async def read():
            calls.append(True)
            await asyncio.sleep(0)
            raise RuntimeError("offline")

        results = await asyncio.gather(*(catalog_read(("catalog", q), read) for q in ["a", "a", "b"]), return_exceptions=True)
        assert len(calls) == 2 and all(isinstance(r, RuntimeError) for r in results)
        with pytest.raises(RuntimeError, match="offline"):
            await catalog_read(("catalog", "a"), read)
        assert len(calls) == 3

    asyncio.run(run())
