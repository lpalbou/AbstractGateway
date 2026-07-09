# Proposed: Secret-at-rest encryption and ledger redaction audit

## Metadata
- Created: 2026-07-05
- Status: Proposed
- Completed: N/A
- Roadmap phase: 1 (security posture)
- Effort: M

## ADR status
- Governing ADRs: None.

## Context

Gateway stores provider API keys and records LLM payloads in the ledger. Both are
secret-adjacent and need explicit handling.

## Current code reality (investigated)

- Provider endpoint-profile keys are written as plaintext JSON, protected only by
  `chmod 600` (`provider_endpoint_profiles.py` ~30, ~205-227). `docs/security.md`
  (~142-143) explicitly defers "a stronger encrypted vault."
- Positive: `public_dict()` excludes the key and returns only a fingerprint
  (~49-66); the secret form `private_resolution()` is used only on the internal
  runtime path (`bundle_host.py:88`, `routes/gateway.py` ~21287) — no route was
  found that returns the raw key, so the "keys stay server-side" API claim holds.
- Suspected (needs confirmation in AbstractRuntime): ledgers persist LLM call
  payloads; provider kwargs include `api_key`/`base_url`. Keys are passed as
  client kwargs (not prompt text), so they likely stay out of ledger content, but
  this must be verified in Runtime's `llm_call` ledger serialization.

## Problem or opportunity

A disk/backup compromise (or an in-process RCE per 0062) yields all provider keys
in cleartext; and if endpoints/keys leak into ledgers, anyone who can read a run
ledger reads secrets.

## What we might want to do

1. Encrypt provider keys at rest (OS keyring/KMS or an app-level key), keeping the
   fingerprint-only public surface.
2. Verify and, if needed, enforce redaction of provider `api_key`/`base_url` from
   ledger records in the Runtime serialization path; add a test asserting no
   secret appears in a recorded `llm_call` ledger.

## Dependency boundary

Encryption is Gateway-local; ledger redaction verification/fix is in
AbstractRuntime.

## Why

Protects secret confidentiality against disk/backup compromise and closes a
plausible ledger-leak path.

## Promotion criteria

Promote with hosted multi-user hardening (0062/0064), especially before untrusted
tenants.

## Validation to require on promotion

- Keys are unreadable at rest without the encryption key; a test proves no
  provider secret is present in a recorded ledger for an LLM call.
