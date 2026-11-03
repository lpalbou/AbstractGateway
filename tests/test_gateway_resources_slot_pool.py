"""Resources table: the model's slot pool (core ADR 0011; OVH 8080 pass four R1). The row carries
slots / slots_max / active / queued / slot_refusal_reason and the console says it in words.
MUTANT: row_v1 without the pool keys, or no console line -> RED."""
from __future__ import annotations


def test_row_v1_carries_the_slot_pool():
    import abstractgateway.routes.gateway as g

    row = g._normalize_model_residency_row_v1({
        "provider": "huggingface", "model": "m", "resident": True, "slots": 1, "slots_max": 4, "active": 1,
        "queued": 3, "slot_refusal_reason": "another slot of m needs 8.3 GiB of GPU memory, 1.0 GiB free"})
    assert (row["slots"], row["slots_max"], row["active"], row["queued"]) == (1, 4, 1, 3)
    assert "needs 8.3 GiB" in row["slot_refusal_reason"]


def test_the_resources_table_says_how_many_wait_and_why():
    from abstractgateway.console import gateway_console_html

    html = gateway_console_html()
    assert "busy (up to ${row.slots_max})" in html and " waiting` : " in html
    assert "Why not more slots: ${row.slot_refusal_reason}" in html and "pool.dataset.slotPool" in html
