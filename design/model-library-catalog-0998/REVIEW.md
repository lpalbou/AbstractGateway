# Models library and catalogue — Gateway 0998, Core 0873 / 0874

Completed and locally deployed 2026-10-30. Gateway `334dd3bb`; Core source
`62581e63` / `8afe7771`, deployed as `e42d5146` / `a2fd2b10`.

## User-facing result

Downloaded only is the single quick library control. Duplicate Browse/Downloaded
tabs and repeated Downloaded summary text are removed; the existing URL state,
keyboard operation, reload and restoration of browse filters remain. Host controls
and shared reserved chrome remain intact. Recommended replaces Starter with an
outlined pill, star, primary text and a useful hardware/engine explanation.
Measured composited label contrast: 13.14:1 dark and 15.09:1 light.

All 23 actual LM Studio artifacts now have catalogue identities and are reachable
under model cards after disclosure. There are zero LM Studio rows in the separate
non-catalogue section. The catalogue grows from 96 to 102 curated models; installed
precisions keep exact engine IDs, sizes and individual actions. The captured
Downloaded view contains 29 catalogue models (previously 19).

## Identity and file correctness

An exact registry resolver handles aliases, provider paths, format/precision
suffixes and UD quant labels without using the permissive base-model partial
match ladder. Authoritative LM Studio paths disambiguate instruction/QAT names.
Ambiguous or unknown derivatives remain unresolved; the HauhauCS fine-tune gets
a distinct model identity. Canonical grouping never establishes file equivalence:
8-bit and 4-bit explicit variants cannot cover or satisfy one another.

Catalogue assembly joins exact installed variants even when the curated seed
lacks that engine or precision, preserving presence, quant and actual size. LM
Studio/Ollama engine inventories establish presence; HF cache directories retain
checkpoint probes, so partial caches do not become installed merely by existing.
No model download, inference, deletion, default change or gate approval was used.

## Metadata and primary sources

Five capability entries: Spark X2.6 4B, LFM3 1.7B, Bonsai 27B, HauhauCS Aggressive
and all-MiniLM-L6-v2. Four format entries: Spark sentence/role wrappers and GLM-style
tools; LFM3 im_start/end and Qwen3-Coder tools; Bonsai's parent Qwen format; MiniLM's
non-chat encoder identity. Actual tool payloads are checked through existing parsers.
Six download rows: Spark, LFM3, Bonsai, HauhauCS, Gemma E2B and Gemma 12B. Existing
Liquid/Qwen/Gemma/embedding entries are reused, with explicit Gemma conversion/QAT
aliases. Gemma E2B's total count uses 5,123,178,051 tensor parameters, distinct from
its effective-size name. Example generation budgets are not promoted to hard caps.
MTP claims are not copied onto derived models from config flags alone.

Primary model cards, configs and templates accessed 2026-10-30:

- [Spark X2.6](https://huggingface.co/XHToken/Spark-X2.6-4B)
- [LFM3 1.7B](https://huggingface.co/LiquidAI/LFM3-1.7B)
- [Prism Bonsai](https://huggingface.co/prism-ml/Bonsai-27B-gguf), [unpacked config](https://huggingface.co/prism-ml/Bonsai-27B-unpacked/blob/main/config.json), [LM Studio conversion](https://huggingface.co/lmstudio-community/Bonsai-27B-GGUF)
- [HauhauCS fine-tune](https://huggingface.co/HauhauCS/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive)
- [MiniLM](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2)
- [Gemma E2B](https://huggingface.co/google/gemma-4-E2B-it), [Gemma 12B](https://huggingface.co/google/gemma-4-12B-it)

New remote artifacts use existing size-unknown semantics unless measured by the
engine or Hub. A non-public Bonsai MLX download candidate was omitted from the
seed; its installed LM Studio build is represented by its actual local ID.

## Validation and deployment

165 source Core checks passed, followed by 35 identity/checkpoint-probe checks;
the actual Core deployment checkout passed all 132 focused registry, catalogue,
materializer and schema tests. Gateway: 36 tests and 214 browser assertions pass.
Coverage includes all 23 observed engine identifiers, unknown derivative negatives,
precision separation, parser formats, schemas, version disclosure, exact actions,
recommendation visibility, switch behavior, themes and 320px overflow.

Read-only local catalogue assembly measured 0.75s. The first newly enriched live
catalogue took 10.43s while fetching new Hub facts; the warmed enriched live
catalogue plus inventory verification took 1.97s. Initial curated painting remains
independent of enrichment. Measurements are local snapshots, not latency promises.

The first live check exposed that the runtime imports Core from the separate
`untracked/deploy/abstractcore` checkout. Only the two scoped Core commits were
cherry-picked there; unrelated source/TUI work was not deployed. One accidentally
reselected old commit was empty and skipped. The existing graceful gateway restart
was used after model inflight and runner ticks were zero. Subsequent live dark/light
checks all pass: mappings/presence, every local variant and exact delete target
reachable after disclosure, no duplicate local extras, recommendation explanation
and contrast, keyboard toggle, reload, filter restore, narrow layout and health.
No page errors or management writes. Verifier-only corrections used row data-mc-art
rather than an absent data-artifact attribute, and composited transparent surfaces
for contrast; no additional product changes were required for those corrections.

Single-agent visual review confirms readable badge and cards in desktop themes
and a bounded phone layout. In-app browser execution was unavailable; local
Playwright was used. Agora connection was refused; these local receipts and backlog
items record completion. No registry publication, new service or machine persistence.
No ADR change: Core retains identity/capability truth and Gateway composes shared UI.
No residual work within the reported LM Studio set. Unrelated HF adapters and caches
remain visible as unclassified inventory rather than receiving inferred capabilities.

- [Recommendation, dark](recommended-dark.png)
- [Recommendation, light](recommended-light.png)
- [New catalogue cards, dark](downloaded-catalog-dark.png)
- [Phone, light](mobile-light.png)
- [Machine receipts and all 23 mappings](validation.json)
