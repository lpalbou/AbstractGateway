# Accounts action containment — 1005

Gateway `04a3f981`, locally redeployed 2026-10-30. The action column now budgets
404px for all eight 44px controls, including spacing and padding. Text columns
share the remaining width. The existing flat cards take over when the table
cannot fit, and action wrapping prevents buttons escaping a narrow container.
All account actions and credential semantics are preserved; production changes
are CSS only. Shared theme, icons, tooltip and focus behavior remain intact.

Live baseline reproduced 34px right overflow at 1600/1440/1280/1100/1024/920px.
After redeployment, dark and light checks at 1600/1440/1280/1100/1024/920/880/834/
390/320px plus 390px touch show zero button/table/page overflow and minimum 44px
targets. Desktop tables keep every action on one line; cards wrap safely. No
page errors or live account mutations occurred. Healthy idle restart and the
new served source are verified.

The disposable Gateway Accounts browser suite passes 121 assertions, including
all eight member actions visible with 44px bounds and containment at every
width, 320px/390px/834px touch, archive/unarchive, tooltips, modals and entity
creation. Its login helper and action expectations now reflect password support.
19 focused console/workspace tests pass. One pre-existing static catalogue
assertion in `test_r9w2_console_workspace_modals.py::test_icon_buttons_use_the_kit_tooltip_never_a_native_title`
fails while parsing `mcDeleteButton` (`ValueError: substring not found`); both
that test and the catalogue source match HEAD unchanged. It was excluded from
the focused rerun; no claim that the entire console suite passes.

Two independent adversarial reviewers found no blocking source issue. Their
requested stronger visibility checks and minor comment/diagnostic fixes were
adopted. Conditional checks are satisfied by the browser gate and live captures;
integrated verdict: approved for this layout scope. No fallback or lost reviewer
lens. No account/security redesign, Core changes, new ADR boundary, publication,
persistence or required layout follow-up.

[Before](before-dark.png), [dark](desktop-dark.png), [light](desktop-light.png),
[narrow](narrow-dark.png), [phone](phone-light.png), [touch](touch.png),
[baseline](baseline.json), [live checks](validation.json),
[browser checks](browser-validation.json), [restart](deployment.json).
