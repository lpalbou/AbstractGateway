# Proposed: Externalize the console into static assets

## Metadata
- Created: 2026-07-05
- Status: Proposed
- Completed: N/A
- Roadmap phase: 2 (velocity and reviewability)
- Effort: S

## ADR status
- Governing ADRs: None.

## Context

Gateway ships a built-in control-plane console at `/console`.

## Current code reality (verified)

- `src/abstractgateway/console.py` is 3,959 lines, essentially a full HTML/CSS/JS
  web application embedded inside a single Python string literal.
- This means the console cannot be linted, type-checked, bundled, or diffed as
  frontend code, and it inflates a Python module that reviewers must scroll past.

## Problem or opportunity

Frontend-in-a-string is unmaintainable and untestable, and it obscures Python
review. It is also duplicated conceptually with the thin-client apps.

## What we might want to do

1. Move the console HTML/CSS/JS into packaged static files served by a small
   route; keep only the serving logic in Python.
2. Optionally build it with the same toolchain as the other UIs so it can share
   the future client SDK (0071) instead of hand-rolling calls.

## Dependency boundary

Gateway-only (packaging + a static route).

## Why

Restores maintainability and shrinks the Python surface a reviewer must read; a
precondition for the console dogfooding the client SDK.

## Promotion criteria

Promote with or after 0066 (router decomposition).

## Validation to require on promotion

- `/console` serves identical functionality from static assets; a smoke test
  loads the console and performs a sign-in + basic action.
