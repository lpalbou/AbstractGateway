### Changed

- **Resources: each resident model's row shows its slot pool.** The row says how many requests run and wait on its
  one copy, for example "1 of 1 slot busy (up to 4) · 3 requests waiting", in the warning colour while requests
  wait, followed by the reason no other slot is built ("another slot ... needs 8.3 GiB of GPU memory for its
  135,680-token context, 1.0 GiB free"). `model_residency_row_v1` carries `slots`, `slots_max`, `active`, `queued`
  and `slot_refusal_reason` (AbstractCore ADR 0011), read from AbstractRuntime's model listing.
