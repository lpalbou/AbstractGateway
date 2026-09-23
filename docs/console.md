# Consoles: web (`/console`) and terminal (`abstractgateway-console`)

AbstractGateway ships two operator consoles over the same admin HTTP API. Both
edit the same stores through the same endpoints with the same request bodies, so
a change made in one is immediately visible in the other.

| | Web console | Terminal console |
|---|---|---|
| Delivery | served by the gateway at `GET /console` (part of the `abstractgateway` Python package) | Rust crate [`abstractgateway-console`](https://crates.io/crates/abstractgateway-console), installed with `cargo install` |
| Sign-in | Gateway browser session (user id + token) | base URL + bearer token (`ABSTRACTGATEWAY_AUTH_TOKEN` or the Connection screen) |
| Best for | day-to-day administration in a browser, sandbox chat with media previews | SSH sessions, headless hosts, keyboard-only setup |

For how the stores behind these screens are owned (Gateway vs AbstractCore),
see [configuration.md](./configuration.md). For the endpoints themselves, see
[api.md](./api.md).

## Web console (`/console`)

Start the gateway and open `http://<host>:<port>/console`:

```bash
abstractgateway serve --host 127.0.0.1 --port 8081
# then open http://127.0.0.1:8081/console
```

The console uses the current origin, so it never asks for a gateway URL. With
user auth enabled, the first-login token is written to
`<ABSTRACTGATEWAY_DATA_DIR>/auth/bootstrap-admin-token`. The console covers:

- **Users & runtimes:** user records, token rotation, retained runtime
  reservations, the runtime inventory with sessions, data and caches.
- **Providers:** provider connections (OpenAI, Anthropic, OpenRouter, Portkey,
  LM Studio, Ollama, custom OpenAI-compatible endpoints) with write-only keys.
- **Multimodal capabilities:** capability route defaults, the text reasoning
  effort, and the MTP (speculative decoding) default.
- **Workflows:** every registered workflow with versions and entrypoints,
  import/export/delete, and versions that are not served (with the reason).
- **Sandbox:** quick chat and media generation against the configured defaults.
- **Resources:** memory/GPU meters, resident models (warm up, lock, unload),
  and session prompt caches.
- **Entities:** summoned-entity roster and management (see
  [entities.md](./entities.md)).

## Terminal console (`abstractgateway-console`)

Install it from crates.io (Rust 1.87 or newer):

```bash
cargo install abstractgateway-console
```

Connect it to a running gateway. Pass the token through the environment rather
than on the command line:

```bash
ABSTRACTGATEWAY_AUTH_TOKEN=... abstractgateway-console --url http://127.0.0.1:8081
abstractgateway-console --help
```

It opens as a guided wizard on first run and as free tabs afterwards, with eight
screens: Connection, Providers, Routes (capability defaults, including the MTP
selector and a Test verb per route), Users & Entities, Runtimes (runs with
cancel and steer, data homes), Workflows, Review & Test (the session's change
journal), and Resources. Every write is verified with a follow-up read and
recorded in the journal. Keys: `Tab` focus, `Enter` activate, `1`-`8` screens,
`r` refresh, `q` quit; each screen lists its actions in the footer.

The terminal console needs no gateway-side component beyond the admin API. The
crate version is independent of the Python package version; see
[`console-tui/CHANGELOG.md`](../console-tui/CHANGELOG.md).

## Model residency from a shell

The same model routes the consoles drive are available from the Python CLI
against a running gateway:

```bash
abstractgateway models loaded --url http://127.0.0.1:8081
abstractgateway models load   --url http://127.0.0.1:8081 --provider ollama --model qwen3:4b
abstractgateway models unload --url http://127.0.0.1:8081 --provider ollama --model qwen3:4b
```

The token comes from `--token` or `ABSTRACTGATEWAY_AUTH_TOKEN`, and the URL from
`--url` or `ABSTRACTGATEWAY_URL`. The command prints the gateway's JSON answer
and exits non-zero when the gateway reports a failure. `unload --force` unloads
a locked model; in-flight calls on the model are cancelled first.
