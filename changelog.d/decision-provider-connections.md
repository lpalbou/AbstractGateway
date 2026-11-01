### Fixed

- **TypeSafe (Jev) and System One connections save** (Providers → Connect). The key check passed, then Save
  answered "Not saved: Unsupported provider family 'typesafe'": the root store is AbstractCore's
  `provider_profiles`, whose validator kept its own list of eight families. AbstractCore now derives the
  accepted families from its provider registry (every provider reached over HTTP), so a `typesafe` connection
  (key, no base URL) and a `systemone` one (base URL, key optional) are created, listed as connected, edited
  (PUT) and removed (DELETE) like the others, and `endpoint:<id>` routes them with the stored key and URL. A
  bogus family is still refused, naming the accepted ones. Needs the AbstractCore release with
  `ProviderRegistry.endpoint_provider_families`. Tests: `tests/test_gateway_provider_endpoint_profiles.py`
  (typesafe and systemone create/update/delete, families follow the registry).
