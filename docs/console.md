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
| Accounts | **Accounts** | every user and entity in one table: the **Active** switch, email, a link to each account's runtimes, activity (Logs), workspace folders, token rotation, entity management, the administrator's **Shared workspace & allowed folders** and **Email for everyone** switches ([below](#accounts)) |
| Work | **Workflows** | the workflow bundles on this gateway, shared by the gateway or your own (name, what each does, version, source, the apps that use it, availability to users), import, export, open in AbstractFlow, archive, the default workflow for each app and the **Streamed replies** setting ([below](#workflows)) |
| Work | **Skills & MCP** | administrators: **Skills** — the skills shelf setting and every skill (curated and imported, **Show archived**) with View, Export and Archive, import a `.zip` or folder, Duplicate to edit a curated one; **MCP servers** — whether agents are offered tools, an **Enabled for agents** switch per server, add, edit, test and archive ([configuration.md](./configuration.md#skills-shelf)) |
| Work | **Runtimes** | execution planes: runs (cancel, steer), sessions, data and caches; `#runtimes?account=<id>` lists one account's runtimes ([below](#runtimes-of-one-account)) |
| Work | **Apps** | the browser apps, Code's terminal app and the desktop Assistant (below), no settings disclosures: the toolbar gear opens **Apps settings** and the gear beside Continuum's Open opens **Continuum settings** (backlog folder, exec runner, process manager), each a dialog whose rows apply on their own |
| Models | **Providers** | **Local providers**: one card per local engine on the gateway host (Ollama, LM Studio, MLX, llama.cpp, Hugging Face, vLLM) with its status, install, start, stop, **Browse models** and its server connection; **Remote providers**: OpenAI, Anthropic, OpenRouter, Portkey and custom OpenAI-compatible connections with write-only keys (shown as fingerprints); then the **Available Providers** table ([below](#models-and-local-providers)) |
| Models | **OpenAI API** | the OpenAI-compatible API at `/v1`: status with Endpoint switch, Restart and Check setup; the base URL and your API key (your gateway token); Authentication and Who can connect, applied at once; supported surface and snippets; recent requests ([openai-api.md](./openai-api.md)) |
| Models | **Models** | browse models that fit this machine, download them, delete installed ones (below) |
| Models | **Multimodal** | capability route defaults, the text reasoning effort, the MTP (speculative decoding) default, and model weights per route |
| System | **Resources** | memory and GPU meters, resident models (warm up, lock, unload), session prompt caches, and the **Gateway** card (pause, update, restart, desktop icon, start at login) |
| System | **Sandbox** | quick chat and media generation against the configured defaults |
| System | **Network** | who can reach the gateway (localhost only, local network, internet), its addresses (with the Tailscale name when Tailscale runs), *Reached through another address?* (Tailscale, a reverse proxy: detected addresses are accepted automatically), and *Advanced*: manual allowed origins and *Trust proxies on other machines* ([configuration.md](./configuration.md#network-exposure-localhost--local-network--internet)) |

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
provider lists no voices. The cloud voice providers (OpenAI,
OpenAI-compatible) are always listed, marked "needs an API key" until you add
one under **Providers**.

The **Technical details** switch at the bottom of the sidebar shows commands,
route ids and other technical information throughout the console. The top bar
holds the **Docs assistant** (book icon: the same chat as every app — your question on the right, the answer on the left with Markdown, code and links, copy, attachments, live streaming, an icon-only New conversation; answers come from this gateway's llms.txt through the docs-qa workflow, as its one-line footer says; **Past conversations** reopens or archives an earlier docs chat, and docs chats never appear in the conversation lists),
the appearance settings, **About**, the gateway's address with a copy button,
who is signed in, and the sign-out control.

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
Administrators also have **Shared workspace & allowed folders** first in that
row: the folders every agent may use ([below](#workspace-folders)).

| Column | Shows |
|---|---|
| **Name** | the account id and a kind chip: **Admin**, **User** or **Entity** (hover it for the role) |
| **Email** | the account's address and its mailbox state in one line: `alice@example.org · connected`, `· not connected`, `· receive only` (with the reason under it) or `· paused`; "No address" when the account has neither a registered address nor a connected mailbox (users and entities alike: an entity has its own mailbox) |
| **Runtime** | the account's runtime id as a link to the Runtimes page filtered to that account (administrators; plain text for everyone else), or "No runtime" |
| **Active** | the switch described below; "Archived" on an archived row |
| **Actions** | icon buttons, each with a tooltip sentence: users **Email**, **OpenAI API**, **Logs**, **Workspace**, **Rotate token**, **Archive**; entities **Email**, **Logs**, **Workspace**, **Manage**, **Archive**; archived rows **Logs**, **Unarchive** |

Rows are tinted by kind (a legend under the table reads "Tint: admin · user ·
entity"). Only the actions that apply to a row are shown: your own row has no
**Archive** and an entity has no token to rotate. The action buttons are
44 px targets. Hover one, or reach it with the keyboard, and a tooltip says
what it does for whom: "Email address and mailbox of alice", "OpenAI API
access for alice", "Activity log of alice", "Workspace folders alice's agents
may use", "Manage castor (mind, voice, prompt…)", "Rotate alice's sign-in
token", "Archive alice (kept, hidden)". The same tooltip explains every icon
button in the console (Workflows, Models, Apps, the top bar…): it appears
after a short pause, stays inside the window and Escape hides it. The table never scrolls sideways: long names and
addresses wrap, and below about 900 px of window width each account becomes
one flat block: name, kind chip and Active, then the email line, then the
runtime, then the actions, which wrap.

**Show archived** (administrators, above the table) lists archived accounts too;
it is off by default and your browser remembers it.

- **Active** for a user: off signs them out and refuses their sign-in until you
  turn it back on. Turning it off asks first ("Deactivate alice? They are
  signed out until you turn Active back on."). Your own account and the last
  active admin cannot be switched off.
- **Active** for an entity: off suspends it (it stops acting and its gateway
  credential is switched off) after "Suspend castor? It stops acting until you
  turn Active back on."; on resumes it in the state it had before.
- **Email** opens a dialog. On your own row it holds your email settings: your
  email address, your mailbox, the **Job failed** and **Approval needed**
  notifications, **Agent email tools** and Advanced ([email.md](./email.md)).
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
  account's **OpenAI API** switch: on, the account's gateway token works as an
  API key at `/v1`; off, its requests answer `403 openai_api_off` (its console
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
- **Workspace** opens "Workspace folders — <id>", the folders that account's
  agents may use ([below](#workspace-folders)).
- **Rotate token** asks in a row under the account ("Rotate the token of
  alice? The current token stops working now; the new one is shown once."),
  then issues the new token and shows it once. Entities have no token to rotate.
- **Manage** (entities) opens the entity's lifecycle, mind and voice,
  capabilities and prompt; **Talk** is there too. **Mind & voice** uses the
  shared pickers: **Gateway default** (the gateway's text model and default
  voice) or its own choice; changes save themselves.
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
Create user, no Shared workspace & allowed folders and no Email for everyone;
their own row's **Workspace** opens their own folders; it has no **Rotate token**
(only an admin rotates tokens), they can archive an entity they created but not
unarchive it, and an entity's Active switch says "Only an admin can suspend an
entity." Archived accounts are not listed for them. See [security.md](./security.md#who-sees-which-account).


### Workspace folders

Agents read and write in folders the administrator allows; each account
turns on what it needs within that allowance
([security.md](./security.md) explains how the gateway enforces it).

**Shared workspace & allowed folders** (administrators, top of Accounts)
opens the gateway's folder policy. A line at the top sums it up, for example
"Agents may use the shared workspace + 2 allowed folders (each account turns
them on); accounts may not add folders of their own. Never allowed: 1 folder,
plus the gateway's own data and credential folders."

| Control | What it does |
|---|---|
| **Shared workspace** | the one folder every conversation, automation and entity works in (each gets its own folder inside it); always on for every account and required |
| **Allowed folders** | extra folders accounts may turn on for their agents (**Add folder**, the remove icon on each row); off for each account until turned on |
| **Allow any folder** | off by default; on, accounts may also add folders of their own |
| **Never allowed** | folders no agent may use, even inside an allowed folder |
| **Always refused** | the gateway's own data folder and credential folders (read-only) |
| **Launch-folder trust** | agents may also use the folder an app was started from |

**Workspace** on an account's row opens that account's folders (users,
entities and your own row): the **Shared workspace** (always on), one switch
per allowed folder (off until turned on), and **My folders** while the
administrator allows any folder (otherwise a sentence says when they appear).
The line at the top, "Agents may use: …", is the gateway's own summary of
what applies. **Follow the gateway policy** turns every allowed folder off and
removes the account's own folders, after asking. This is the same chooser, with
the same words, as the Workspace settings of AbstractCode and the Assistant.
An entity's folders are changed by an administrator; someone who is not an
administrator opens their own folders from their own row.

Every change applies at once; there is no Save button. A folder row applies
when you leave the field (or press Enter): the gateway first checks the path
(`POST /api/gateway/workspace/path-check`) and says in a sentence why it
cannot be used ("Use a full path that starts with / …", "No folder at this
path on the gateway's computer.", "This is a file, not a folder."); such a
row is not saved, and a refused change ends with "Not saved.". A saved row
shows "Saved" for a moment. The API is `GET`/`PUT /api/gateway/workspace/policy`
and `GET`/`PUT /api/gateway/workspace/policy/{account}`
([api.md](./api.md)).

### Runtimes of one account

The Runtime link on an Accounts row opens **Runtimes** at
`#runtimes?account=<id>` (`GET /api/gateway/admin/runtimes?account=<id>`): a
chip "Account: alice" names the filter, only that account's runtimes are
listed, and a single runtime opens at once. The link survives a reload; the
× on the chip shows every runtime again. The **Workspace** column opens the
folders that apply: **Shared workspace** (the gateway policy) for the default
runtime, **Folders** (the owner's Workspace folders) for a user's or an
entity's runtime.

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
each card: the app's mark, name and status pill (Not installed, Installed,
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

### Setup guide and browse mode

The console lists its pages in the web console's sidebar order, each with a
fixed key. **1** Connection (the terminal's sign-in) comes first, then the
groups: ACCOUNTS **2** Accounts, **W** Workspaces; WORK **3** Workflows, **4** Skills & MCP,
**5** Runtimes, **6** Apps; MODELS **7** Providers, **8** OpenAI API, **9**
Models, **0** Multimodal; SYSTEM **H** Resources, **T** Sandbox, **N**
Network; then **S** Setup and **I** About. A line above the tabs names the
groups and their keys. The local engines live on Providers, as on the web.

- **Setup guide.** For an admin whose first run is not completed, the console
  opens the setup guide: Connection → Setup → Providers (local engines, then
  cloud keys) → Multimodal → Models → Apps → Sandbox, the same steps as the web
  console's first-run guide. Multimodal shows the recommended models for this
  computer with fit warnings (`a` applies them, `D` downloads all of them as
  one job, `C` cancels, `p` shows the plan). Sandbox ends with **Finish** or
  **Skip setup**, recorded on the gateway, and a **Start at login** switch. No
  step is gated except signing in.
- **Browse mode** (free tabs) opens otherwise. `--wizard` and `--browse` choose
  the mode at launch.
- **`Ctrl+G`** reopens the guide from browse mode; inside the guide it opens
  the guide menu: go to any step, leave for now, or skip setup.
- **S Setup** shows this computer at a glance (memory, graphics, data folder,
  sign-in mode, whether the gateway starts at login, the first-run state) and
  **Recommended for this computer**: each recommended route with its status,
  the engine and model that run it and any fit or engine warning, the text
  model in use, `a` **Use recommended defaults** (keeps the routes you chose,
  then offers **Replace mine too** inline) and `D` **Download all** (asks
  first). Both are admin-only.

### Screens and panels

- **Multimodal** flags a route this computer cannot run (for example an MLX image
  route on Linux) with the reason, and a route whose engine is not installed
  ("engine missing", with the install command), like the web console. A model
  that is not on this computer reads "not downloaded — w: download"; `w` asks
  first, naming the model and its size, and the row updates by itself when the
  download finishes. The voice picker says why a provider lists no voices (for
  example "Supertonic is not installed … Install it with: …", or "OpenAI:
  needs an API key (add it under Providers)"). The transcription row names the
  route's engine and model, and says "Engine missing" only with the reason.
- **N Network** shows who can reach the gateway: the **saved** exposure
  (localhost only, local network, internet) next to what is **running now**,
  and every address to copy (`c`). `(•)` marks the saved mode; move the cursor
  and press `Enter` to save another (internet asks for an acknowledgement
  first). When the saved mode needs a restart that can apply it, the console
  offers the restart, reconnects when the gateway is back and reads the network
  again. When a restart cannot apply it, the gateway's reason is shown. The
  Connection screen keeps a one-line summary.
- **T Sandbox** holds the session's change journal and the sandbox: every
  output mode (text, image, voice, music, sound effects, video), file
  attachments and speak-this-reply.
- **2 Accounts** is the web console's Accounts page: one table of users and
  entities with the columns **Name** (and kind: Admin, User or Entity),
  **Email** (the address and the mailbox state, for example
  `alice@example.com · connected`, or "No address"), **Runtime** and **Active**
  (`[x]` on, `[ ]` off, `[-]` when it can't be switched here, "Archived"). Long
  cells wrap onto more lines; nothing scrolls sideways. The line under the table
  lists the selected row's actions with their keys, in the web console's order;
  `Enter` on a row shows the reason of every action that can't apply.
  **Show archived** (`h`, admin) lists archived accounts with Logs and
  Unarchive. Keys on the selected row: Space switches Active (deactivating a
  user or suspending an entity asks first, inline under the table), `@` email
  (your own row: your email settings, below; another user: their email address
  and a read-only mailbox line; an entity: the entity's own mailbox form), `o`
  OpenAI API (admin, user rows: one switch), `l` logs ("Activity — <id>", `f` /
  `F` change the filter: All, Sign-ins, Runs, Automations, Email; `o` opens a
  run in Observer), `w` opens **Workspaces** on that account, `t` rotate token,
  `m` manage (entities), `d` archive (asks inline) or unarchive, `g` opens
  **Runtimes** filtered to that account. Also `a` create user, `e`
  edit user, `n` create entity, `c` talk with an entity, `i` inspect, `s` spark
  templates, `v` retained runtimes (transfer one to a user; data is never deleted) and `x` reset an old per-user
  mailbox override. **Email for everyone** (admin) is the page's second tab
  (`Tab`): the three switches **Mailboxes for users**, **Agent email tools for
  users** and **Sign-in by email**, each applied at once. Someone who is not an administrator
  sees their own row and the entities they created.
- **W Workspaces** sets which folders agents may read and write. The top line
  says what the gateway policy allows and how many accounts have their own;
  the table lists the **Gateway policy** and every user account with its
  **Own policy** switch and one sentence ("Follows the gateway policy." or
  what its own policy allows). `Enter` edits the highlighted policy:
  **Access** is a segmented switch (**Allow my list** / **Allow everything
  except**; `←` / `→` or Space), **Launch-folder trust** is a switch, and the
  **Allowed folders** and **Refused folders** are rows: `Enter` edits a row in
  place, **+ Add folder** adds one, `x` removes one. The gateway policy also
  has **Default folder** and, like an account, **Any folder (old clients)**.
  The gateway checks a typed folder first; when it can't be used, or is
  already listed, it says so and nothing is saved. For an account, **Own
  policy** on starts from the gateway's access mode and launch-folder trust;
  off asks first, then the account follows the gateway policy again. Every
  change applies at once and says "Saved". Someone who is not an
  administrator sees their summary in one line and edits their own policy.
- **Your email settings** (`@` on your own row): your **Email address**, your
  **Mailbox** (tabs **IMAP**, the default, **Google** and **Microsoft**; the
  IMAP pane shows the incoming and outgoing servers, filled in as soon as the
  address has a domain and replaced by what discovery finds unless you edited
  them; `Ctrl+O` shows a Login field for providers that use a different login
  name; with an email address already saved the pane reads "Mailbox account:
  x@y" and `Ctrl+U` uses a different account; one **Connect**), once connected
  the mailbox's **Active** switch, Test and Disconnect, the **Job failed** and
  **Approval needed** notifications with **Send a test** (its answer is a
  sentence, for example "Not sent: hourly limit reached (100 of 100 this hour)
  — resets at 14:05."), the **Agent email tools** switch and Advanced
  (recipient rules, send limits, folder) — see [email.md](./email.md).
- **3 Workflows** is the web console's Workflows page, with three tabs
  (`Tab`). **Workflows** groups the bundles under **Shared with everyone** and
  **Mine**: name and id, what it does, version (+N older), source, the apps that
  use it and, for an admin, **Available to users**. `/` searches, `t` Drafts,
  `o` Older versions and `h` Show archived switch what is listed; with Older
  versions on, each older version is its own row with its own actions. The line
  under the table lists the selected row's actions: `x` exports that version as
  a `.flow` file on this machine, `f` opens it in AbstractFlow, `d` archives
  (it asks first; workflows are never deleted) or unarchives, `e` edits the
  description in place (shown when the gateway lets you: your own workflows,
  or the gateway's as an admin; `Enter` saves, `Esc` keeps, empty returns to
  the file's own description), Space switches **Available to users** (admin,
  shared workflows), and `i` imports `.flow` files from this machine. **Default workflow per app** (admin only) lists each app and
  what it runs; `Enter` picks a workflow and saves it at once, `o` shows
  **Other workflow types**, `s` switches **Streamed replies**. **Broken
  workflows** appears when the gateway could not load some versions, with the
  reason; `d` archives them.
- **4 Skills & MCP** is the web console's page of the same name, with two tabs
  (`Tab`). **Skills**: the shelf (name, what it does, version, trust, source);
  `/` searches, `h` Show archived, `v` opens a skill (an imported one can be
  edited and saved, a curated one duplicated to edit), `x` exports it as a
  `.zip` on this machine, `i` imports a `.zip` or a skill folder from this
  machine, `d` archives an imported skill or unarchives. One row under the list
  shows the shelf folder: `f` edits it in place (`Enter` saves, `Esc` keeps,
  empty = the gateway's own copy) and `u` runs **Refresh curated shelf**.
  **MCP servers**: each
  server's transport, status and tools; Space switches **Enabled for agents**
  (turning it on asks first; it needs a successful test), `a` adds and `e`
  edits a server (a command or a URL with headers, **Test connection**,
  **Save**), `t` tests it, `d` archives or unarchives. Writes are admin-only.
- **5 Runtimes** shows the web console's run table (Run, Workflow, Status,
  Node, Session, Updated) as wrapping rows: `Enter` opens a run's details in
  place, `t` switches **Root runs only**, `c` cancels a run (it asks first,
  inline) and `s` steers it. The data planes, Artifacts, Cache and Logs are
  wrapping tables with the web page's column names. Opened from an account
  (`g` on Accounts), the page lists that account's runtimes with the chip
  `[Account: <id> ×]`; `x` lists every runtime again.
- **6 Apps** is the web console's Apps tab: open browser apps signed in,
  install or update them, start and stop them, the desktop Assistant and
  Node.js. `a` opens **Apps settings** (Node.js for apps, ports, npm registry,
  Node.js download index); `g` on the Continuum card opens its settings
  (backlog folder, backlog exec runner, process manager). Each row applies on
  its own and says "Saved" beside it; empty returns to the default (the
  gateway's own folder for the backlog). Over SSH, or on a machine without a display, **Open** never starts
  a browser: it shows the one-time link to copy (`y`) and the `ssh -L` port
  forwards that make it work from your own computer.
- **F2** opens the docs assistant (questions answered from the gateway's own
  documentation; signed in).
- **F3** opens the gateway host panel: the **Workflows paused** switch, restart,
  quit, check for and install updates (the same answer and the same update as
  the web console and the tray: an AbstractFramework installer install runs the
  installer, see [tray.md](./tray.md#restart-and-update)), the tray, and
  **start at login** (`L`,
  confirmed, then read back). A banner shows on every screen while workflows
  are paused.
- **I About** (also `F1`, or `?` outside a text field, as an overlay) shows the
  same card as the web console's About: this console's name and version, the
  AbstractFramework and AbstractGateway versions from `GET /api/gateway/about`,
  the links (Website, Source, Docs, Issues, Feedback, Contact) and the licence
  line. `abstractgateway-console --about --gateway-url <gateway>` prints the
  same text without opening the interface.

Every on/off setting is a switch: `[x] Feature` on (highlighted), `[ ] Feature`
off, `[-] Feature — reason` when it can't be used here. `Space` (or `Enter`)
switches the focused one; it applies at once and the status line names the new
state.

Keys: `Tab` focus, `Enter` activate, `Ctrl+N` / `Ctrl+P` next and previous
step, `Esc` back (in a text field, the first `Esc` releases it so screen keys
work again), `1`-`9`, `0`, `W`, `H`, `T`, `N`, `S` and `I` screens, `r` refresh,
`q` quit. Each screen lists its own actions in the key-hint bar at the bottom,
which wraps onto a second line rather than cutting a verb.

`←` / `→` switch to the previous and next screen, wrapping from the last screen
to the first and back, like `Ctrl+P` / `Ctrl+N` in browse mode. The arrows keep
their own meaning wherever the focused element uses them: a text field moves its
caret, a choice list (radio buttons) or a tabs bar (the Runtimes inspector, the
Resources Loaded/Caches tabs) changes its selection, the screen bar moves
between screens, a focused scrolling text pane scrolls, and an open dialog keeps
every key. Press `Esc` in a text field, or `Tab` away from the widget, and the
arrows switch screens again. In the setup guide the arrows do not jump
screens; `Ctrl+N` walks the guide.

**Admin rules.** Admin-only actions are refused before anything is sent for a
non-admin sign-in, with the reason, and the footer marks them "admin only".
The setup guide is admin-only, as on the web. Every write is verified with a
follow-up read and recorded in the journal.

### Providers, Models and OpenAI API in the terminal console

These three pages read and write the same gateway routes as their web pages,
with the same wording:

- **7 Providers** has the web page's three sections; `v` switches between
  them. **Local providers**: one row per engine on the gateway host with its
  state, version and models; `Enter` opens its details (address, connection,
  links), `i` installs it after an inline confirm that shows the plan (and the
  install location for app engines), `s` / `x` start and stop its server, `b`
  opens Models filtered to that engine, `c` cancels a running install.
  **Remote providers**: the cloud and OpenAI-compatible presets; `Enter` or `a`
  opens the connection form. **Available Providers**: the full table; `e`
  edits or overrides, `d` deletes, `m` lists models, `t` tests.
- **9 Models** is one list, like the web page: one header line per model and
  one row per build (engine, id, quantization, size, status), with the models
  you downloaded that the catalog does not know listed under **Not in the
  catalog**. Filters: `/` search, `z` quantization, `p` provider, `t`
  capability, `s` Downloaded / All, `f` fits this computer, `x` clears; `m`
  searches Hugging Face. `w` downloads (with progress, `c` cancels), `d`
  deletes after an inline confirm that states how much it frees ("Files only —
  nothing in your runs is touched."; a loaded or locked model is refused with
  the reason), `u` makes a downloaded text model the default.
- **8 OpenAI API** shows the endpoint's status and base URL, your API key
  (masked; `v` shows it, `y` copies it, `n` issues a new one, shown once), the
  examples (`s` picks one, `c` copies it with the key in clear) and the recent
  requests (`Tab`, then `Enter` opens one with its recorded request and
  response; `f` shows the full record). Administrators also get the Endpoint
  switch (`e`), Restart (`x`), Check setup (`h`), Authentication (`a`), Who can
  connect (`w`) and the Open-mode account (`u`). See
  [openai-api.md](./openai-api.md).

Downloads, deletes, installs and endpoint changes are admin-only; a refusal is
shown with the gateway's reason.

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
