# 0975-gateway: [BUG] Instances on one host share the GPU: residency fit and eviction must be host-wide

> Package: abstractgateway (instances, model residency, setup guide); abstractcore (fit check, `ModelDoesNotFitError`)
> Type: bug
> Status: planned (not implemented)
> Created: 2026-10-29
> Priority: P1
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
- A text route can cap its context (the KV allocation), and the recommendation picks a context that fits beside the
  other instance.
- A test with two instances on a fake 16 GB card: the second load counts the first instance's model.
