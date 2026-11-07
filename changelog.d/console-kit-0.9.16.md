### Changed

- **The console's kit islands and themes come from the published ui-kit 0.9.16** (`console_islands.py`,
  `console_themes.py`, synced from the AbstractUIC v0.10.3 release; they were at 0.9.15). The console
  now carries the same kit version as the apps: its voice picker is the kit's rebuilt VoiceSettings
  (on the shared `useVoiceSample`, `voiceChoices` and `voiceChoicePatch` helpers; the kit says behaviour
  is unchanged) and the kit's latest component styles.
