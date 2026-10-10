# Consoles: web (`/console`) and terminal (`abstractgateway-console`)

AbstractGateway ships two operator consoles over the same admin HTTP API. Both
edit the same stores through the same endpoints with the same request bodies, so
a change made in one is immediately visible in the other.

| | Web console | Terminal console |
|---|---|---|
| Delivery | served by the gateway at `GET /console` (part of the `abstractgateway` Python package) | Rust crate [`abstractgateway-console`](https://crates.io/crates/abstractgateway-console), installed with `cargo install` |
| Sign-in | Gateway browser session (user id + token) | gateway URL + token (`--gateway-url URL --token <token>`, or the Connection screen) |
| Best for | day-to-day administration in a browser, sandbox chat with media previews | SSH sessions, headless hosts, keyboard-only setup |

For how the stores behind these screens are owned (Gateway vs AbstractCore),
see [configuration.md](./configuration.md). For the endpoints themselves, see
[api.md](./api.md).

## Web console (`/console`)

Start the gateway and open `http://<host>:<port>/console`:

```bash
abstractgateway serve
# then open http://127.0.0.1:8080/console
```

On a first run, `serve` prints a one-time link that signs you in
([first-run.md](./first-run.md)); `abstractgateway claim --open` prints a new
one. The console uses the current origin, so it never asks for a gateway URL.
You can also sign in with a user id and token (the admin's first token is in
`<data dir>/auth/bootstrap-admin-token`).

The sidebar groups the tabs in four sections, in this order:

| Group | Tab | What it covers |
|---|---|---|
| Accounts | **Accounts** | every user and entity in one table: the **Active** switch, email, a link to each account's runtimes, activity (Logs), workspaces, token rotation, entity management, the administrator's **Eligible workspaces** and **Email for everyone** switches ([below](#accounts)) |
| Work | **Workflows** | the workflow bundles on this gateway, shared by the gateway or your own (name, what each does, version, source, the apps that use it, availability to users), import, export, open in AbstractFlow, archive, the default workflow for each app and the **Streamed replies** setting ([below](#workflows)) |
| Work | **Skills & MCP** | administrators: **Skills** — the skills shelf setting and every skill (curated and imported, **Show archived**) with View, Export and Archive, import a `.zip` or folder, Duplicate to edit a curated one; **MCP servers** — whether agents are offered tools, an **Enabled for agents** switch per server, add, edit, test and archive ([configuration.md](./configuration.md#skills-shelf)) |
| Work | **Runtimes** | execution planes: runs (cancel, steer), sessions, data and caches; `#runtimes?account=<id>` lists one account's runtimes ([below](#runtimes-of-one-account)) |
| Work | **Apps** | the browser apps, Code's terminal app and the desktop Assistant (below), no settings disclosures: the toolbar gear opens **Apps settings** and the gear beside Continuum's Open opens **Continuum settings** (backlog folder, exec runner, process manager), each a dialog whose rows apply on their own |
| Models | **Providers** | **Local providers**: one card per local engine on the gateway host (Ollama, LM Studio, MLX, llama.cpp, Hugging Face, vLLM) with its status, install, start, stop, **Browse models** and its server connection; **Remote providers**: OpenAI, Anthropic, OpenRouter, Portkey and custom OpenAI-compatible connections with write-only keys (shown as fingerprints); then the **Available Providers** table ([below](#models-and-local-providers)) |
| Models | **OpenAI API** | the OpenAI-compatible API at `/v1`: status with Endpoint switch, Restart and Check setup; the base URL and your named API keys (New key, Reveal, Copy, Revoke); Authentication, who may reveal keys and Who can connect, applied at once; supported surface and snippets; recent requests ([openai-api.md](./openai-api.md)) |
| Models | **Models** | browse models that fit this machine, download them, delete installed ones (below) |
| Models | **Multimodal** | capability route defaults, the text reasoning effort, the MTP (speculative decoding) default, and model weights per route |
| System | **Resources** | memory and GPU meters, resident models (warm up, lock, unload), session prompt caches, and the **Gateway** card (pause, update, restart, desktop icon, start at login, last restart after a hang) |
| System | **Sandbox** | quick chat and media generation against the configured defaults |
| System | **Network** | who can reach the gateway (localhost only, local network, internet), its addresses (with the Tailscale name when Tailscale runs), *Reached through another address?* (Tailscale, a reverse proxy: detected addresses are accepted automatically; manual allowed origins and *Trust proxies on other machines* are in the same card) ([configuration.md](./configuration.md#network-exposure-localhost--local-network--internet)) |

Below the groups, at the bottom of the sidebar, **Setup** (administrators)
runs the setup guide again: engines, default models, apps, network. It keeps
your current choices unless you replace them ([first-run.md](./first-run.md)).
The **Technical details** switch sits under it.

**Start at login** (the Gateway card, and the setup guide's last step) is a
switch for admins: it names the mechanism (a LaunchAgent, a systemd user
unit, a desktop autostart entry, a Windows Run entry), asks before each change
and shows the state read back from the gateway. Where nothing on the machine
could start the gateway at login, it says why instead of offering the switch.

**Workflows paused** (the Gateway card) is a switch for admins: on, no new
workflow step starts and work already inside a call finishes; the console and
the apps keep answering. A banner on every tab says so while it is on.

**Last restart** (the Gateway card, admins) appears after the event-loop
watchdog restarted the gateway: "Gateway restarted at <time> after a hang —
<reason>", the reason naming the code the event loop was stuck in and the
request it was serving, with the path of the file holding every thread's
stack (`<data dir>/incidents/`); its tooltip names the frame, the stack dump and
the incident file. No incident, no row.

Every persistent on/off setting in the console is a switch labelled by the
feature (highlighted when on). It applies at once, shows the new state, and an
unavailable switch stays visible with its reason ("Connect a mailbox first.").
One-shot actions (Rotate token, Archive, Test, Disconnect) stay buttons.

The **sign-in page** asks for the gateway user and the token, with one status
("Not signed in", "Signed in as admin", "Token refused") and errors under the
field that failed. When sign-in by email is on, **Forgot your token? Email me a
sign-in code** requests a code and opens the code step in place
([email.md](./email.md#sign-in-by-email)).

In **Multimodal**, a configured route whose engine is not installed reads
"engine missing" with the install command, and the voice pickers say why a
provider lists no voices. Under the capability table, **Spoken language** is
your own account's spoken language, the same setting as the last row of
[Preferences](#preferences-of-an-account): pick **Auto (detected)** or a
language, it saves at once ("Saved." / "Not saved." with the gateway's
sentence). The cloud voice providers (OpenAI,
OpenAI-compatible) are always listed, marked "needs an API key" until you add
one under **Providers**.

The **Technical details** switch at the bottom of the sidebar shows commands,
route ids and other technical information throughout the console. The top bar
holds the **Docs assistant** (book icon: the same chat as every app — your question on the right, the answer on the left with Markdown, code and links, copy, attachments, live streaming, an icon-only New conversation; answers come from this gateway's llms.txt through the docs-qa workflow, as its one-line footer says; **Past conversations** reopens or archives an earlier docs chat, and docs chats never appear in the conversation lists),
the appearance settings, **About**, who is signed in, and the sign-out control.
Left of them sits the **memory and compute line** — the same glance as the
desktop tray: memory used of total (with the percentage), GPU busy % and how
many models are loaded, with two small bars for memory and GPU (at phone
width the memory reads as a percentage). It reads the Resources page's data
(`GET /api/gateway/host/state`) and refreshes every 5 seconds while you are
signed in and the browser tab is visible. Hover or focus it for the real
values, one per line: RAM, the accelerator heap (and whose memory it counts),
model weights and models loaded, KV caches (for models and in sessions) and
GPU load; a figure the host does not report reads "—" or "unknown", never 0.
Click it (or press Enter on it) to open **Resources**. The gateway's
addresses, each with **Copy**, are on the **Network** page.

**About** (the *i* button) is the compact card every AbstractFramework app
shows: AbstractGateway and the version this gateway runs, the AbstractFramework
version installed on this host (or "not installed on this host"), one row of
links (Website, Source, Docs, Issues, Feedback, Contact) and the copyright and
licence line. It lists no packages: the per-package versions stay on `GET
/api/gateway/about`.

### Responsive layout

The web console works in any browser window and on phones and tablets:

- From 1024 px wide, the sidebar is a column on the left. Below that, it becomes a drawer: open
  it with the **☰** button in the header, and close it with Escape, the close button, a tap
  outside it, or by picking a section. While the drawer is open, the page behind it does not
  respond to clicks or keyboard focus.
- On narrow screens the top-bar buttons wrap under the page title. In phone landscape the header
  is a single thin row.
- Below 768 px wide, or in phone landscape, dialogs open as bottom sheets with their buttons
  always visible, and the docs assistant drawer is full width. A dialog opened while a drawer is
  open appears above it. Escape closes the top one first.
- On touch screens, buttons and rows are at least 44 px tall, form fields use 16 px text (iOS
  does not zoom into a focused field), and reading text is 14 px.
- The layout respects the notch and home-indicator areas of phones.

### Accounts

**Accounts** lists the people who use this gateway and the entities that act
on it, in one table (`GET /api/gateway/admin/accounts`, see
[api.md](./api.md#accounts-and-activity)). Above it, **Create user** issues a
user and their token (shown once) and **Create entity** summons a new entity
from a spark template (its name is permanent, [entities.md](./entities.md)).
Administrators also have **Eligible workspaces** first in that row: the
workspaces accounts may choose from, and the most each one allows
([below](#workspaces)).

Below that row, everyone signed in sees the host's **command sandbox** as a
state line, with an explanation in its tooltip:

- **Commands sandboxed: macOS sandbox-exec** (or Linux bubblewrap): every
  command a run starts is confined by the operating system to that run's
  workspaces.
- **Commands refused: no sandbox on this host**.
- **Unsandboxed commands allowed (flag)**: the gateway was started with
  `serve --unsandboxed-commands`.

It is a state, not a control. The terminal console shows the same line under
its Accounts header ([security.md](./security.md#command-sandbox)).

| Column | Shows |
|---|---|
| **Name** | the account id and a kind chip: **Admin**, **Member** or **Entity** (hover it for the role). There are two roles: admin and member; a member is a human account or an entity |
| **Email** | the account's address and its mailbox state in one line: `alice@example.org · connected`, `· not connected`, `· receive only` (with the reason under it) or `· paused`; "No address" when the account has neither a registered address nor a connected mailbox (users and entities alike: an entity has its own mailbox) |
| **Runtime** | the account's runtime id as a link to the Runtimes page filtered to that account (administrators; plain text for everyone else), or "No runtime" |
| **Active** | the switch described below; "Archived" on an archived row |
| **Actions** | icon buttons, each with a tooltip sentence: users **Email**, **OpenAI API**, **Logs**, **Workspace**, **Rotate token**, **Archive**; entities **Email**, **Logs**, **Workspace**, **Manage**, **Archive**; archived rows **Logs**, **Unarchive** |

Rows are tinted by kind (a legend under the table reads "Tint: admin · member ·
entity"). Only the actions that apply to a row are shown: your own row has no
**Archive** and an entity has no token to rotate. The action buttons are
44 px targets. Hover one, or reach it with the keyboard, and a tooltip says
what it does for whom: "Email address and mailbox of alice", "OpenAI API
access for alice", "Activity log of alice", "Workspaces alice's agents may
use", "Manage castor (mind, voice, prompt…)", "Rotate alice's sign-in
token", "Archive alice (kept, hidden)". The same tooltip explains every icon
button in the console (Workflows, Models, Apps, the top bar…): it appears
after a short pause, stays inside the window and Escape hides it. The table never scrolls sideways: long names and
addresses wrap, and below about 900 px of window width each account becomes
one flat block: name, kind chip and Active, then the email line, then the
runtime, then the actions, which wrap.

**Show archived** (above the table) lists archived accounts too (for someone
who is not an administrator: the archived entities they created); it is off by
default and your browser remembers it.

- **Active** for a user: off signs them out and refuses their sign-in until you
  turn it back on. Turning it off asks first ("Deactivate alice? They are
  signed out until you turn Active back on."). Your own account and the last
  active admin cannot be switched off.
- **Active** for an entity: off suspends it (it stops acting and its gateway
  credential is switched off) after "Suspend castor? It stops acting until you
  turn Active back on."; on resumes it in the state it had before.
- **Email** opens a dialog. On your own row it holds your email settings: your
  email address, your mailbox, the **Job failed** and **Approval needed**
  notifications, **Agent email tools** and **Recipients and limits** ([email.md](./email.md)).
  An administrator's own row adds one sentence: the address also receives the
  administrator's sign-in codes and notifications, and the mailbox serves the
  administrator's own agents. On another user's row it holds only that user's
  **Email address** (with Save) and a read-only mailbox line ("Mailbox: not
  connected — only alice can connect a mailbox. You never see anyone's mail.").
  On an entity's row it holds the same email settings as your own, for the
  entity's own mailbox (an entity is an AI user: its agents read and send from
  it, and notifications about its runs go to its address); the administrator
  and the entity's creator can open it.
- **OpenAI API** (administrators, user rows) opens a dialog with the
  account's **OpenAI API** switch and its API keys (never a key) with **Revoke**:
  on, the account's API keys work at `/v1`; off, its requests answer `403 openai_api_off` (its console
  sign-in is unchanged). Entities have no key and never use the OpenAI API
  ([openai-api.md](./openai-api.md#access)).
- **Logs** opens "Activity — <id>": the account's sign-ins, token rotations,
  runs started, automation commands, account changes and email events from the
  gateway's audit log, newest first, in your local time. The chips **All**,
  **Sign-ins**, **Runs**, **Automations** and **Email** filter the list; a run
  event names its workflow and links to the run in the Observer app (**Open in
  Observer**, `/apps/observer/#run/<run_id>`); a run started under a
  gateway older than 0.10.0 says "Run id not recorded (before this version)" and has no link.
  Notifications say what they were about ("Approval needed", "Job failed",
  "Test notification"). The footer says what the audit log does not
  record (page views and reads, mail received, what agents send with their
  email tools).
- **Workspace** opens "Workspaces — <id>", the workspaces that account's
  agents use ([below](#workspaces)). Every row has it — users, entities
  (including entities older than entity accounts: the gateway gives each one
  an account at start) and your own.
- **Rotate token** asks in a row under the account ("Rotate the token of
  alice? The current token stops working now; the new one is shown once."),
  then issues the new token and shows it once. Entities have no token to rotate.
- **Manage** (entities) opens the entity's lifecycle, mind and voice,
  capabilities and prompt; **Talk** is there too. **Mind & voice** uses the
  shared pickers: **Gateway default** (the gateway's text model and default
  voice) or its own choice; changes save themselves. An administrator or the
  entity's creator changes its mind, voice, tools per phase and instructions
  (the creator picks among the models and voices the gateway offers; a tier-2
  tool's box stays off for the creator, with the tooltip "Only an admin can
  give castor a tier-2 tool: …"); sleep and wake, personal time, freeze, the
  work order and the memory index rebuild are an administrator's. Anyone else
  reads the current values as text.
- **Archive** asks in a row under the account ("Archive alice? They can't sign
  in any more. Their runtime, runs and history are kept; you can unarchive
  later." / for an entity: "It stops acting and never wakes. Its memory, runs
  and history are kept"). Nothing is deleted. **Unarchive** brings the account
  back inactive: turn **Active** on to let it sign in (or act). Accounts are
  never deleted (`DELETE /api/gateway/admin/users/{id}` answers `410`).

Under the table, **Email for everyone** (administrators) holds the switch
three switches, directly in the card: **Mailboxes for users**, **Agent email
tools for users** and **Sign-in by email** ([email.md](./email.md#administrators)).

Someone who is not an administrator sees the page as **Your account** ("Your
account and the entities you created."): the same table with their own row and
one row per entity they created (`GET /api/gateway/me/accounts`). There is no
Create user, no Eligible workspaces and no Email for everyone;
their own row's **Workspace** opens their own workspaces, and an entity's row
opens that entity's; it has no **Rotate token**
(only an admin rotates tokens). They configure an entity they created: its
**Manage** settings, **Archive**, **Unarchive** (with **Show archived** on) and
its **Active** switch. See [security.md](./security.md#who-configures-an-entity).


### Workspaces

A workspace is a directory an agent may work in. Three levels decide which
workspaces a run may use, each inside the one above
([security.md](./security.md) explains how the gateway enforces it):

1. **Eligible workspaces** — the administrator's set: what any account may
   choose from, and the most each workspace allows.
2. **An account's workspaces** — what that account's agents use, among the
   eligible ones (every account has its own: users and entities).
3. **One conversation's workspaces** — chosen in the apps (AbstractCode,
   Flow, Observer, the Assistant) for one conversation or one run, inside the
   account's.

Rows may nest: **the most specific row wins**. Refusing `/Users/me` and
allowing `/Users/me/projects` (Read & write) is valid; the child is usable and
the rest of `/Users/me` is refused. A refused row inside an allowed one refuses
that part. The chooser shows the gateway's answer; it has no rule of its own.

Every run also has its own private workspace (`<data dir>/workspaces/session-…`):
a file written without a full path lands there. It is always available, read &
write, and never listed. The workspaces a run may use are listed to its agent
with their paths and modes.

Both dialogs below are the same chooser, with the same words, as the apps'
workspace settings. Their parts:

| Part | What it does |
|---|---|
| **Gateway: …** (account level) | the eligible set in one line, as the gateway states it, e.g. "Gateway: Allow everything, refuse listed workspaces (rw) · /secrets (refused) · /archive (ro)" |
| **Follow the gateway policy** (account level) | a switch: on, the account gets exactly the eligible set; off, the account has its own posture and rows (starting from what applied) |
| Posture | **Deny everything, allow listed workspaces** (only the **Allowed workspaces** listed below) or **Allow everything, refuse listed workspaces** (any workspace except the **Refused workspaces** listed below; **Everything else** sets **Read-only** or **Read & write** for the rest) |
| Rows | one row per workspace: the path, **Read & write** / **Read-only** / **Refused**, and a remove icon; **Add a workspace path** adds one (Refused under Allow everything, refuse listed workspaces; under Deny everything, allow listed workspaces Read & write in Eligible workspaces and Read-only in an account) |
| The last line | what applies, as the gateway states it, e.g. "Deny everything, allow listed workspaces · /Users/me/Pictures (rw) · /Users/me/Documents (ro)" |

**Eligible workspaces** (administrators, top of Accounts) holds the
administrator's set. A row's mode is its **cap**: no account can use that
workspace with more. Under it, the gateway's own data directory and credentials
are listed as always refused. A fresh gateway starts at Allow everything,
refuse listed workspaces (Read & write), with no row.

**Workspace** on an account's row opens that account's level. An account
chooses among the eligible workspaces: a path outside them, or a mode above a
workspace's cap, is refused with the gateway's sentence. A mode above the cap
is shown greyed and keeps its tooltip, "The gateway allows this workspace
read-only" (reachable with the keyboard too). An entity's workspaces are set by
its creator or an administrator; someone who is not an administrator opens
their own from their own row.

Every change applies at once and is saved by the gateway; there is no Save
button. When the gateway refuses a change, its sentence shows under the control
with "Not saved." and nothing changes. The API is `GET`/`PUT
/api/gateway/workspace/policy` (eligible workspaces) and `GET`/`PUT
/api/gateway/workspace/policy/{account}` (`me` for your own) ([api.md](./api.md)).

### Preferences of an account

**Preferences** on an account's row (the sliders icon, tooltip "Default workflows of <name>")
opens the workflow each app runs for that account unless a conversation picks another: one row per
app (AbstractCode — chat agent, Assistant). The first option is **Gateway default (<name>)**,
selected while the account has no choice of its own; it follows the administrator's **Default
workflow per app** (Workflows). The other options are the workflows the account may run for that
app. A change applies at once and says "Saved."; there is no Save button. When the gateway refuses
a change, its sentence shows with "Not saved." and nothing changes. A choice that no longer runs
says why under its row.

The last row is the account's **Time zone**: daily, weekly and monthly automations run on this
clock, and automation times show in it. The first option is **Gateway default (<zone>)**, this
computer's time zone, selected while the account has none of its own; the other options are the
time zone names the gateway knows (type to search, e.g. "Europe/Par"). A pick applies at once
("Saved." / "Not saved." with the gateway's sentence); new automations take the zone in force when
they are created. The list, its label and its help come from the gateway (the `time_zone` block of
the answer); the row is the kit's time-zone picker, the same one AbstractCode uses. An answer
without that block shows an error in the dialog instead of the row.

After it comes **Spoken language**: the language you speak to the microphone. **Auto (detected)**,
the default, lets the speech engine detect it; naming a language (English, French, ...) skips
detection, so short phrases and mixed-language speech transcribe reliably and a little faster.
Every transcription of the account uses it — dictation in the apps and the Sandbox, and the
OpenAI-compatible transcription route — unless a request names a language of its own. A pick
applies at once ("Saved." / "Not saved." with the gateway's sentence). The label, the help (the
**?** beside the label) and the list of languages come from the gateway (the `spoken_language`
block of the answer); an answer without that block shows an error in the dialog instead of the
row.

Everyone opens their own from their own row. An administrator opens any account's (choosing among
the gateway's shared workflows); an entity's preferences are set by its creator or an
administrator. The Assistant (Settings → Workflow) and AbstractCode (Workflow → *Default for new
conversations*) read and write the same choice. The API is `GET`/`PUT
/api/gateway/accounts/{account}/preferences` ([api.md](./api.md#account-preferences)).

### Runtimes of one account

The Runtime link on an Accounts row opens **Runtimes** at
`#runtimes?account=<id>` (`GET /api/gateway/admin/runtimes?account=<id>`): a
chip "Account: alice" names the filter, only that account's runtimes are
listed, and a single runtime opens at once. The link survives a reload; the
× on the chip shows every runtime again. The **Workspace** column opens the
workspaces that apply: **Eligible workspaces** for the default runtime,
**Workspaces** (the owner's) for a user's or an entity's runtime.

### Workflows

The **Workflows** tab starts with what workflows are: the programs your apps
and automations run, packaged as bundles (`.flow` files) that ship with the
gateway, that you import, or that you publish from AbstractFlow.

The table has one row per bundle, in two groups: **Shared with everyone** (bundles that ship with the gateway and those an admin imported or
published) and **Mine** (bundles you imported or published yourself; shown only
when you have some). An admin's own imports are shared, so admins see one group.
A user sees the shared bundles an admin left available, and their own.

| Column | Shows |
|---|---|
| **Name** | the default entrypoint's name, with the bundle id under it, and **Deprecated** / **Archived** pills when they apply |
| **What it does** | the owner's description when they wrote one, else the default entrypoint's (two lines; click shows it whole). The owner (an admin for shared ones; never a shipped bundle) edits it in place: the pencil opens a text box, Enter or leaving the box saves it (`PATCH /api/gateway/bundles/{id}`), Escape cancels, "Saved" shows beside it |
| **Version** | the latest version ("+2 older" when there are more; a manifest version 0.0.0 reads "unversioned") |
| **Source** | a badge: "Shipped", "Imported" or "From AbstractFlow" |
| **Used by** | the plain names of the apps that ask for its interfaces, each with a (?) that explains it, or "No app" |
| **Available to users** | (administrators only, shared bundles) a switch; off hides the workflow from users' lists and app pickers |
| **Actions** | icon buttons with tooltips, in one row that never wraps: **Export**, **Open in AbstractFlow** (a new tab) and **Archive** (or **Unarchive**) for imported and published bundles |

Rows do not expand. With **Older versions** on, each older version is its own
row under its bundle (version, channel, date and its own three actions). The toolbar has a search field, the switches **Drafts**, **Older
versions** and **Show archived**, and **Import .flow**. Everyone can import: a
user's import lands in **Mine**, an admin's in the shared group.

Workflows are never deleted. **Archive** asks in the row ("Archive … ? It
disappears from lists and can't start new runs; the file and every past run
stay on the gateway.") and hides the bundle (or one version); runs and
automations that already use it keep resuming and replaying. **Show archived**
lists archived bundles with **Unarchive**. Bundles that ship with the gateway
have no Archive at all; an admin hides them with **Available to users** instead.
Admins archive shared bundles; each user archives their own.

**Available to users** (administrators): off hides the workflow from every
user's lists and app pickers (AbstractCode, the Assistant, AbstractFlow), refuses
their new runs and automations with "This workflow isn't available to users on
this gateway. Ask an admin.", and pauses their existing automations on it with
the reason "Paused: this workflow is no longer available to users — ask an
admin." Turning it back on does not resume those automations; each user resumes
their own. Admins always see every workflow, and an app's default workflow
(**Default workflow per app**) keeps running for everyone even when it is hidden.

**Open** opens the workflow in the visual editor in a new tab
(AbstractFlow must be running: **Apps > Flow Editor**). A bundle published from
one of your flows opens that flow; any other bundle opens as an unsaved copy
titled `<name> · <bundle id>@<version>`, so Save creates your own flow and never
changes the bundle. **Broken workflows** appears only when the gateway refused a
bundle file: the workflow, the versions affected and why it cannot run, with
**Archive** to hide them.

**Default workflow per app** (administrators only; other accounts do not see it) comes next: "When an app asks for "an agent"
without naming a workflow, the gateway runs this one." One row per interface,
with its plain name ("AbstractCode — chat agent", "Assistant", "Deep research",
…), a (?) that says what it is for, and the interface id in small type. The
list offers the workflows that declare that interface, each once (the
registry scope is shown only when the same workflow comes from two scopes),
after "Gateway default: <workflow>" — what runs when nothing is saved. The
gateway always resolves that default when at least one workflow declares the
interface: the shipped bundle first (basic-agent, the Assistant orchestrator),
else the newest available workflow. A choice applies at once ("Saved"). A row warns
only when its saved workflow is broken (removed, deprecated, or no longer
declaring the interface). Interfaces that no app asks for sit under **Other
workflow types**. See
[configuration.md](./configuration.md#default-agent-workflow).

Under **Settings**, the **Streamed replies** switch sets
`agents.streaming_default`: when it is on, a new interactive run whose app does
not choose shows the model's reply as it is written. Scheduled runs, bridges
and entities always get whole replies. The switch applies at once
(administrators; others see "Only an admin can change this."). A gateway that
does not have this setting says so. The same setting is in the terminal console
(Runtimes → *Runtime knobs* → *Edit stream replies*) and in
`abstractgateway config set agents.streaming_default true|false`.

### Memory figures on Resources

The accelerator meter shows the larger of two figures and names it: the
memory **this gateway process** holds ("this process only"), or the
system-wide figure ("all processes"), which on macOS does not see MLX memory.
The process figure covers every model library in the gateway (MLX, llama.cpp
GGUF engines, transformers and embeddings), and the meter's tooltip says how
it was measured:

| Measured by | Meaning |
|---|---|
| metal device counter | the Mac's GPU counter for this process (MLX, torch and llama.cpp memory are all inside it) |
| cuda device counter | what PyTorch has reserved on the NVIDIA GPUs, plus the llama.cpp estimate when a GGUF model is loaded |
| sum of MLX + llama.cpp | MLX's live and cached buffers plus the llama.cpp estimate (weights plus the KV cache estimate), when no device counter is available |

When no model is listed as resident but the process still holds memory, the
resident-models table says "Gateway still holds N GB of accelerator memory (no
model listed)", then how it was measured, what holds it (for example `[mlx]
qwen/27b × 2 holders`, or "not attributed to any model" when no model library
reports it), and the two ways out: eject the held model, or restart the
gateway. A model the gateway's own pool released but another part of the
process still holds is marked **resident via other holders**; ejecting it
frees every holder.

Above the table, the gateway also lists the ejects it still owes or that
failed after you switched the default model: "Will eject X when the in-flight
call ends" (the old model is still answering a call), "X: eject failed:
reason", "X kept in memory: reason" (something else still uses it) and "X
ejected".

### Models and local providers

Everything the Models tab and the Providers tab's Local providers show and do happens **on the gateway host**: the
machine that runs `abstractgateway serve`, not the computer your browser runs
on. The catalog data, the presence checks and the fit verdicts come from
AbstractCore (`GET /api/gateway/models/catalog`, contract `model_catalog_v1`);
downloads are the gateway's own jobs (see [Model downloads](model-downloads.md)).

**Models** (tab id `catalog`) is one page: the catalog as **one compact card
per model**, and the downloaded models the catalog does not know in the same
list.

- The card header is one line: the model's name, organisation, parameter count
  and licence, its capabilities (Text, Thinking, Tools, Vision, Audio,
  Embedding, Voice, Image, Video) as small tags, and a **Starter** badge for the
  models of the recommended starter set.
- The card body: one line per downloadable build (artifact) of that model: the
  provider (MLX, Ollama, LM Studio, Hugging Face, ...), the artifact id (long
  ids are shortened with "..."; hover to read the whole id, click it to copy
  it), the quantization (4-bit, 8-bit, 16-bit, ...; hover for the exact
  quantization and bits per weight), the download size ("about" when the size is estimated from the parameter
  count), whether the weights are already here (Downloaded, Not downloaded,
  Unknown, Remote) and whether it fits this machine (Fits, Tight, Partial
  offload, Too large; hover the pill for the numbers behind it), then the
  actions. The build recommended for this computer comes first, with a small
  accent dot before its id; the others are quieter. On a narrow window the
  facts move under the id; every button is at least 44 px.
- One action per row: **Download** (a download shows its progress bar with
  bytes, speed and time left, and **Cancel**, the same progress display as the
  setup guide), then **Use as default** once a text model is downloaded (it
  sets the default text model, like the Multimodal tab).
- Many models have 8-bit builds next to the 4-bit ones when upstream publishes
  them (MLX `-8bit` repositories, Ollama `-q8_0` tags, GGUF `Q8_0` files).
  Every build the catalog knows is listed; nothing is cut off.

The filter bar above the cards:

- **Search** matches model names, organisations and artifact ids (every word
  must match). **Escape** clears it.
- **Quantization**: All, 4-bit, 8-bit, Other (every other class: 16-bit, full
  precision, 2/3/5/6-bit and builds whose reference names no quantization).
  The classes come from AbstractCore's `quant_class` field.
- **Catalog / Hugging Face**: the switch left of the search box. In
  **Hugging Face** mode, type a name and press **Enter** (or **Search**): the
  gateway searches the Hugging Face Hub (answers are cached for 24 hours) and
  the results show as the same cards, with a **Hugging Face** badge: provider,
  artifact id, quantization when the result names one ("Not stated"
  otherwise, never guessed), size from the Hub, fit, and **Download** with
  the same progress bar. Capabilities of a Hub result are unknown until it is
  installed, so it offers no **Use as default** from here. When the Hub has
  nothing for the query the view says so; when the gateway host cannot reach
  the Hub it says "Hugging Face could not be searched right now" (the raw
  reason sits behind **Show details**), and results the Hub only partly
  answered carry a warning. The query is part of the address:
  `/console#catalog?hf=smollm`.
- **Provider**, **Capability** and **Status** (Downloaded, Not downloaded)
  chips, each with the number of builds it would show (the rows under "Not in
  the catalog" included).
- **Fits this computer** hides the builds that do not fit (only Fits and Tight
  remain).
- A live count: "12 of 77 models · 31 artifacts shown". The row with the search
  box, the count and the filters in use stays under the header while you
  scroll; **Filters** brings the chips back into view. When nothing matches,
  **Clear filters** resets them.
- The filters are part of the address: `/console#catalog?quant=8bit&provider=mlx&fits=1`
  opens the tab with exactly that view, so a link reproduces it. The keys are
  `q`, `quant` (`4bit`, `8bit`, `other`), `provider`, `cap`, `status`
  (`downloaded`, `not_downloaded`), `fits=1` and `hf` (Hugging Face mode and
  its query).

**Delete** (a trash-bin icon; its tooltip names the build: "Delete <build> from
this computer (files only)") sits on every downloaded build's
row. A click first asks the gateway what the delete would free, then shows one
sentence under the row with that size, for example "Deletes 351 MB from this
computer. Files only — nothing in your runs is touched.", with **Keep** and
**Delete**. Only the downloaded files go (the Ollama model through Ollama's own
delete, the MLX / Hugging Face cache folder, or one GGUF quant's files, leaving
the repo's other quants); runs, conversations and settings are not touched. The
row then says "Not downloaded" and offers **Download** again. The gateway
refuses, and the row says why and what to do, when the model is loaded or
locked in memory ("Unload it first"), while its download is still running
("Cancel the download first"), or for LM Studio, which keeps its own library
(delete it in LM Studio). Every delete and refusal is written to the audit log.
The terminal console's Models page deletes through the same route, with the same confirmation.

**Not in the catalog.** With Status **All** or **Downloaded**, the models the
local engines hold that no catalog build accounts for (`GET
/api/gateway/models/installed`: an Ollama tag you pulled yourself, a Hugging
Face or MLX repository downloaded elsewhere) follow the cards as plain rows
under a small **Not in the catalog** heading: provider, id, the quantization
the engine reports, the size on disk, Downloaded (and Loaded) chips, and the
same **Delete** with the same confirmation and refusals. They follow the
Provider filter and the search; a capability, **Fits this computer**, the 4-bit
/ 8-bit chips and Hugging Face mode leave them out, because nothing is known
about them there. A model the catalog does know is never listed twice. If the
engines' list cannot be read the page says so under that heading.

The setup guide's **Default model** step shows the same catalog cards with
**Fits this computer** already on; **Open in the Models tab** carries the
filters over. An engine card's **Browse models** opens the tab filtered to that
engine's builds.

**Local providers** (the Providers tab, id `providers`; a `#engines` link
opens Providers):

- One card per engine with a status pill (Ready, Running, Installing, Needs
  your approval, Needs Apple tools, Not installed, Not for this computer),
  its version, base URL and model count, **Browse models**, **Learn more**,
  and one primary action for its state: **Install**, **Start**, **Stop**, **Continue with administrator
  password**, **Install tools** or **Try again**.
- **Install** opens a confirmation that shows what will run and the host it
  runs on, with a **Preview (dry run)** button. Ollama and LM Studio offer
  "Install" (just for you, no password) and, when that plan needs an
  administrator, "Install for all users (administrator)". What each install
  does, engine by engine: [engines.md](./engines.md).
- An install shows its progress on the card, one plain sentence first and the
  full log behind **Show details**. **Open download page** links to the
  vendor page.

Keys (when the tab is visible and you are not typing in a field): `/` search,
`f` fits-only on/off, `r` refresh; on a focused row `w` download, `d` delete,
`i` install, `o` open the download page, `c` cancel the row's job.

Who can do what:

- Every signed-in user can browse both tabs.
- Download, Delete, Install and Cancel are for **admins** only; the buttons are
  disabled for other users, and the gateway refuses those calls from them.
- Installing an engine also needs the gateway setting
  [`allow_engine_install`](./configuration.md#allow_engine_install). It lives in
  the runtime configuration and is on by default when the gateway listens on
  this machine only (`127.0.0.1`), and, whatever it listens on, for someone
  using the console on the gateway machine itself. A browser on another
  computer is refused until an admin turns the setting on. A dry run
  (**Preview**) is always allowed.
- Every action is recorded in the gateway's audit log, and each job card shows
  the equivalent command, for example
  `abstractgateway models download ollama qwen3:8b`.

If AbstractCore on the gateway host is missing or too old for these routes,
both tabs show a card with the installed version and the upgrade command; the
rest of the console keeps working.

### Apps tab

The Apps tab and the setup guide's Apps step show the same cards (the apps
themselves are described in [apps.md](./apps.md)). What a plain user sees on
each card: the app's mark, name and status badge (Not installed, Stopped,
Running, Installing, Stopped unexpectedly, Keeps crashing), one line of
description (hover it for the whole sentence), and one row of buttons. The
button rows of the cards side by side are always at the same height. Above
the cards, one line names where the apps open: this browser's own address
plus `/apps/` (for example `https://gateway.example.com/apps/…`), whatever
address the browser used to reach the gateway.

| The app is | The action row |
|---|---|
| not installed | **Install** (installs Node.js first when the gateway needs it, then the app and, for Code when a ready-made download exists for this computer, its terminal app too; nothing opens by itself) |
| installing | a progress bar above the row (with one row per part: "Code in the browser", "Code in the terminal"), and **Cancel** (it stops both) |
| an install failed | the reason, **Show details**, and **Install** again |
| installed and running | **Open** (a new tab at `/apps/<app>/`, already signed in) |
| installed but stopped, or crashed | **Open** (starts it, then opens it); a crash also shows the reason, with **Show details** |

The status badge is the start/stop control (R11.3), one click as Install is:
**Running** stops the app ("Running — click to stop"), **Stopped** (or a
crash) starts it without opening a tab ("Stopped — click to start"); the
Assistant's **Running** quits the Assistant this gateway opened ("Running —
click to quit") and its **Stopped** opens it. The badge is a button with the
kit tooltip, reachable with Tab (Enter or Space clicks it); it reads
"Stopping…" / "Starting…" while its request runs, and a refusal shows on the
card with **Show details**. Disabled (still focusable, the tooltip says why):
an app started outside the gateway ("Started outside the gateway — stop it
where it was started") and every badge for a user who is not an admin ("Only
an admin can start or stop apps"). There is no separate Stop or Start button,
not even under **Technical details**. The words come from the gateway
(`status_control` on each row, [apps.md](./apps.md#http-api)).

Code has a terminal version too. Next to Code's Open: **Open in Terminal**
when the terminal version is installed and the browser is on the gateway
machine (a new terminal window opens there, signed in). The plain view never
installs the terminal version on its own: Code's Install installs both. When
the terminal version needs the Rust toolchain, or when the browser is on
another computer, the plain view shows no terminal button (the commands are
under Technical details, as is "Install terminal app" for a browser app that
is installed without it).

Below the cards, the **Skills shelf** block shows the folder the gateway
reads skills from, where that folder comes from (saved setting, the gateway's
own copy, the environment, or a framework checkout), the version of the
curated shelf shipped with this gateway, and how many skills the shelf holds.
Admins can type another folder and **Save skills shelf**, or **Refresh the
curated shelf** (new and updated curated skills are copied in; your own edits
are kept). See [configuration.md](./configuration.md#skills-shelf).

The last card is the **Assistant**, the desktop app: **Install** when it is
not on the gateway's computer, then **Open** (it starts in the menu bar of the
gateway's computer, or comes to the front when it already runs). From another
computer the card says "The Assistant runs on the gateway's computer: open it
there." with no button. See [apps.md](./apps.md#the-assistant-a-desktop-app).

A link to `/apps/<app>/…` opened while signed out (a bookmark, a shared
link) lands on the console (`/console#apps?open=<app>&path=…`): once you are
signed in, the console opens that app in the same tab, on the page the link
named.

A result box appears only after something you did ("Code opened in a new
Terminal window, signed in to this gateway.", "Flow Editor opened in a new
tab.") and closes itself after a few seconds. A failure stays, with the
gateway's reason and **Show details** (the full response or log).

The **Technical details** switch (bottom left of the console, and in the
guide) adds a secondary line under each card's buttons, and removes it again
when switched off:

- **Stop** (a running app), **Start** (start without opening), **Show log** /
  **Hide log** (the app's log, the log file's path, **Show more** up to 5000
  lines), **Update to X** when a newer version is published, and the version.
- For Code's terminal version: its version, **Update terminal app to X**, and
  the exact command to copy (one line, **Copy**): the command that opens it,
  `cargo install abstractcode` when it needs the Rust toolchain, or, for a
  browser on another computer, the command to run there and the one-time
  `abstractcode login` line.
- The app's local address, the `npx @abstractframework/<app>` line, and the
  log of a finished install.
- For an app started outside the gateway (the development stack, `npx`, a
  service): "Started outside the gateway on port 3001" instead of Stop,
  Start, Show log and Update; the card shows the Running pill and **Open**
  like any running app ([apps.md](./apps.md#apps-started-outside-the-gateway)).

Installing, starting, stopping and updating need an admin; other users see the
buttons disabled with the reason on hover.

## Terminal console (`abstractgateway-console`)

The terminal console does what the web console does, through the same gateway
routes, with the same admin rules and confirmations. Use it over SSH on a
headless server, where it also runs the first-run setup guide.

Install it from crates.io (Rust 1.87 or newer):

```bash
cargo install abstractgateway-console
```

Connect it to a running gateway with its URL and your token. The admin token
is printed by `abstractgateway serve` when it starts:

```bash
abstractgateway-console --gateway-url http://127.0.0.1:8080 --token <token>
abstractgateway-console --help
```

`--url` is an alias of `--gateway-url`. Without the flag, the console uses
`ABSTRACTGATEWAY_URL` (legacy), else the address this computer's gateway
records in `~/.abstractframework/gateway.json` (a loopback address only), else
`http://127.0.0.1:8080`; when that gateway stops answering, the console reads
the file again and follows it to a new port. Give the token with `--token
<token>` or paste it on the Connection screen.
When sign-in fails, the Connection screen says whether no token was
sent or the token was rejected. When the gateway offers sign-in by email, the
screen shows **Forgot your token? Email me a sign-in code** under your gateway
user: the gateway emails an 8-digit code to the account's email address and the
screen says where it went (or why none was sent); **Use code** gives the console
a new token, shown once to copy (the old one stops working). A new code can be
requested after 30 seconds.

### Using the terminal console

The console works with the mouse and with the keyboard alike: everything you
can click has a key, and everything you can do from the keyboard has a control
you can click.

- **Navigation.** On a terminal of 120 columns and 32 rows or more, a grouped
  rail on the left lists the screens in the web console's sidebar order; on a
  smaller terminal, a one-row tab strip with `‹` `›` takes its place. Click a
  screen, press its key (below) or use `←` / `→` (previous and next screen,
  wrapping) from anywhere outside a text field.
- **Header.** The title shows the mode (browse or wizard). Signed in, the
  memory and compute line (`Mem 18.2 GiB / 64.0 GiB (28%) · GPU 3% · 1 model`,
  `Mem 28% · …` when narrow) opens **H Resources**, your identity opens
  **1 Connection**, **✦ Docs** opens the docs assistant and ☾ / ☼ switches the
  theme (on terminals narrower than 90 columns, the theme switch sits at the
  right end of the status bar).
- **Buttons.** Row actions are buttons in the web console's order: glyph
  buttons where the web shows icons (for example `@` Email, `⇄` OpenAI API,
  `≣` Logs, `◫` Workspaces, `⊜` Preferences, `↻` Rotate, `⊟` Archive), labelled
  buttons where the web shows labels. Hovering a button, or reaching it with
  `Tab`, shows its tooltip (the web's sentence and its key), and the status
  bar names the focused control. An action that cannot apply stays visible
  and faint; clicking it says why.
- **Tables.** Click a row to select it; `↑` / `↓`, PgUp / PgDn and Home / End
  move the selection, `Enter` runs the row's first action, and the wheel
  scrolls a long table. The selected row's buttons are reachable with `Tab`.
- **Switches** read `━●` on and `●─` off next to their feature's name; a click
  or Space switches them, and they apply at once. **Choices** are segmented
  rows of options (one click each, one `Tab` stop per option) or pickers that
  open a list.
- **Dialogs** have a title with `✕` and edit several settings at once, with
  the web console's apply model: rows that apply on their own say "Saving…",
  "Saved" or the gateway's sentence followed by "Not saved."; forms with a
  **Save** button save together. Closing a dialog with unsaved edits (Close,
  `✕` or `Esc`) asks **Discard changes?** with [Discard] [Keep editing].
- **Confirmations** show the web's question and two buttons named after what
  they do, for example [Rotate] [Cancel]. A destructive question opens with
  the focus on Cancel, so `Enter` keeps things as they are; `Esc` and Cancel
  also keep them. A long question scrolls and its buttons stay on screen.
- **Results.** A verified change shows a short message in the corner; a
  refusal stays next to the control, in the gateway's words.
- **Themes.** `--theme gateway-dark` (default) or `--theme gateway-light`,
  built from the web console's palettes; ☾ / ☼ or `Ctrl+T` switches between
  them.
- **Keys.** `?` (or the **?** button at the right end of the status bar)
  lists every key of the current screen, each next to the control it
  presses, with **Quit the console**. Screens: **1** Connection, **2** Accounts,
  **3** Workflows, **4** Skills & MCP, **5** Runtimes, **6** Apps,
  **7** Providers, **8** OpenAI API, **9** Models, **0** Multimodal,
  **H** Resources, **T** Sandbox, **N** Network, **S** Setup, **I** About
  (`W` opens Accounts). Also `Tab` / `Shift+Tab` focus, `Enter` press,
  `Esc` close or back (in a text field, the first `Esc` releases it),
  `Ctrl+N` / `Ctrl+P` next and previous step, `Ctrl+G` setup guide, `F1`
  About, `F2` docs assistant, `F3` gateway host panel, `r` refresh, `Ctrl+L`
  repaint, `q` / `Ctrl+C` quit. In a text field, `←` / `→` move the caret
  and the screen stays; in the setup guide, `Ctrl+N` walks the steps.

### Setup guide and browse mode

- **Setup guide.** For an admin whose first run is not completed, the console
  opens the setup guide: Connection → Setup → Providers (local engines, then
  cloud keys) → Multimodal → Models → Apps → Sandbox, the same steps as the web
  console's first-run guide. No step is gated except signing in. The guide's
  row above the status bar names the step and its goal, with **‹ Back**
  (`Ctrl+P`), **Next ›** (`Ctrl+N`) and **Steps…** (`Ctrl+G`: go to a step,
  leave for now, or skip setup).
- **Browse mode** (free tabs) opens otherwise. `--wizard` and `--browse` choose
  the mode at launch.

### Screens and panels

- **1 Connection** is the terminal's sign-in. Type the **Gateway address** and
  your **Token**, then press **Sign in** (`Enter`). **Show** / **Hide** reveals
  the token. Once you are connected, the button reads **Re-probe** (`r`), and
  **Network** (`N`) beside the one-line network summary opens the Network page.
  When sign-in fails, the screen says whether no token was sent or the token was
  rejected. When the gateway offers sign-in by email, the link **Forgot your
  token? Email me a sign-in code** sends an 8-digit code to the account's email
  address and says where it went (or why none was sent). Type the code under
  **Code from the email**: **Use code** stays unavailable until it has 8 digits.
  **Send a new code** works again after 30 seconds, and **Back to token**
  returns to the token field. The new token is shown once, with **Copy** and
  **Done**; the old token stops working.

- **2 Accounts** is the web console's Accounts page. The head has
  **Eligible workspaces** (`E`, admins), the **Show archived** switch (`h`;
  admins: archived accounts, members: the archived entities they created — with ≣ Logs and ⤒ Unarchive), **Create user**
  (`a`) and **Create entity** (`n`). Under it, every signed-in user sees the
  command sandbox state line (for example **Commands sandboxed: macOS
  sandbox-exec**) with its explanation. The table has the columns **Name**
  (and kind: Admin, Member or Entity — the two roles are admin and member), **Email** (the address and the mailbox
  state, for example `alice@example.com · connected`, or "No address"),
  **Runtime** (a link that opens **5 Runtimes** filtered to that account; `g`),
  **Active** (a switch; Space: deactivating a user or suspending an entity asks
  first, and your own row says why it can't) and **Actions**: `@` Email, `⇄`
  OpenAI API (`o`, admin, user rows), `≣` Logs (`l`), `◫` Workspaces (`w`),
  `⊜` Preferences (`p`), `⬖` Manage (`m`, entities), `↻` Rotate token (`t`,
  admin) and `⊟` Archive (`d`) or `⤒` Unarchive. Rotate and Archive ask the
  web's question ([Rotate] [Cancel], [Archive] [Cancel]). **Logs** opens
  **Activity — <id>** with the filter [All] [Sign-ins] [Runs] [Automations]
  [Email] and **Open in Observer** for a run. Also: `e` edit user, `c` talk
  with an entity, `i` inspect, `s` spark templates, `v` **Retained runtimes**
  (transfer one to a user; the data is never deleted) and `x` reset an old
  per-user mailbox override. **Email for everyone** (admin) is a card under
  the table with the three switches **Mailboxes for users**, **Agent email
  tools for users** and **Sign-in by email**, each applied at once. Someone who
  is not an administrator sees their own row and the entities they created,
  and configures those entities: the Active switch, Archive, Unarchive and, in
  **Manage**, the mind, voice, tools per phase and instructions (the gateway's
  `GET /entities/{name}/access` decides; anyone else reads the gateway's
  sentence "Only an admin or <entity>'s creator can change its settings.").
- **Email** (`@`): on your own row, **Email — <you>** with your **Email
  address**, your **Mailbox** ([IMAP] [Google] [Microsoft]; the IMAP pane
  shows the incoming and outgoing servers, filled in as soon as the address has
  a domain; the links **My provider uses a different login name** (`Ctrl+O`)
  and, when your address is already set, **Use a different account**
  (`Ctrl+U`) reveal the Login and Mailbox address fields), once connected the mailbox's **Active** switch, Test and
  Disconnect, the **Job failed** and **Approval needed** notifications with
  **Send a test**, the **Agent email tools** switch and **Recipients and
  limits** (recipient rules, send limits, folder) — see [email.md](./email.md).
  On another user's row, their **Email address** with **Save** ("Saved:
  <id>'s codes and notifications go to <address>.") and a read-only mailbox
  line; on an entity's row, the entity's own mailbox form.
- **Workspaces** are the web console's two dialogs ([Workspaces](#workspaces)),
  with the same words. **Eligible workspaces** (admins) has the posture
  (**Deny everything, allow listed workspaces** or **Allow everything, refuse
  listed workspaces**), **Everything else** under the second posture, the
  listed workspaces under **Allowed workspaces** and **Refused workspaces**,
  each with its mode (**Read & write**, **Read-only** or **Refused**) and a
  ⊟ remove button, the gateway's built-in refusals as fixed rows, **Add a
  workspace path** with **Add**, and the gateway's summary line. `◫` on any
  Accounts row opens **Workspaces — <id>**: the line "Gateway: …", the
  **Follow the gateway policy** switch, then the same posture and rows for that
  account. A mode above the gateway's limit is unavailable, with the reason
  under the row ("The gateway allows this workspace read-only"). Each change is
  saved at once: **Saved**, or the gateway's sentence followed by "Not saved."
- **Preferences** (`⊜` on an Accounts row) opens **Preferences — <id>**: a
  picker per app, listing **Gateway default (<name>)** first and then every
  workflow the gateway offers for that app; a pick saves at once ("Saved.", or
  "Not saved." with the gateway's reason). The last row is **Time zone**, a
  searchable list: click it (or `Enter`) for **Gateway default (<zone>)** and
  the gateway's time zone names, type to filter ("Search time zones"), click a
  name (or `Enter`) to save at once. Hovering its label shows the gateway's
  help. After it, **Spoken language** lists **Auto (detected)** and the
  languages the gateway serves; click it (or `Enter`), then click a language
  (or `Enter`) to save at once ("Saved.", or "Not saved." with the gateway's
  reason). A gateway older than this route has no Preferences on its rows, and the
  button says so.

- **3 Workflows** is one page, as on the web. The head has **↻** (`r`, "Reload
  the workflow list") and **Import .flow** (`i`). Under it are the search field
  ("Search by name, description or id", `/` says where it is) and the switches
  **Drafts** (`t`), **Older versions** (`o`) and **Show archived** (`h`). The
  table groups the bundles under **Shared with everyone** and **Mine**. Its
  columns are Name, What it does, Version, Source, Used by and, for an admin,
  **Available to users** (a switch; Space). On a narrow terminal, what it does,
  the source and the users go on a second line. Each row's glyph buttons, with
  the web's tooltips, are ⤓ Export (`x`, writes the `.flow` on this machine and
  says where), ⇗ Open in AbstractFlow (`f`), ⊟ Archive / ⤒ Unarchive (`d`) and
  ✎ Edit description (`e`). With **Older versions** on, every older version is
  its own row with its own buttons. Archive asks the web's question with
  [Archive] [Cancel]; workflows are never deleted. **Description of <name>** is
  a dialog with **Save** and **Close**: empty returns to the file's own
  description, and closing with an unsaved edit asks "Discard changes?"
  ([Discard] [Keep editing]). Below the table, **Default workflow per app**
  (admin) has a picker for each app that saves at once. Further down are
  **Other workflow types** and **Settings** with the **Streamed replies**
  switch. **⚠ Broken workflows** lists versions the gateway could not load,
  with the reason and an **Archive** button.

- **4 Skills & MCP** has **[Skills] [MCP servers]** in its head (`[` / `]`
  switch them).
  - **Skills:** the search field, **Show archived** (`h`), **Import .zip**
    (`i`) and **Import folder**, and the **Shelf folder** row. Edit the folder
    in place: `Enter` saves, `Esc` keeps the saved one, and empty means the
    gateway's own copy. **Refresh curated shelf** (`u`) is on the same row. The
    table's columns are Name, What it does, Version, Trust and Source. Each row
    has **View** (`v`, also `Enter`), **Export** (`x`, a `.zip` on this
    machine) and, for an imported skill, **Archive** / **Unarchive** (`d`).
    **Skill — <name>** is a dialog: an imported skill has **Save**, a curated
    one **Duplicate to edit**, an archived one **Unarchive**, plus **Close**.
    An unsaved edit asks "Discard changes?".
  - **MCP servers:** **Show archived** and **Add server** (`a`). Each row shows
    the transport, the status, the tools and the **Enabled for agents** switch
    (Space). Turning it on asks "Offer its N tools to your agents? …" with
    [Turn on] [Cancel], and needs a successful test first. Each row has **Edit**
    (`e`, also `Enter`), **Test** (`t`) and **Archive** / **Unarchive** (`d`).
    The server dialog has Name, Description, the Agents switch, **How to reach
    it** [Command] [URL] (command, working folder and arguments, or the URL with
    headers, **Add header** / **Remove**), then **Test connection**, **Save**
    and **Close**, with the same "Discard changes?" guard.
  - Writes are admin-only.

- **5 Runtimes** (admins) has the head **Retained runtimes** and **↻** (`r`,
  "Reload the runtime list"), the web's note, and the table Runtime, Kind,
  Owner, State, Size and Workspace.
  - **Opening a runtime:** a click on a row opens it (or `↑`/`↓` then
    `Enter`). The Workspace cell's link (**Eligible workspaces** on the default
    runtime, **Workspaces** on a one-owner user or entity runtime; `w`) opens
    the Workspaces dialog. Opened from an account (`g` on Accounts), the head
    shows **Account: <id> ×** (`x` lists every runtime again).
  - **The open runtime:** **▷ Runtime <id>** has the segments **Runs** |
    **Artifacts** | **Cache** | **Logs** and its own **↻**. Nothing below the
    table loads until you open a runtime, and each tab loads on first look.
  - **Runs (the default runtime):** the status dropdown (`f`), the search field
    (`Enter` searches) and the **root runs only** switch (`t`). The columns are
    Run, Workflow, Status, Node, Session, Updated, with **Inspect** (`i`) and,
    on a live run, **Steer** (`s`) and **Cancel** (`c`). **‹ Prev** / **Next ›**
    (`p` / `n`) page through the runs. **Inspect** opens the run's rows in a
    dialog. **Steer run** takes guidance: [Send guidance] [Cancel], and closing
    with typed guidance asks "Discard changes?". **Cancel** asks "Cancel run
    <id>? Any in-flight work stops at the next tick." with [Cancel run]
    [Cancel]. Any other runtime is a read-only view with the web's note and no
    buttons.
  - **Artifacts and Logs:** a dropdown and a search field over their tables.
    The name opens the preview, or the log tail (`o`). The log dialog has
    **Show** [last 64 KB | last 256 KB | last 1 MB], **↻** and **Close**.
  - **Cache:** the disposable caches with **× Purge…** (`P`), plus **Stale
    registrations** with **× Forget** and **× Forget all stale** (`F`). Purge
    runs the dry run first, then asks "Purge <name>? This deletes the CONTENTS
    of <name>: N files, X freed. …" with [Purge] [Cancel]. A count the gateway
    does not report reads as unknown. A refused dry run purges nothing and says
    why. Forget asks first, with [Forget] [Cancel]; disk is never touched.
  - **Retained runtimes** lets you transfer a deleted or reassigned user's
    runtime to a new owner.

- **6 Apps** is the web console's Apps tab. The head has **Check again**
  (`r`) and the ⊛ **Apps settings** gear (`a`: Node.js for apps, ports, npm
  registry, Node.js download index; each row applies on its own). The table
  has the columns **App**, **Status**, **Version** (with "latest ⇡" when an
  update exists) and **Actions**. The status badge is the start/stop control,
  as on the web card: its label is the state and its tooltip the action
  ("Running — click to stop"; `s`). An app started outside the gateway, or a
  user who is not an admin, sees why it cannot be switched here. The labelled
  buttons follow the web card: **Install** / **Open** (`o`; Code also has
  **>_ Open in Terminal**), **Update to x.y.z** (`u`, with the gateway's
  tooltip as the question), **Show log** (`l`), **Cancel** (`c`) during an
  install, and Continuum's ⊛ settings (`g`: backlog folder, backlog exec
  runner, process manager). Installs ask first ([Install] [Not now]); **Install
  Node.js** sits under the table when the gateway needs it. Over SSH, or on a
  machine without a display, **Open** never starts a browser: it shows the
  one-time link to copy (`y`) and the `ssh -L` port forwards that make it work
  from your own computer.

- **7 Providers** stacks the web's three sections on one scrolling page.
  - **Local providers** has a summary line and **Check again** (`k`), then the
    table Engine, Status, Connection, with the engine card's buttons:
    **Install** / **Install for all users** (`i`), **Start** (`s`), **Stop**
    (`x`), **Browse models** (`b`), **Cancel** (`c`), **Set up connection** /
    **Add connection** (`n`), **Edit** or **Override** (`o`), **Download page**
    (`w`), **Learn more** (`l`) and **Show details** (`g`). The card's sentences
    sit under each row. An install asks first with the plan the gateway
    reports: [Install now] [Not now] for a wheel or script install, the
    location's sentence for an app install.
  - **Remote providers** has one row per preset, with **Configure** (`p`).
  - **Available Providers** has the columns Name, Provider ID, Type, Models,
    Status, with **Edit** (`e`), **Delete** (`d`) or **Override**, **Models**
    (`m`) and **Test** (`t`), plus **Add connection** (`a`). Delete asks the
    web's question with [Delete endpoint] [Cancel].
  - **Configure <provider>** is the web's endpoint dialog. It has Provider
    type, **Who can use it?** [Gateway-wide] [Only me], Provider ID, Name,
    Description, Base URL, API key (blank keeps the stored key), the **Clear
    stored API key** and **Enabled** switches, and **Visible models** with
    **Clear restriction**. Its buttons are **Cancel**, **↻ Test** and
    **✓ Confirm**, and closing with an edit asks "Discard changes?".
  - **Models — <provider>** lists the models a connection serves.

- **8 OpenAI API** ("Let apps use your models through one OpenAI-compatible
  address") shows the web's cards in one scrolling region.
  - **Status:** the state, the Base URL with **Copy** (`b`), the **Endpoint**
    switch (`e`), **Restart** (`x`) and **Check setup** (`h`), with the check's
    rows.
  - **Connect your app:** the Base URL and **API keys** with **New key**
    (`n`); one key can serve every app. New key asks for a **Name** ([Make
    key] [Cancel]); the key goes straight to the clipboard ("Key “<name>” made
    and copied to the clipboard.") and stays shown with **Copy** ("Copied" for
    2 s) and **Hide** until Hide: a resize or a trip to another page does not
    lose it. Under the cards, **Your API keys** lists each key's name, when it
    was made, when it was last used and from where, and its fingerprint, with
    **Reveal** (`⦿`, `v`) and **Copy** (`y`) — your key again, each reveal in
    the audit log; refused with the reason when an admin turned owner reveal
    off or the key is hash-only — and **Revoke** (`d`): "Revoke “<name>”? Apps
    using it stop working at once. …" [Revoke] [Cancel], focus on Cancel.
    Signed in with the gateway's own token (no account), the row says named
    keys belong to an account.
  - **Access** (admin): **Authentication** [Protected (API key)] [Open (no
    key)] (`a`), and the pickers **Requests without a key run as** (`u`) and
    **Who can connect** (`w`), each with its options' sentences, and the
    **API keys can be revealed by their owner** toggle. A listener warning
    comes with **Network**.
  - **Docs:** the links **OpenAI API compatibility** and **AbstractCore
    server**, then [curl] [Python] [JavaScript] (`s`) and **Copy example**
    (`c`); the example shows `<your key>` until a key is made or revealed,
    then that key.
  - **Recent requests** is a table: Time, Client (with the API key's name
    under it), Model, Tokens, Latency, Status, Run. The Time link (`Enter`, `f`) opens the request's record, with
    **Copy request**, **Copy response**, **Open in Observer** and **Close**.
    The Run link (`o`) opens Observer.
  - The Endpoint, Restart, Check setup and Access controls are admin-only.

- **9 Models** browses the models that fit this computer, downloads them and deletes downloaded ones. The head shows **This computer: …** with **Check again** (`r`), the **Catalog** | **Hugging Face** choice (Hugging Face mode has a search field and **Search**), the search field (`/`), the **Fits this computer** switch (`f`) and four pickers: **Quantization**, **Provider**, **Capability** and **Status**. A count line says how many models and artifacts are shown.
  One table lists each model under its own heading line (name, capabilities, **Starter** / **Hugging Face**), one row per artifact: **Artifact**, **Provider**, **Quant**, **Size**, **Weights** (Downloaded / Not downloaded), **Fit** and **Actions**. On a narrow terminal, Provider, Quant, Size and Fit move to a second line under the artifact. Downloaded models that no catalog entry knows are listed under **Not in the catalog**.
  Row buttons: **Download** (`w`; **Try again** after a failure), **Use as default** (`u`, a text model; the current one reads **Default text model**), the trash ⌫ (`d`; tooltip "Delete <artifact> from this computer (files only)") and, while a download runs, **Cancel** (`c`). `Enter` runs the row's first button; `i` shows the row's details.
  Deleting first asks the gateway what it would free, then asks "Deletes <size> from this computer. Files only — nothing in your runs is touched." **[Delete] [Keep]**; the focus starts on Keep. Cancelling asks "Stop this download?" **[Stop download] [Keep downloading]**.
  Downloading, deleting and changing the default are admin-only. For anyone else these buttons stay visible but faint, and pressing one says why ("Only an admin can download models"). An artifact this computer cannot run reads **Not available here**, with the reason in its tooltip. "No model matches these filters." comes with **Clear filters** (`x`).

- **0 Multimodal** (**Multimodal Capabilities**) shows which provider and model serves each capability route. The head has **Apply recommended** (`a`, admin) and **↻ Refresh** (`r`), then the scope sentence. When a route has no model, a banner names it and offers **⤓ Download missing** (`m`). **Recommended for this computer** (`p`) carries **Download all** (`D`).
  Under the **Transcription** line, **Spoken language** is your own account's spoken language, as on the web: the same list and words as in Preferences, a pick saves at once ("Saved." / "Not saved." with the gateway's reason), and hovering the label shows the gateway's help.
  When the gateway names the AbstractCore store file, a line says so: "AbstractCore store · <file> — shared with AbstractCore — edits here apply to AbstractCore directly" (a read-only store or a runtime overlay says so instead).
  The table has the columns **Route**, **Capability**, **Provider**, **Model**, **Weights**, **Source**, **Status** and **Actions**. On a narrow terminal it shows Route, Model, Weights and Actions, and Capability, Provider and Status move to the row's second line.
  The **Weights** pill reads installed, not downloaded, remote or unknown; its tooltip gives the gateway's sentence and "To fix: …". Model ids wrap at `/` or `-`.
  Row buttons, each with a tooltip: ✎ **Edit** or ⊞ **Configure** (`e`, also `Enter`), ⌀ **Clear** (`x`), ⤓ **Download** (`w`, admin; the tooltip names the artifact and provider) and ⧉ **Copy** (`c`, the install command when there is no download tool). A route that a parent route covers reads "Covered by input.text".
  **Configure capability default** is one dialog with:
  - the choice between use default and override;
  - **Provider**, **Model**, **Base URL (optional)**, **Reasoning** (text route), **Options (JSON, optional)**, **MTP**, and **Voice** on output.voice;
  - the buttons **[Cancel] [Clear] [Test] [Save]**.
  Cancel, ✕ and Esc ask "Discard changes?" when you have edited something. A configured model that is not in the provider's discovered list yet stays selected, under the sentence "Configured model "<m>" is not currently in the discovered <scope> catalog for <provider>." with a **Download** button. When discovery fails, the field takes a typed model id and offers **Retry model discovery**. Clear asks first; downloads ask first and name the size.

- **H Resources** shows three cards on one scrolling page. The page follows the focus when it moves off-screen.
  - **◎ Gateway** (admins) shows the state pill. **Workflows paused** (`p`) and **Start at login** (`L`) are switches. **Version** sits beside **Check now** (`U`), plus **Update** when one is available ("Update now" / "Not now"). It also shows **Desktop icon**, **Last restart** after a watchdog restart (its tooltip names the frame, the stack dump and the incident file), and **Restart gateway…** (`R`: "Restart AbstractGateway? …" **[Restart] [Cancel]**) and **Quit gateway…** (`Q`: **[Quit] [Cancel]**).
  - **▦ Memory & GPU** shows the meters and the itemization; `m` pages it on a short terminal.
  - **▣ Models** has the **Show configured / cached** switch (`a`; resident models only by default) and **Load model** (`w`, a dialog with Provider, Model, the **lock in memory** switch and **[Cancel] [Load model]**). Its table has the columns **Modality**, **Provider**, **Model**, **Resident**, **Size**, **Context**, **Flags** and **Actions**. Row buttons: **Estimate** (`e`), **Lock** / **Unlock** (`k`) and **Unload** (`u`).
    - Unload asks **[Unload] [Cancel]**; a locked model then asks **[Force unload] [Cancel]**.
    - Unlock asks **[Unlock] [Cancel]**.
  - **⌸ Session caches** has the columns **Session**, **Model**, **Size**, **Tokens**, **Created** and **Actions**, with **Clear** (`c`; **[Clear] [Cancel]**).
  Every confirm opens with the focus on Cancel. Everything except Estimate is admin-only; for anyone else these buttons stay faint with the reason.

- **T Sandbox** tries a model directly. **Output** is a choice: **Text**, **Image**, **Voice**, **Music**, **SFX** or **Video**. Each choice's tooltip names the provider and model it uses, or says "not configured". A line says what the next message generates and which route it comes from.
  **System prompt**, **Reasoning** and **MTP** sit on one row, with the web's help sentences as tooltips. **Length (seconds)** appears for Music and SFX: 0.5 to 600, remembered per mode.
  Under the message field, the buttons are **Send** (`g`, or `Enter` in the field), **Attach** (`a`), **Speak** (`v`, speaks a text reply through output.voice), **Play audio** (`p`) and **Stop** (`s`) after a clip, and **Clear chat** (`x`).
  **Attach a file** is a dialog with **[Upload] [Remove all] [Close]**. When Sandbox refuses an action, it says why under the buttons (for example "output.music is not configured — configure it on Multimodal (0) first"). The session's change journal sits below.

- **N Network** shows who can reach the gateway: **Running now: <mode> · port N** above three choices, **Localhost only** / **Local network** / **Internet** (the gateway's labels), each with its sentence. The saved choice is highlighted. A mode that needs accounts reads **Needs accounts**; choosing it shows the reason and how to fix it.
  Choosing Internet asks "Before you open the gateway to the internet" with the gateway's warnings, **[I understand, use Internet mode] [Keep <saved mode>]**; the focus starts on Keep, and a long list of warnings scrolls. A saved mode that needs a restart says "Restart to apply: …" with **Restart now**.
  **Addresses** is a table with the columns **Address**, **URL**, **Status** (Works now / Not in this mode / Through your proxy only) and **Actions**. Each address a client can use has **Copy** (`c`, also `Enter`), and each row's note sits under it. Below the table are **Look up my public address** (Internet mode) and **Check again** (`r`).
  **What to know about <mode>** (`w`) opens the warnings. **Reached through another address?** holds **Allowed origins**, with **Remove** per origin and a field with **Add origin**, and **Client address**, with the **Trust proxies on other machines** switch. Changes apply to the next request. **OpenAI API** (`o`) opens that page.
  Changes are admin-only; anyone else sees "Only an admin can change who can reach this gateway."

- **S Setup** is the setup guide's welcome step, also shown in browse mode.
  - **Head:** "Welcome to your gateway" ("Check this computer"). In browse mode
    the head has **Setup guide** (admins; `Ctrl+G`). Inside the guide it has
    **Go to a step**, **Skip setup** ("Close the guide and do not open it
    automatically again") and **Next** (`Ctrl+N`). **↻** (`r`) is always
    there.
  - **This computer:** the first-run state and this computer's tiles: computer,
    memory, graphics, data folder, sign-in mode, and whether the gateway starts
    at login.
  - **What this guide sets up:** cards for **Local engines**, **Choose your
    default model** and **Apps**, each with a **Go to …** button.
  - **Recommended for this computer:** each recommended route with its status,
    engine, model and any fit or engine warning, and the text model in use.
    **Use recommended defaults** (`a`) keeps the routes you chose, then offers
    **♻ Replace mine too** (or **♻ Clear what cannot run here**) under the
    head. **Download all** (`D`) starts every missing recommended model as one
    job. Both are admin-only.
  - **`Ctrl+G` in the guide** opens **Setup guide**: one button per step (the
    current one marked "(you are here)"), then **Leave for now** (nothing is
    recorded; the guide opens again next start), **Skip setup** (recorded) and
    **Close**.
  - **The last step** ends with **Finish** (a non-admin sees **Leave the
    guide**), **Skip setup** and the **Start at login** switch.

- **I About** (and `F1`, the **About AbstractGateway** dialog with **Close**) shows this console's name and version, the AbstractFramework and AbstractGateway versions the gateway reports, the six links and the licence line. **Website** (`w`), **Source** (`s`), **Docs** (`d`), **Issues** (`i`), **Feedback** (`f`) and **Contact** (`c`, a mailto: link) open in your browser; each tooltip is the address. Without a display, the status bar gives the address to open yourself.

- **Docs assistant** (`F2`, or **✦ Docs** in the header, signed in) is a drawer on the right edge. It takes 48% of the width from 120 columns and the full width below. Esc or ✕ closes it, and the conversation is kept.
  The head has **Past conversations** (`h`) and **New conversation** (`n`, tooltip "Start a new conversation"). The empty drawer reads "Ask anything about AbstractGateway." with three suggestions you can click to ask. Type in **Ask about the gateway…** and press `Enter` or **Send**; while it answers, the button reads **Answering…** and **Stop** ("Stop the answer") ends it with "Stopped. Nothing more will be shown for this question."
  Past conversations lists the web drawer's conversations and the terminal's together, newest first, with the time and "· N questions". A click on one reopens it and continues in it. The ⊟ Archive button asks "Archive this conversation? It stays in the gateway; it leaves this list." **[Archive] [Cancel]**. The footer reads "Grounded on AbstractGateway's documentation (llms.txt) · docs-qa", and while the drawer is open the status bar lists its keys.

- **F3** opens the gateway host panel: the web's **Gateway** card ("How this
  gateway is running right now. …").
  - **Rows:** Workflows (the runner's state and detail), **Last restart** after
    a watchdog restart (the hang's reason, the stack dump's path, how long the
    gateway was blocked and where, the incident file), Version with its hint,
    **Desktop icon** and **Start at login**. The rows scroll when the terminal
    is short.
  - **Switches:** **Workflows paused** (`p`) and **Start at login** (`L`). The
    Start at login switch asks first, then is read back from the gateway.
  - **Buttons:** **Check now** (`u`), **Update** (`U`, when a check found one),
    **Restart gateway…** (`R`), **Quit gateway…** (`Q`) and **Close**. Restart
    and Quit are refused with the gateway's reason when this launch cannot do
    them.
  - **Questions:** each opens over the panel, and Cancel returns to the panel.
    - Restart: "Restart AbstractGateway? Running workflows pause at their next
      step and continue after the restart. The console is unavailable for a few
      seconds." with [Restart] [Cancel].
    - Quit: "Quit AbstractGateway? Workflows stop and this console goes offline
      until you start AbstractGateway again." with [Quit] [Cancel].
    - Update: the gateway's own sentence with [Update now] [Cancel].
    - Restart and Quit open on Cancel, so `Enter` keeps the gateway running.
  - **Admin-only:** the verbs and the Version row. Anyone else sees the card
    with "only an admin can pause, restart, quit or update this gateway".
  - **Paused banner:** shows on every screen while workflows are paused.

- **Manage entity** (⬖ **Manage**, `m`, on an entity's Accounts row; tooltip "Manage <name> (mind, voice, prompt…)") opens **Manage — <name>**, one dialog with the web's six tabs. Each tab's cards hold their own fields:
  - **Overview**: Right now, Identity with **Verify memory** and **Reload**, and memories from sleep with **Promote (accept)**, **Reject** and **Reload**.
  - **Talk**: **Open visit**, **Send** and **Close visit**.
  - **Lifecycle**: **Awake or asleep** (awake / asleep / asleep + dream pass / paused) applies at once. Sleep asks "Put it to sleep? …" **[Sleep] [Cancel]**; paused asks **[Pause] [Cancel]**. **Personal time** is a switch with its schedule, **Grant (timer)** and **Revoke grant**. **Emergency freeze** has **Freeze now**, which asks **[Freeze] [Cancel]**.
  - **Mind & voice**: Mind (**Save**), Voice (**Hear a sample**, **Play**, **Save**) and **Rebuild index**, which asks **[Rebuild] [Cancel]**.
  - **Work & tools**: Work order (**Give this task**, **End the work order**) and Tools per phase (**Save**, the **Empty phase means no tools** switch).
  - **Prompt**: the layers you may rewrite (**Save**).
  Confirms open over Manage and return to the same tab. With unsaved edits, Close, Esc, ✕ and switching tab ask "Discard changes?" **[Discard] [Keep editing]**. A long tab scrolls. For anyone who is not an admin, the admin-only acts stay visible but faint, with the reason.

- **Create entity** (the Accounts head's **Create entity**, `n`) opens **Summon a new entity**:
  - **Name** (permanent) and **Template**, with the template's core values;
  - the always-visible **Optional configuration**: provider, model, reasoning and embedding at birth, each defaulting to the gateway's;
  - **[Cancel] [Validate & create]**.
  Validate runs a dry run, then asks before creating **[Summon] [Back to the form]**. Cancel with a name typed asks "Discard changes?".
  **Spark templates** (`s` on Accounts) lists the templates with **View**, **Edit** (operator templates only; the builtin is refused with the reason), **New from selected** and **Close**. The template editor saves with **Lint and save as a new version** (admin) and asks "Discard changes?" before dropping edits.

**Admin rules.** Admin-only actions stay visible for everyone else, faint, and
pressing one says why before anything is sent. The setup guide is admin-only,
as on the web. Every write is verified with a follow-up read and recorded in the
journal (**T Sandbox**).

The terminal console needs no gateway-side component beyond the admin API. The
crate version is independent of the Python package version; see
[`console-tui/CHANGELOG.md`](../console-tui/CHANGELOG.md).

## Model residency from a shell

The same model routes the consoles drive are available from the Python CLI
against a running gateway:

```bash
abstractgateway models loaded
abstractgateway models load   --provider ollama --model qwen3:4b
abstractgateway models unload --provider ollama --model qwen3:4b
```

Without `--url`, the command finds this computer's gateway the same way every
local client does: the running gateway's address, else the installed service's,
else the port saved in the Network setting, else `http://127.0.0.1:8080`. Use
`--data-dir DIR` when the gateway keeps its data somewhere other than the
default, and `--url` (or `ABSTRACTGATEWAY_URL`) to reach another machine. The
token comes from `--token` or `ABSTRACTGATEWAY_AUTH_TOKEN`; for a gateway on
this computer it falls back to the data dir's bootstrap admin token. The command prints the gateway's JSON answer
and exits non-zero when the gateway reports a failure. `unload --force` unloads
a locked model; in-flight calls on the model are cancelled first.
