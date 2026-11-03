# Direct UX review — catalogue overview

## Execution Mode

Single-agent direct visual and browser review, using the UX review skill
implicitly. The user did not request an independent persona swarm.

## Planned Reviewer Set

Direct reviewer of the normal catalogue and Downloaded tasks.

## Actual Reviewer Set

Direct reviewer; no independent reviewers claimed.

## Subagents Used

None, consistent with the session's delegation constraint.

## Fallback Reason

The in-app browser execution tool was unavailable. Existing standalone Chromium
was used for visible local testing; no reduction in the explicitly tested states.

## Contract Degradation

No mandatory independent swarm was triggered. This report is not a consensus or
an independent adversarial review.

## Observed Evidence

The operator's screenshots show background version loading, large gaps and a
recommended name truncated to Qwen3…. Live dark/light screenshots now show full
Qwen3.8 Flash-Next, clear Recommended text, and one build per model. The first six
cards in both catalogue and Downloaded have equal 141px heights. The counted
chevron exposes all matching local builds, retains keyboard focus and collapses
without an additional request. Explicit refresh keeps the existing cards usable.

The primary walkthrough is open Catalogue → local search/filters → inspect a
model → expand its other builds → use a named icon operation. Mock browser
checks validate download/default changes and two-step deletion; deletion feedback
remains visible after a downloaded card is removed. Live model operations are
not executed. Advanced supporting assets remain behind the Asset type facet.

## Inference Or Unverified Claims

No universal performance/SLA or subjective user acceptance is claimed. Latest
local first-card times are 1.56–2.04 seconds. Live upstream Hub results and live
model mutations were deliberately not exercised; explicit Hub request semantics
and operation outcomes are covered with isolated browser fixtures.

## Blocking UX Issues

No blocker found in the reviewed scope. The extra loading phase, clipped names
and default variable-height comparisons have been removed. Opening a comparison
intentionally increases that card's height; the counted chevron makes this an
explicit user action.

## Terminology And Meaning Risks

Catalogue and Hugging Face are now distinct sources. All is scoped to the selected
source: known catalogue builds versus upstream versions. Counts in the toolbar
are models; numbers on chevrons are per-model matching builds. Adapter metadata
is still declared local metadata rather than a runtime compatibility guarantee.

## Critical Distinctions

Models versus adapters/components remain distinct. Downloaded means local files;
partial caches remain visibly Cache/Partial with no unsupported chat default.
A current default and ready installed build are easier to reach than another
provider's proposed download. Expanding a card does not initiate a download.

## State Coverage

- Entry/waiting: delayed browser fixture with real spinner; ready live page.
- Happy path: live local search, Downloaded, comparison and return.
- Empty: live visible no-match state and Clear filters recovery.
- Error: browser fixture initial failure, stopped spinner and retry recovery.
- Permission/approval: reader actions disabled; delete dry-run, Keep and deliberate
  confirmation exercised in isolated browser/functional fixtures.
- Responsive: live dark/light at 390px and 320px, actual 320px touch.
- Keyboard: disclosure Enter, retained focus, filter switches and shared tooltips.

## High-Value Improvements

Local browsing no longer initiates unsolicited network discovery. Progressive
comparison keeps the first decision compact. The name is readable before the
user commits an action, and the existing capability matrix remains comparable.

## Persona Verdicts

Not run as independent personas; no fabricated naive/intermediate/expert votes.
The direct review found no blocking issue in the inspected workflow.

## Workflow Recommendations

Keep online discovery explicit and avoid reintroducing a global expansion pass.
Keep the overview's primary action close to its model; comparisons belong behind
the counted disclosure. Existing model confirmation and provenance paths remain.

## Accessibility And Visual Notes

Theme-aware contrast and shared structured explanations retained. Names wrap
instead of being clipped. Capability positions remain fixed and targets remain
at least 24px on narrow cards. Touch disclosure retains 44px targets.
No horizontal overflow at 320px. Expandable headers retain hover/focus feedback;
headers with one build do not imply a disclosure action.

## Validation Status

51 focused tests, 262 Chromium assertions and all saved live checks pass. No
page errors or live model-management writes; gateway healthy after deployment.

## Final Verdict

Approved for the directly reviewed functional/readability scope. Aesthetic
acceptance remains the operator's decision; no broader app-wide approval implied.
