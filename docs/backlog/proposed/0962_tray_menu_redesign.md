# 0962-gateway: Redesign and restructure the gateway's menu-bar (tray) menu

> Package: abstractgateway (tray)
> Type: design
> Status: proposed
> Created: 2026-10-29
> Priority: P2
> Labels: tray, macOS, windows, design, readability

## Summary

Operator review of the macOS tray menu (2026-10-29): the first line "AbstractGateway — Running" is greyed
(disabled) although it is the identity of the app; "Addresses" and "Network" say more or less the same
thing; the memory line mixes machine memory, this gateway's memory and GPU busy in one grey sentence.

Target structure: line 1 "AbstractGateway (version)" as a normal, clickable item (opens the console's
About/status), line 2 "Open Console"; then Apps; one "Network" submenu that merges Addresses and Network
(the primary address with copy, exposure mode, Tailscale/Funnel state); Conversations; Models with a
readable one-line status (what is resident, GPU busy) and the memory figures as a submenu, not a sentence;
Pause/Restart/Start at login grouped; Check for updates; Help; Quit. Every line readable by a person
(ADR 0043): no wire words, no ids.

## Acceptance (for promotion)
- [ ] A mock-up (text or screenshot) reviewed by the operator.
- [ ] The tray code renders the structure on macOS and Windows with the same labels; tests on the menu model.
