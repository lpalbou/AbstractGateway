# 0975-gateway: [PROPOSED] Instances on one host share the GPU: residency fit and eviction must be host-wide

> Package: abstractgateway (instances, model residency, setup guide); abstractcore (fit check, `ModelDoesNotFitError`)
> Type: bug
> Status: proposed (not planned): several instances on one machine is not a product goal (operator ruling, 2026-10-29)
> Created: 2026-10-29
> Priority: P3
> Labels: instances, gpu, residency, setup-guide
> Root link: OVH release-test server, 2026-10-29 (instances `default` on 8080 and `fresh` on 8443, one Quadro RTX 5000 16 GB)

## Summary

Two gateway instances on one host each keep their own model residency: each fit check and each eviction sees only
its own models. On the OVH server, the 8443 instance loaded its Text default (Qwen3.5 9B Q4_K_M, 7,270 MiB). The 8080
instance's default text model could then no longer reload beside its own embedder (5,128 MiB). Its chat answered the
refusal sentence ("needs 7.5 GB but only 2.6 GB is free").

A 4B text default does not solve it either. llama.cpp loads it at the model's 65,536-token context, so the KV cache
puts a Qwen3.5 4B Q4_K_M at about 7.1 GiB resident (measured 7,120 MiB after one chat).

## Done when

- The residency fit check and eviction are HOST-WIDE across instances. Either a shared residency ledger in the
  instances registry's folder (each instance records what it holds, with its pid, and the fit check counts the other
  instances' models), or one host-level model host that every instance asks. An instance never evicts another
  instance's model silently; a refusal names the other instance and what it holds.
- The setup guide's recommendation on a host with another instance counts the memory the other instance holds.
- No context cap per route and no silent truncation (operator ruling 2026-10-29, framework ADR 0026): the KV is reserved
  at the model's chosen context, shown and editable on the Models row; a load that does not fit gets the readable
  refusal, a request longer than the loaded context fails with its sentence (AbstractCore ADR 0010).
- A test with two instances on a fake 16 GB card: the second load counts the first instance's model.

## Ruling (2026-10-29)

The operator ruled that several gateway instances on one machine are not a product goal. The test server runs ONE
gateway. For a fresh onboarding try, 8080's data dir is reset on request; no second instance runs. The OVH 8443
instance "fresh" is stopped, and its data dir is kept on disk. This item stays as a record, in case the goal changes.
