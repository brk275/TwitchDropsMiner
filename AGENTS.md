# Agent Instructions


## Repository Instructions

This file is the canonical harness for AI agents working in this repository.
`CLAUDE.md` and `GEMINI.md` are relative symbolic links to `AGENTS.md` so every agent
reads the same guidance. Maintain all shared and agent-specific instructions here.

## Mandatory Contribution Workflow

Every coding agent MUST read [CONTRIBUTING.md](./CONTRIBUTING.md) before planning,
editing, testing, or reviewing changes and MUST follow its applicable requirements.
It is the repository's contribution policy, not optional background reading.

- Use its PR checklist as completion criteria, including integration with current
  `main` for PRs, unit and regression testing, and independent adversarial review.
- Pass this policy to delegated implementation and review agents. For agent-authored
  work, use a separate adversarial review agent when available, or an independent human.
- Report actual validation and review results and any missing checks in the handoff
  or PR. Never claim completion of checks that did not run or approval not received.
- Do not declare a PR ready to merge while required checks or review are missing or
  blocking findings remain. Document the gap and keep an incomplete PR in draft.
- When taking on an issue or PR, first post a comment there acknowledging that work
  is starting and briefly stating the scope, before investigation, implementation,
  or review. If both an issue and a PR are being worked on, acknowledge both.
- Before ending or pausing work, update every issue or PR being worked on with the
  outcome, relevant evidence or validation, and any remaining action or request for
  information. Post this update for every outcome, including resolved, closed,
  no change needed, blocked, or more information needed, even when no code changed
  or the issue or PR remains open.

## Development Guidelines

### Issue triage

- `.github/ISSUE_TEMPLATE/bug_report.yml` requires the running app version,
  installation method, hosting environment, dashboard browser/device, reproduction
  steps, expected/actual behavior, redacted evidence, and troubleshooting results.
  Keep the form simple; allow an explanation when evidence is unavailable or browser
  details do not apply. The chooser disables blank issues and preserves a separate
  feature/question/documentation template.
- Read the issue and its comments before asking for missing information. Tailor the
  request to unresolved gaps, avoid duplicating unanswered requests, and distinguish
  feature requests, retired version-specific paths, and confirmed recoveries from
  current defects. Never request credentials, authentication files, whole data
  directories, or unredacted network captures. Preserve the home-hosting support scope.

1. **Testing**:
   - Always add unit tests for backend changes.
   - Frontend changes should have tests if possible.

2. **Code Style & Architecture**:
   - **DRY (Don't Repeat Yourself)**: Codebase must follow DRY principle.
   - **OOP (Object-Oriented Programming)**: Required for all backend code.

3. **Refactoring**:
   - You are authorized to refactor code to align with DRY/OOP principles.
   - **Permission Required**: You MUST ask for user permission before significant refactoring.

4. **Localization (i18n)**:
   - Update translation files if there are changes to UI text or console messages.
   - Frontend translation rendering must use safe DOM construction. Do not inject translated strings with non-clearing `innerHTML`; allowlist any intentional links and build them as DOM nodes.

5. **Documentation**:
   - Always update `README.md` and `AGENTS.md` when making changes.
   - Keep `README.md` short and focused on ordinary users: setup, login, migration,
     and links. Detailed public instructions belong in `docs/`, the source for the
     GitHub wiki. Developer contribution policy belongs in `CONTRIBUTING.md`.
   - Personal development plans, investigation notes, local verification records,
     and captures belong in `.dev-notes/`, which Git and Docker builds ignore.
     Never commit them or put them under `docs/`; this overrides skill templates
     that suggest committing `docs/plans/` or `docs/notes/`. Keep credentials out of
     public documentation, PRs, issues, and logs. Report concise, redacted validation
     results in the PR without publishing personal working records.
   - Keep `CLAUDE.md` and `GEMINI.md` as relative symbolic links to `AGENTS.md`; do not replace them with duplicated text. Put any agent-specific instructions in clearly named sections of `AGENTS.md`.

## Project Overview

Twitch Drops Miner is a Python application that automatically mines timed Twitch drops without downloading stream data. It uses Twitch's GraphQL API and websocket connections to simulate watching streams while tracking drop progress.

**Key Characteristics:**

- Python 3.12+ required
- Web-based GUI using FastAPI and Socket.IO
- Async/await architecture with asyncio
- Session persistence via cookies
- No stream video/audio download (bandwidth-efficient)
- Docker-ready for easy deployment

## Architecture


The application now uses a clean `src/` package structure with clear separation of concerns.

### Project Structure

```text
src/
├── models/          # Domain models (Game, Channel, Campaign, Drop, Benefit)
├── config/          # Configuration (constants, paths, operations, settings, client_info)
├── utils/           # Pure utilities (string, JSON, async helpers, rate_limiter, backoff)
├── i18n/            # Translation system (Translator class, TypedDict schemas)
├── auth/            # Authentication (auth_state for OAuth and token management)
├── api/             # External API (HTTP client, GraphQL client)
├── websocket/       # Real-time updates (websocket connection, pool)
├── web/             # Web GUI (app, gui_manager, api/)
│   └── managers/    # Individual UI managers (status, console, channels, campaigns, inventory, login, settings, cache, broadcaster)
├── services/        # Business logic services (channel, inventory, watch, maintenance, message_handlers)
├── core/            # Core client (Twitch client)
├── drop_history.py  # Claimed drop history store (JSON persistence + CSV/JSON export)
├── exceptions.py    # Custom exceptions
├── version.py       # Version string
└── __main__.py      # Entry point

lang/                # Translation JSON files (20 languages)
├── English.json     # Default/fallback translations
├── Español.json
├── Français.json
├── Deutsch.json
└── ...              # 16 more languages
```

### Core Components

**main.py** - Simple launcher:

- Runs the `src` package as a module using `runpy.run_module("src")`
- All application logic is now in `src/__main__.py`

**src/__main__.py** - Entry point:

- Parses command-line arguments
- Initializes Settings, Twitch client, and WebGUIManager
- Starts the FastAPI web server (uvicorn on port 8080)
- Runs the main asyncio event loop
- Handles signals (SIGINT, SIGTERM on Linux) and exit codes

**src/core/client.py** - Central client (`Twitch` class):

- State machine: IDLE, INVENTORY_FETCH, GAMES_UPDATE, CHANNELS_CLEANUP, CHANNELS_FETCH, CHANNEL_SWITCH, EXIT
- Composes `_AuthState`, `HTTPClient`, and `GQLClient`
- Delegates to service layer for business logic
- Drop progress monitoring via periodic "watch" payloads
- Manages WebsocketPool and maintenance tasks

**src/services/** - Business logic layer (fully implemented):

- `ChannelService`: Channel management and selection logic
- `InventoryService`: Campaign and drop inventory operations
- `InventoryService.get_active_campaign(channel)` honors an explicit channel even
  when another is currently watched; only an omitted channel uses the watched value.
  Restrict local campaign guesses to `wanted_games`, including enabled runtime
  fallback games, then preserve shortest remaining time and real earning/ACL gates.
  Special Events/IRL must not win a local estimate when outside that queue. This is
  not proof of Twitch allocation and must not suppress authoritative observations
  for other campaigns. Keep lookup free of selection/settings/progress mutation.
  Regressions: `tests/test_active_campaign_selection.py`.
- `InventoryService.fetch_campaigns()` skips explicit null `user.dropCampaign` nodes,
  including the GQL client's handled server-error nullification. Never construct new
  campaigns from catalog-only metadata. Validate response batch size, required
  traversal and unique returned string IDs belonging to the requested set;
  malformed/missing user/data or unexpected IDs
  raise a fixed redacted `GQLException`, not an empty successful result. Preserve
  cancellation and safe-read retry rules. Match non-null results by ID, not array order.
  Before detail fan-out, merge available catalog metadata only into matching inventory
  records, preserving authoritative inventory fields and existing merge precedence.
  Keep known minutes/claim IDs without inventing missing fields, settled claims or
  eligibility. Unavailable new campaigns are retried by the normal next refresh; this
  adds no fast retry, permanent exclusion, auth recovery or live-progress guarantee.
  Traversal, batch-size and returned-ID failures reject before current model state is
  cleared or published. This is not an atomicity guarantee for later model or GUI errors.
  Tests: `tests/test_unavailable_campaign_details.py` and cancellation/cleanup coverage.
- `tests/test_deadline_inventory_integration.py` combines unavailable details,
  watch-requirement refusal and local wanted-queue lookup with direct observations,
  stale-poll guards and short-lived experimental contexts. Every refresh attempt
  through `Twitch.fetch_inventory()` retires prior context bindings, including a
  failure that leaves old models installed. A rejected refresh alone is not a newer
  minute observation; normal poll reconciliation remains possible for unchanged
  models. Tests use explicit caller-handled failures, not production retry/shutdown
  recovery. Claims must not turn normalized minutes into direct progress, and null
  new details must not create placeholder models or evidence of complete coverage.
- `WatchService`: Drop mining watch payload logic
- `MaintenanceService`: Periodic maintenance tasks
- `MessageHandlerService`: Websocket message routing and handling


**src/models/channel.py** - Channel and Stream:

- `Channel` class: Twitch channel with online/offline status
- `Stream` class: Active stream with game, viewers, drop status
- Stream URL fetching and validation
- ACL-based vs directory channels
- `Channel.banned` accepts an error-free, matching-channel `user.self.banStatus`
  object with boolean `isPermanent`. Both true and false mean an active ban; false
  identifies a temporary ban. Null explicitly clears a ban. Missing, malformed or
  errored responses retain known state. A temporary ban expires at a valid, timezone-aware
  `expiresAt`; absent or invalid expiry waits for a confirmed null status. Permanent
  bans ignore expiry. This account-specific field comes from the separate raw
  `ChannelBanStatus` query to Twitch GraphQL, not broadcast status or watch failures.
- `ChannelService.filter_banned_channels()` checks every discovery/reload candidate in
  batches of 20 with a ten-second timeout, drains tasks on cancellation/exit, and
  removes confirmed bans before sorting and the tracking cap. Preserve existing
  persisted stream reads and safe-read retry rules. Optional failures do not classify
  unknown channels as banned. Watch eligibility and transport also reject known bans,
  including manual selection and special-category ACLs. Explicit unbans take effect at
  the next discovery/reload check; temporary expiry queues discovery automatically.
  Retain known bans across rediscovery failures and synchronize watched/manual objects
  by channel ID, preserve known bans on optional failures, and keep delayed online checks
  from redisplaying a banned channel. Regression coverage: `tests/test_banned_channels.py`.
  Keep confirmed bans and expiry callbacks across cache recovery until expiry or a
  confirmed null response. Cancel callbacks and clear account-owned ban state on
  identity replacement and shutdown.

**src/models/campaign.py** - Drop campaigns:

- `DropsCampaign`: Campaign with game, timeframe, allowed channels
- Time-based eligibility and progress tracking
- Special Events (`509663`) and IRL (`509672`) are identified by `Game.is_special()`.
  Their campaigns can progress across categories only on live channels in a non-empty,
  enabled ACL. Without an ACL, the streamed category must still match. Explicit
  `ignore_channel_status=True` checks retain their discovery-only status bypass.
- `WatchService.can_watch()` requires the campaign's game in `wanted_games`, a live
  channel, and `campaign.can_earn(channel)`. Special categories bypass the channel's
  drops-enabled flag; regular campaigns still require it. Account eligibility, campaign
  and drop timing, prerequisites, claims, and ignore rules remain enforced.
- Channel priority still uses the streamed category; channels outside `wanted_games`
  retain `MAX_INT` fallback priority. Preserve special-category eligibility when changing
  watch selection; do not reintroduce an unconditional campaign/channel game equality gate.
- `WatchService.should_switch()` allows replacement of an unwatchable current channel
  before comparing priorities. Healthy streams keep the existing priority rules; tied
  `MAX_INT` participants must still take over after an offline or ineligible stream.

**src/models/drop.py** - Drop types:

- `TimedDrop`: Drops with minute requirements and progress
- Validate raw `requiredMinutesWatched` at `TimedDrop` construction: accept only a
  plain nonnegative integer, including zero for non-watch/subscription rewards.
  Missing/null, booleans, floats, strings, containers and negative values raise a
  fixed `MinerException` without echoing data, coercion or a zero default. Refuse
  before base construction and before inventory installation, retaining the prior
  inventory/index/GUI state at this failure boundary. Preserve valid claim/progress
  normalization and estimate semantics. This is not general model/GUI transactionality
  or automatic retry/auth recovery. Tests: `tests/test_drop_watch_requirements.py`.
- `TimedDrop.update_minutes()` accepts only a plain integer before arithmetic or
  state changes. Ignore booleans, floats (including NaN/infinity), strings,
  containers and integer subclasses without coercion, counter/estimate mutation
  or a counter-change event from this method. Transports may still display the
  unchanged state. Preserve existing integer correction/clamping, unchanged-reading
  and unequal-sibling underflow behavior. This guards live update values, not
  inventory ingestion, already-corrupt models or whole transport payload shapes.
  Regressions: `tests/test_progress_reading_types.py`, including real websocket
  handling and watch-loop polling followed by a valid reading.
- `BaseDrop`: Base class with claim logic
- `BaseDrop.can_claim` bounds attempts for an existing, unclaimed ID to strictly less
  than 14 days after the campaign end, not the individual reward end. This follows the
  developer API FAQ's maximum grace. Viewer help describes seven days and individual
  campaigns may close earlier; this is an attempt window, not a promise of eligibility
  or delivery. Twitch responses remain authoritative. Never extend mining deadlines,
  fabricate success on rejection, or duplicate history/notifications for repeated claims.
  Regression coverage: `tests/test_claim_grace.py`.
- Precondition chains for sequential drops
- `BaseDrop.claim()` applies successful state synchronously through `_apply_claim_result()`
  before awaiting dashboard, Telegram or Discord delivery. `TimedDrop` normalizes minutes,
  records first-transition history, and publishes inventory in that hook. Preserve
  cancellation propagation, failure/repeat behavior and one-time effects for overlapping
  confirmations; do not postpone persistence until optional notifications finish.
  Synchronous policy/queue-hook failures must also preserve confirmed timed state and
  first-transition history through finally cleanup. Unaccepted or interrupted deferral
  still requests the ordinary games update before propagating the failure; do not
  silently hide it, retry alerts or promise persistence after a filesystem error.
  Regression coverage: `tests/test_claim_state_order.py`. This does not add notification
  retries or guarantee persistence after a filesystem failure.
  Keep the campaign progress revision increment in the same synchronous transition:
  cancelled notification delivery must not allow an older CurrentDrop reply to update
  sibling progress or create estimates. Claim-normalized counters are not direct minute
  observations. Integration coverage: `tests/test_claim_progress_integration.py`.

**src/web/gui_manager.py** - Web GUI:

- `WebGUIManager`: Main GUI coordinator
- Composes individual managers for different UI concerns (status, console, channels, campaigns, inventory, login, settings, cache)
- Uses `WebSocketBroadcaster` for real-time Socket.IO updates
- Pure asyncio, no tkinter dependency

**src/web/app.py** - FastAPI application:

- REST API endpoints: `/api/status`, `/api/channels`, `/api/campaigns`, `/api/settings`, `/api/session`, `/api/session/finish`, `/api/session/retry`, `/api/session/logout`, `/api/session/vnc`, `/api/reload`, `/api/cache/clear`, `/api/close`, `/api/version`, `/api/history`, `/api/history/export.csv`, `/api/history/stats`
- Socket.IO server for real-time bi-directional communication
- Serves static web frontend from `web/` directory
- Integrates with WebGUIManager via `set_managers()`
- The Settings **Clear All Cache** action discards local campaign, channel, and other
  derived miner state, preserves OAuth login and settings, and then reloads from Twitch.
  It is a recovery and diagnostic action, not a correction for Twitch campaign metadata.
- **Dump diagnosis data** calls dashboard-protected `POST /api/diagnostics`; retain
  origin/CSRF guards, a 4-KiB body limit, allowlisted browser metadata and fixed redacted
  validation/storage errors. There is no upload or file-download route. `Diagnostics`
  in `src/diagnostics.py` owns per-client capture, strict snapshot projection and atomic
  saves in `DATA_DIR/diagnostics` (`/app/data/diagnostics` in Docker). Keep the directory
  0700 and files 0600 on POSIX, reject symlinks, drain cancelled writer threads while
  holding the admission lock, and enforce the ten-second cooldown. Retain ten generated
  files, each at most 8 MiB, with explicit snapshot/truncation counts.
- Capture responses from the miner HTTP transport, authenticated GraphQL/OAuth
  transport, SDK integrity exchange, Twitch websocket, Telegram, version checks and
  proxy verification. Sanitize before retaining any payload; keep two recent samples
  per API operation plus counts. API body capture is capped at 256 KiB, with bounded
  nodes/depth/arrays. New response reads must also be bounded. Non-JSON bodies and
  rejected authentication responses retain only metadata. Never record request headers,
  cookies, tokens, passwords, SDK/browser profiles, verification data, signed URLs or
  formatted logs/tracebacks. Unknown keys/free text and identities use HMAC aliases;
  exports use fresh per-dump aliases. Project snapshot booleans/numbers strictly rather
  than trusting raw model annotations. Clear capture state on account replacement;
  remove the warning-location handler on shutdown. Regression coverage:
  `tests/test_diagnostics.py` and `tests/test_diagnostics_frontend.py`.
- `serve_index()` replaces the `__APP_VERSION__` placeholder in local CSS/JavaScript URLs
  with the application version and serves `/` with `Cache-Control: no-cache`
- Any `app.js` or `styles.css` change requires an application version bump through the release
  workflow before deployment so existing clients receive a new asset cache key
- Now Watching offers a page-local thumbnail toggle, off on each page load. Only an
  explicit opt-in loads a JPEG; channel events refresh it at most once per minute.
  Disabling it clears the image source, and losing the watched channel resets the toggle.
  Keep translated labels, native keyboard activation and `aria-pressed` state in sync.
  The dashboard does not play stream video or audio.
  Use canonical channel `login`/`url` for thumbnails and Twitch links; `name` is a display
  label and may contain localized characters. Keep the card title translated.
  Regression: `test_stream_preview.py`.
- The dashboard header keeps brand, translated tabs and account controls in one row
  from 1280 CSS pixels. Narrower screens give navigation a separate row; screens through
  768 pixels also group account controls and allow account text to wrap. Desktop
  account/renewal text may shorten with ellipsis, but its full translated text remains
  in the DOM and title.
  Update both titles on state changes, including logout and renewal errors. Check all
  20 locales, dashboard logout controls, keyboard tab navigation and narrow viewports
  in a rendered browser. `test_header_frontend.py` and `test_browser_login_panel.py`
  cover full tooltip text and removal of stale account/renewal information.

**src/websocket/pool.py** - WebSocket management:

- Sharded connections (up to 50 topics per socket, max 199 channels)
- Topics: User.Drops, User.Notifications, Channel.StreamState, Channel.StreamUpdate
- Automatic reconnection with exponential backoff
- Message routing to registered callbacks

**src/config/settings.py** - Application settings:

- Games to watch list (auto-populated from available campaigns if empty)
- Available from v2.3.0, `priority_list_only` defaults to true. Disable it to append
  other eligible games to the runtime wanted queue without changing saved Games to Watch. Both modes use
  the same watch-drop, claim, expiry, mineability, benefit and campaign eligibility
  guards; omit empty campaigns and games. Fallback games sort by earliest remaining
  campaign end, then game name. Selected campaigns take precedence over fallback,
  including Special Events/IRL ACL channels streaming another category. Use
  `ChannelService.get_mining_priority()` for discovery and automatic switching;
  preserve `get_priority()` and strict-mode streamed-category ordering. Failed
  settings saves restore the previous mode and must not trigger policy refresh.
- Successful unclaimed-to-claimed transitions request a queued games update so
  claiming the last wanted reward also releases campaigns with unwanted rewards
  remaining. Failed or repeated claims must not enqueue duplicate updates.
  Only an explicitly attached experimental calendar may defer a recognized confirmed
  claim's update until that same calendar completes or retires, as described below.
- `tests/test_priority_fallback.py` and `tests/test_priority_fallback_frontend.py`
  cover both modes and their boundaries.
- Games can also be added manually from the web settings search box. Exact and
  unique partial matches resolve to available game names; ambiguous matches do not
  add a game. Confirmations support keyboard focus and Escape. Select All preserves
  priority order and manual entries, and manual confirmation uses current settings.
- Games to Watch supports drag ordering and editable integer priority numbers. Clamp valid
  ranks to the list bounds; reject blank/fractional values without changing settings.
  Keep priority and remove-control labels translated and accessible. Regression tests in
  `tests/test_game_priority.py` cover order, bounds, invalid inputs, and persistence calls.
- Connection quality multiplier
- Language selection
- Proxy support (including verification)
- Logging and dump flags from command-line arguments
- Persistence to JSON file (`settings.json`) in DATA_DIR
- Drop-name ignore list (`drop_name_blacklist`), empty by default. Entries are literal,
  case-insensitive substrings entered one per line; whitespace and blanks are removed and
  duplicates are casefolded while preserving the first spelling/order.
- Inventory filters (Status, Benefit Type, Game Search); Active/Upcoming/Expired use
  OR semantics; All / Linked / Not Linked narrows the result using Twitch-reported
  linkage, independently of mining eligibility. Finished opts claimed campaigns in.
  Persist `inventory_filters.link_status`; migrate the previous `show_only_not_linked`
  checkbox before merging defaults. The older `show_not_linked` key stays ignored.
- `allow_unlinked_campaigns` defaults false. Only explicit boolean true bypasses the
  campaign account-link gate; preserve raw `linked` metadata, badge/emote eligibility,
  timing, ACL/category, Games to Watch, prerequisites, claims, and ignore checks.
  Changes trigger game selection refresh. Keep the warning and save-failure feedback
  translated and safely rendered. Estimated minutes do not prove Twitch progress.
- Zero-minute subscription rewards are omitted from Inventory and Wanted Drops Queue;
  individually expired and non-mineable rewards are omitted from the queue without hiding
  upcoming or sequential rewards; successful claims refresh the queue immediately; the
  actively watched channel remains visible while game settings are changing
- Consecutive identical no-active-campaign console prompts are collapsed until another
  console message appears
- Mining hours default to `always`; `range` uses the miner host's local time (Docker `TZ`),
  inclusive start/exclusive end, supports overnight windows, and treats equal times as all day.
  Validate complete `mode`/`start`/`end` objects before live changes or loading saved values;
  failed persistence restores the previous schedule. The UI uses a dedicated save with
  required time inputs and translated errors. Never silently default an invalid range.
  The schedule task enforces closing and reopening, wakes on policy edits, resamples wall
  time at least every minute, and is cancelled and awaited during authentication changes
  and run exit. Gate watch selection, playlist/Spade freshness, and progress polling too.
  Defer pending inventory/policy/cache requests while closed without setting a busy-loop
  event; fetch fresh inventory on reopening. Existing websocket claims remain available.
  Regressions live in `tests/test_mining_hours.py` and `tests/test_mining_hours_frontend.py`.
- Telegram drop notifications use a bot token stored server-side. The web API/socket
  never echoes the stored token: `get_settings()` returns only a `telegram_configured`
  flag and a masked placeholder, console logs mask the value, and a submitted value equal
  to the mask (or empty) leaves the stored credential untouched.
- Telegram alerts originate in the shared `BaseDrop.claim()` successful unclaimed-to-claimed
  transition, covering websocket, startup, and inventory-refresh claims without duplicate
  alerts for repeated events. Telegram failures must not change a successful Twitch claim.
- The Telegram form reuses the saved token when its input is blank. Clearing the chat ID
  and saving disables alerts. Test Connection waits for settings persistence before showing
  success; HTTP, network, and application save failures must remain visible as errors.
- Discord alerts also originate in the shared `BaseDrop.claim()` transition and must
  preserve Telegram, history and successful Twitch claims on notification failure.
  `DiscordNotifier` accepts only `https://discord.com/api/webhooks/ID/TOKEN`, refuses
  redirects, disables mentions, bounds transport to ten seconds, and never logs URLs,
  response bodies or exception text. Diagnostics retain only status/outcome metadata.
  Settings expose a mask/configured flag, reuse a blank URL, and use the explicit
  `discord_remove_webhook` action to delete it and disable alerts. Validate before
  mutation; restore the previous webhook on save failure. API validation errors must
  not echo submitted settings. Test Connection reports success only after saving.
  Preserve localized safe Help rendering and `tests/test_discord_*` coverage.
- `tests/test_telegram_frontend.py`, `tests/test_telegram_api.py`, and
  `tests/test_telegram_integration.py` cover translated Help rendering, stored credentials,
  failed saves, disabling, all shared claim paths, and transport failures without sending
  real Telegram messages. Keep Telegram UI result strings in every locale.

Drop-name ignore policy is dependency-aware: a matching unclaimed drop and its dependent
branches are ignored dynamically. Prerequisite-only branches with no mineable reward are
skipped, while shared prerequisites required by an allowed reward remain mineable. Ignored
and skipped drops are never counted as claimed. `DropIgnorePolicy.evaluate()` memoizes
immutable prerequisite branch results only within one evaluation to bound repeated
work in shared DAGs. Preserve cycle/unknown/non-watch rejection and recompute after
claim or policy changes; never mutate or persist a branch result across evaluations.
Regression coverage includes dense fan-in, shared cycles and claim-state changes.
Twitch can still award simultaneous
progress to an ignored drop while the miner intentionally targets another reward.

### State Machine Flow

1. **IDLE** - Waiting for campaigns or user action
2. **INVENTORY_FETCH** - Fetch campaigns from GraphQL, claim completed drops
3. **GAMES_UPDATE** - Determine wanted games based on priority/exclude lists
4. **CHANNELS_CLEANUP** - Remove channels not streaming wanted games
5. **CHANNELS_FETCH** - Discover channels via ACL lists or game directories
6. **CHANNEL_SWITCH** - Select best channel to watch based on priority/ACL
7. Loop between CHANNEL_SWITCH and periodic INVENTORY_FETCH (hourly)

### Authentication and integrated browser login

- Helper admission preserves allowlisted validation codes without printing remote text.
  Catalog rejection logs contain only fixed error categories and field-shape labels.
  Preserve redaction, strict catalog validation, and lost-ack receipt reconciliation.
  `SessionDiagnostic` owns fixed code/stage/reason/operation labels and separate dashboard
  and upstream HTTP statuses. Keep `/api/helper/*`'s existing `detail` strings and add only
  sanitized metadata; helper/browser/renewal status exposes the same diagnosis. Distinguish
  captured-session validation, server issuance, issued-session validation and persistence.
  Unknown upstream causes stay `unknown`; never expose arbitrary text, values or exception
  messages. Generic/transport/5xx failures can have an unknown post-upload outcome; query
  the receipt without repeating the upload. A newer attempt must clear older diagnostics.
  Keep transient renewal retries and cancellation semantics. CLI diagnostics use the
  invariant `SESSION_DIAGNOSTIC` machine marker with JSON; existing guidance stays translated.
  Tests in `test_session_diagnostics.py` and `test_helper_diagnostics.py` cover redaction,
  code completeness, admission phases, storage, cancellation and renewal errors.

- `ImportedSession` owns validated WEB contexts and reads persisted formats 1/2/3.
  Accepted browser state takes precedence over valid saved Android cookies. Fresh,
  expired or rejected sessions wait for the integrated container browser. Do not
  restore legacy cookies after explicit logout. Version 3 atomically persists a
  logged-out marker with cleared session/SDK state; successful login clears it.
- `ContainerLogin` owns one temporary attempt with a 15-minute interactive timeout.
  Launch ordinary Chromium at Twitch sign in without remote automation, close its
  window normally to flush storage, then reopen the owned profile for CDP capture.
  Verify the browser PID before reading credentials. Finish sign in uses EWMH close;
  the window X works too. Profiles and all owned processes must be drained through
  cancellation, startup failures, timeout and shutdown. Never attach another profile.
- `SessionController` validates the captured account and catalog, independently issues
  a server context, revalidates the same account and atomically installs the seed.
  Account replacement and logout use `Twitch.authentication_change()` to cancel AND
  await miner, watch, maintenance, websocket callbacks and tracked channel/UI tasks.
  Clear old topics and derived account state before resuming. Epoch/revision checks
  prevent delayed login or renewal from reinstalling credentials after logout.
- The renewal worker rotates SDK state, normally five minutes before expiry, with
  bounded retries and same-account/freshness validation. Mining GraphQL stays in
  Python. Preserve safe-read retry rules; never replay ambiguous mutations.
- `SessionAPI` exposes sanitized status and finish/retry/logout actions, plus optional
  desktop-helper enable/cancel actions guarded by dashboard authentication and CSRF.
  There is no general session upload/export or renewal HTTP route. The binary
  `/api/session/vnc` WebSocket connects only to the current attempt’s loopback VNC
  listener, requires the exact dashboard origin and optional dashboard session, and
  rechecks authorization/attempt/state while connected. Cap viewers and input frames.
  All login HTTP actions retain CSRF/origin guards and fixed redacted error codes.
- The interactive desktop runs as dedicated UID/GID `tdm-browser`, with supplementary
  groups cleared and only PATH/timezone/locale environment inherited. Protect miner
  data and logs with mode 0700 and verify it took effect before starting VNC; fail
  closed on mounts that ignore chmod or give the browser ownership. Keep temporary
  display/profile directories owned by the browser UID. Root-only SDK profiles remain
  unreadable to it. Retain tests for file-access isolation and pre-login password setup.
- Docker includes Chromium, Xvfb, openbox, x11vnc, xdotool, noVNC and timezone data.
  Set `TZ` to the home internet connection’s timezone. Use init, shared memory and
  a cleanup grace period. Only the dashboard port is published. Display numbers are
  allocated by Xvfb to avoid stale fixed-display locks after engine restarts.
- The frontend shows the VNC sign in page when Twitch is logged out, keeps verification
  visible until cleanup, then restores the normal dashboard. Settings ends with Twitch
  logout. The dashboard-password form is reparented into the sign in screen while
  logged out, preserving its single form and listeners. Embedded login stays the default;
  desktop helpers are an explicit fallback. Detailed user
  guidance lives in `docs/authentication.md`; private tests and credentials stay ignored.
- `SessionAPI.viewer()` drains its bridge tasks, closes the VNC connection and releases
  its viewer slot on disconnect or cancellation. Its final WebSocket close must tolerate
  `WebSocketDisconnect`, including Starlette's conversion of transport `OSError`.
  Preserve regression coverage for abrupt viewer closure, task cancellation and
  propagation of unexpected close errors; disconnecting a viewer does not end login.
- Preserve matching WEB client/device/token/integrity/user-agent for imported requests,
  `Channel.url` on WEB for beacon discovery, locale/schema parity and safe DOM rendering.
  Never include session, SDK, password or verification data in dashboard status/logs.
- `LoginFormManager` may publish the account's Twitch avatar as an optional `avatar_url`
  in login status. It is fetched once per account change through the authenticated
  `currentUser` GQL raw query (`GQLRawQuery`), accepted only when it is an `https://`
  URL, and rendered by the frontend as a CSS background on `#user-avatar` with the
  initial-letter fallback preserved. It must never be sourced from or expose credentials.
  Avatar requests are optional, bounded to ten seconds, attempted once per account
  context, and accepted only for the matching `currentUser.id`. Track and cancel them
  when login clears; cancel AND await them before identity replacement and shutdown.
  Keep exception details out of avatar logs. Preserve responsive wrapping of header
  navigation and account controls at tablet widths.
- `HelperConnections` owns only temporary admission and receipts. It must call the
  shared `SessionController.accept()`; never add a second renewal worker or bypass
  current account validation, authentication-change draining, or logged-out persistence.
  Dashboard enable cancels and awaits the container attempt, then opens a ten-minute
  window for one helper. Native `/api/helper/connect` accepts an empty JSON object
  without a pairing code and issues the first client a scoped ticket;
  `/api/helper/session` submits once and `/api/helper/result` recovers
  a lost acknowledgement. These exact method/path pairs bypass dashboard cookies
  but retain origin/Fetch Metadata/request-header checks and bounded bodies. Upload
  and result routes require the issued bearer ticket. Admission is closed by default,
  requires an explicit dashboard action, and never uses a persistent enable flag.
  Atomically recheck the live window after parsing before issuing the one ticket.
  Recheck admission expiry and initiating dashboard authorization before installation.
  Cancel, logout, shutdown, replacement login and new admission invalidate stale work.
  In-memory receipts are intentionally lost on restart; never replay ambiguous uploads.
  Keep tickets out of URLs, CLI arguments, logs, status, broadcasts and storage.
  Document that the first reachable helper is admitted during the trusted-network window.
  `tests/test_helper_admission.py` and `tests/test_desktop_helper_api.py` cover these boundaries.
- Keep the released helper instructions and download links aligned with the four build
  targets: Windows x64, Linux x64, macOS arm64, and macOS x64. Users choose the helper
  computer's platform and match the miner release. The login screen's **Use desktop
  helper** action shows only the dashboard URL; do not reintroduce a pairing-code or
  dashboard-password prompt in the helper. **Return to embedded browser** revokes access.
  Explain the ten-minute, first-helper admission window and retain the embedded browser
  as the default. On macOS, users must quit the helper-owned browser instance to flush
  its profile. Keep the release-ready README, installation examples, login guide, and
  generated release-note instructions consistent with these behaviors.
- The close-ownership regression in `tests/test_login_helper.py` waits for a bounded
  fake-peer completion event before asserting observed commands. A client-side send
  returning is not a server-observation barrier. Preserve exact owned-peer commands,
  the ownership error and absence of close commands for a different owner, plus peer
  cleanup. Use synchronization rather than sleeps or weaker assertions. This test
  change does not alter production close behavior or guarantee real-browser delivery.

### Dashboard authentication

- `src/web/auth.py` owns optional password-only dashboard protection, separate from Twitch
  OAuth and ordinary settings. It defaults off and stores a salted scrypt hash and SHA-256
  session-token digests in `data/web_auth.json` using atomic replacement; corrupt state must
  fail closed. Never expose these credentials in settings, broadcasts, validation errors,
  logs, or cache operations. Use one miner process per data directory.
- `AuthMiddleware` guards FastAPI and the outer Socket.IO ASGI app. Login resources,
  auth status, and `/healthz` are public when enabled. Login actions and the VNC
  viewer require the dashboard session when protection is enabled. Unsafe HTTP requests require
  `X-TDM-Request: 1`; writes and Socket.IO reject foreign origins. `DashboardOrigin` in
  `src/web/origin.py` owns the optional `PUBLIC_BASE_URL` startup configuration: one
  absolute HTTP(S) root URL supplies the allowed browser origin and cookie scheme even
  behind an HTTP backend or rewritten Host. Normalize host/scheme/default ports and IP
  serialization to browser origins; reject ambiguous short/octal/hex IPv4 forms and
  credentials, paths, queries, fragments, wildcard/list origins, and malformed values
  without echoing them. Unset/empty preserves request-derived behavior. This configuration
  must not change ASGI scheme/client or trust forwarded headers; client-IP forwarding
  still requires explicit trusted proxies. HTTPS public URLs enable Secure cookies on
  set and delete. Preserve CSRF, Fetch Metadata, session/revocation, and rate-limit checks.
  `tests/test_web_public_url.py` covers both Socket.IO transports, production environment
  wiring, cookie lifecycles, hostile origins, and separate proxy-IP trust.
- Default cookies are HttpOnly, SameSite=Strict session cookies. Remember me adds a fixed
  30-day Max-Age; server sessions also expire after 30 days and survive restarts. Logout
  revokes the current session; password changes require the current password and revoke
  other sessions. Disabling auth requires the current password and clears all credentials.
- `AuthSocketServer` rechecks authorization on events and broadcasts, disconnects revoked
  sessions, and schedules idle connections to close at expiry. Enabling auth must evict
  already connected anonymous clients before subsequent private broadcasts.
- `web/static/auth.js` owns login/settings behavior and adds the same-origin write header.
  A failed initial auth-status request must leave login available for retry without a
  reload; settings controls stay disabled until auth state is known.
  Keep all UI strings in `gui.auth` across all locales and render them using textContent.
  Local auth assets use the release version cache key; bump through the release workflow
  before deploying changes to existing auth assets, as with app.js and styles.css.
- `tests/test_web_auth.py` and `tests/test_web_auth_frontend.py` cover access control,
  credential persistence, cookie lifetimes, CSRF, rate limiting, revocation, and UI errors.
  The idle socket-expiry regression controls the auth wall clock and captures the
  scheduled callback. Preserve its remaining-lifetime, disconnect, and cleanup assertions;
  do not replace them with millisecond session lifetimes or fixed wall-clock sleeps.
  Docker checks `/healthz`, not the protected `/api/status`. Recovery is local: stop the
  miner, restrict access, remove only `data/web_auth.json`, restart and set a new password.

### Drop Mining Mechanism

- `Channel.send_watch()` polls the lowest-bandwidth HLS playlist and sends HEAD requests
  for every new media segment, without downloading stream audio or video. `WatchService`
  polls about every ten seconds, within the rolling playlist window.
- Deduplicate successful segments in a bounded 256-entry cache per channel/broadcast;
  retain it and the cached playlist URL across same-broadcast metadata refreshes. Reset
  on a new broadcast; account replacement drains watch tasks and derived channel state.
  Retry failed segments, refresh expired playlist URLs, and stop stale batches when the
  selected channel or broadcast changes or goes offline. Bound the entire poll and
  each request; propagate cancellation and process exit. Never log signed URLs or bodies.
- Spade minute-watched telemetry remains auxiliary and runs at most once per 59 seconds.
  Its HTTP 204 response and a Watching label do not prove Twitch has credited progress.
  Keep fallback CurrentDrop queries and estimated-minute bumps on the existing minute
  cadence when changing playlist polling, including across restarts/channel events.
- `WatchService.watch_loop` schedules progress queries with its monotonic deadline,
  independently of the GUI's remaining-seconds value. That value represents total reward
  time and does not count down on the server. Keep successful-watch, current-broadcast,
  mining-hours and cancellation guards. The real `CampaignProgressManager` regression
  covers positive remaining time, exact query cadence, failed watches and stale streams.
- Twitch reports authoritative progress through User.Drops websocket events and GraphQL
  Inventory/CurrentDrop. Distinguish those readings from dashboard estimates and mocks.
- `TimedDrop.progress_observation` is a copied read-only payload for the latest valid
  direct reading: integer minutes within the requirement, allowlisted source and local
  UTC `received_at`. `observed_minutes` is nullable: missing/invalid inventory minutes
  are not confirmed zero. Only inventory ingestion and explicitly tagged websocket or
  CurrentDrop observations record it, including unchanged values. Sibling deltas, local
  estimates, untagged updates and claim normalization do not fabricate observations.
  Preserve all existing counters and mining eligibility. Re-publish unchanged readings
  so cached inventory and Socket.IO consumers receive the new receipt metadata.
  API inventory, incremental updates, active progress and initial-state progress all
  expose the same field. Diagnosis adds strictly projected `observed_minutes`; legacy
  `reported_minutes` still includes sibling inference. Never export source free text.
  Regression coverage: `tests/test_progress_observation.py` and `tests/test_diagnostics.py`.
  `InventoryService` records UTC receipt times immediately after each Inventory,
  Campaigns and CampaignDetails response. A per-refresh receipt ledger follows the
  selected raw drop rows through the existing primary-preferred merge; construction
  after slow sibling requests must not refresh those readings. The ledger holds row
  references and verifies campaign/drop IDs and the original integer minutes, without
  modifying wire payloads. Copied, changed or unrecorded rows remain unknown, not
  freshly stamped. Explicit constructor receipt maps are strict, including an empty
  map; standalone model construction without one retains local ingestion semantics.
  Preserve source precedence, counters, claimed normalization and child-task draining.
  Tests: `tests/test_inventory_receipt_times.py` and delayed inventory-to-planner
  regressions in `tests/test_deadline_inventory_integration.py`. This does not add
  refresh requests or enable deadline scheduling.
  Capture imported-session transport replies before accepted siblings are retained
  during partial auth retries. `GQLResponseObserver` is synchronous and task-local,
  scoped to one inventory fetch; direct HTTP replies are observed before context
  teardown. Repeated observation of the same raw row retains its first receipt.
  Close and release the callback on every scope exit, even cancellation, so copied
  child contexts cannot retain the ledger or renew readings later. Do not replay
  accepted operations, add requests, mutate wire data or change auth ownership.
  Observer scope/cancellation regressions: `tests/test_gql_response_observer.py`.
  Bound each refresh ledger to 8,192 original drop rows, independently of retry
  count. On the next distinct row, release all retained receipts and keep provenance
  unknown for the rest of that refresh; service-level fallback stamps must not
  restart tracking. A later refresh starts a new budget. Duplicate row identities
  do not consume it again. Preserve counters, claims, raw payloads and retry policy.
  This bounds retained row count, not arbitrary input byte size. Repeated discarded
  batch, exhaustion, cancellation and recovery tests: `tests/test_inventory_receipt_budget.py`.
  The active-drop dashboard distinguishes its local counter/estimated remaining time
  from this direct reading and the miner's receipt time. Render zero as known and
  absent/malformed metadata as no report; never infer it from counters, siblings or
  claim normalization. Keep receipt text/data cleared across drop changes and stops,
  use safe text DOM APIs, and redraw active labels when language changes. Do not run
  a browser countdown that implies credit between server updates. These labels are
  not freshness, current Twitch allocation, successful mining or claim guarantees.
  All 20 locale files and GUIProgress must retain matching keys/placeholders.
  Tests: `tests/test_reported_progress_frontend.py`; include rendered desktop/mobile
  checks with fake data only. Static asset changes require the release workflow's
  version/cache-key bump before deployment; do not independently publish a release.
  `tests/test_deadline_progress_display.py` composes real drop observations, progress
  payloads, DOM rendering and conditional planning. A readable old report is not a
  planning freshness lease; missing/stale readings refuse planning even when local
  counters advance. Keep model replacement and stop handling free of retained receipts.
  Combined regressions in `tests/test_progress_observation_integration.py` ensure
  stale replies and estimate recovery cannot rewrite direct-reading provenance.
  Record accepted readings before publishing counter changes, but after reconciliation
  eligibility checks. Unchanged readings still clear eligible estimates and publish
  updated receipt metadata without duplicate recovery requests.
- `TimedDrop.can_reconcile_progress()` retains account, campaign/drop timing, watch
  reward, prerequisite, claim, ignore and category/ACL checks, but not the local estimate
  cap. Websocket and CurrentDrop observations use it; actual mining and future selection
  still enforce the cap through `_base_can_earn()` and `_can_earn_within()`. An unchanged
  observation clears eligible estimates too. Recovered capped rewards queue one games
  update per observation so idle/fallback policy can be reconsidered; repeated readings
  must not enqueue duplicate recovery updates. Preserve sibling bounds and do not
  infer progress from ignored, claimed or otherwise ineligible reported rewards.
  If a sibling delta would underflow, retain its real minutes but still clear eligible
  estimates immediately; an identical second observation must not recover it again.
  Regression coverage: `tests/test_progress_reconciliation.py`.
- `DropsCampaign.progress_revision` advances on plain-integer `TimedDrop.update_minutes()`
  calls, including unchanged values, and before awaiting notification work on a
  successful claim transition. Sibling progress shares this revision. CurrentDrop polling
  snapshots drop identity and campaign revision before awaiting Twitch; discard replies
  superseded by observations, claims or model replacement/removal without estimating a
  minute. Snapshot the fallback campaign before the request too; null, errored or timed-out
  replies may estimate only against that same campaign and unchanged revision. Fresh
  polls can still correct progress downward. Keep this separate from estimated-minute
  bumps. Regression coverage: `tests/test_stale_progress.py`.
  Reject other minute types before revision changes as well as counter arithmetic;
  rejected values must not refresh direct evidence, clear estimates or supersede a
  valid pending poll. Existing out-of-range integer corrections still advance the
  revision without inventing in-range direct evidence. Preserve this branch's valid
  unchanged-reading and underflow estimate recovery. Combined regressions:
  `tests/test_progress_type_integration.py`.
  `tests/test_progress_integration.py` also covers unchanged observations recovering
  capped rewards during pending polls: superseded session/null/error/timeout replies
  must not undo recovery or estimate another minute, while a fresh downward poll works.
- `DropsCampaign.bump_minutes()` must update all eligible drops before testing whether
  any reached the estimate limit. Evaluate `_bump_minutes()` eagerly; a short-circuiting
  generator leaves later rewards unchanged. Preserve eligibility and the estimate cap,
  request at most one channel switch per batch, and display only after the batch.
  Regression coverage: `tests/test_campaign_estimates.py`.
  Playlist/transport, lifecycle, and controlled-clock regressions live in
  `tests/test_playlist_watch.py`, `tests/test_watch_transport.py`, and
  `tests/test_watch_poll_cadence.py`.

### Experimental Deadline Forecasting

- `src/services/deadline_forecast.py` is a pure, opt-in forecast component, not a live
  scheduler. It does not mutate settings, domain models, or channels and has no network
  or wall-clock calls. The runtime adapter, persisted setting, translated status and
  safe reevaluation/handoff are not implemented. Do not present it as an enabled feature.
- Snapshots supply ordered selected games, fresh direct per-drop readings, explicit UTC
  availability/mining windows, channel preference order, prerequisites and claim delays.
  Viewing windows declare simultaneous credit conditionally; `allocation_known` defaults
  false. Never infer it from estimated counters or assume overlapping campaigns progress
  together. A future adapter must enforce actual eligibility and preserve the existing
  selector's baseline, manual control and account/inventory identity boundaries.
- `ChannelSwitchPolicy.should_switch()` shares the runtime's priority/ACL comparison
  with conditional routing. Keep eligibility checks in `WatchService.should_switch()`;
  an absent or unwatchable current stream permits replacement. Lower `(tier, game)`
  tuples win; an eligible current stream survives ties unless promoted from ordinary
  to ACL. Do not prefer channel ID or discovery order over a healthy tied current.
  Forecast candidates use supplied `channel_order` (then ID for deterministic ties),
  not a new implicit ACL sort. `ViewingWindow.acl_based` is strictly boolean;
  optional `mining_priority` is an explicit `(0|1, 0..MAX_INT)` tuple, not future proof.
  Without it the conditional priority follows the highest-ranked watchable reward's
  game, including completed work waiting on its supplied claim settlement.
  With it channel priority remains separate from reward rank, including cross-category
  Special Events/IRL and selected-before-fallback tiers. These are supplied assumptions,
  not a live routing adapter. Fixed-calendar certificates accept only absent or
  single-game rank-canonical overrides; other routing returns `unknown_coverage`
  because the blocker proof is rank-based. Reconnect/setup and rescue release rules
  still apply. Decision timing, complete discovery and future eligibility equivalence
  remain unproven; sharing this predicate does not deliver a live scheduler.
  Tests: `tests/test_deadline_channel_policy.py`, including real-model pointwise
  special-category/fallback checks and the original comparison truth table.
- Conditional baseline watchability includes completed but unclaimed drops until
  their explicit successful-claim settlement time, subject to the same declared
  windows/prerequisite and priority/ACL rules. Separate channel occupancy from
  positive remaining-work credit; never add minutes while waiting on a claim.
  Expiry/offline can release occupancy before settlement; mining pauses do not
  settle claims. Detours return to ranked work after their target watch work, not
  after all claim waits. Tests in `tests/test_deadline_claim_occupancy.py` include
  real `Twitch._run()` CHANNEL_SWITCH checkpoints with explicit settlement input.
  These checkpoints do not prove event cadence, network claim success, complete
  future availability/allocation or full live baseline equivalence. No live policy
  or user setting is added.
- Reject unknown/stale progress, unknown allocation, malformed dependency graphs,
  overlapping contradictory channel windows, naive datetimes, incomplete horizons and
  exhausted budgets. Normalize aware timestamps to UTC before arithmetic. Input limits:
  256 drops/games, 512 viewing/mining windows, 32 rescue candidates, a 14-day horizon,
  and shared transition/work budgets. Do not treat a truncated simulation as successful.
- Compare one selected lower-ranked wanted reward's ancestor detour with rank-only
  mining. Protect every baseline-earned reward at higher or equal rank, honor individual
  exclusive deadlines and claim readiness, and return to ranked work after the target.
  Outcomes are conditional on supplied windows/allocation and do not prove future Twitch
  availability or real credit. Earliest deadline, rank and ID make candidate choice stable.
  `analyze()` remains an exact-input conditional analysis. `analyze_robust()` enumerates
  the full Cartesian product of supplied finite progress offsets, claim delays and
  channel-switch costs, up to 64 cases with one shared transition/work budget. Unknown
  timing alternatives return no recommendation; never sample or silently truncate.
  Offsets are hypothetical additional progress, not rewritten direct observations.
  Charge setup on startup, each channel change and reconnection after idle windows;
  account for interrupted setups without crediting watch time during setup.
  Require the same immediate target/game/channel in every case and preserve each case's
  baseline-earned higher/equal rewards. Search later candidates when earlier ones fail
  in some cases. `earned_at` is the latest target completion among supplied alternatives;
  `baseline_earned_ids` and `protected_drop_ids` are unions with case-conditional meaning,
  not claims that every reward is earnable in every case.
  Finite alternatives are not endpoints standing in for a continuous interval. This
  does not bound arbitrary backend lag/failures or predict future availability. Continuous
  uncertainty treatment and real-selector equivalence remain required before live steering.
  Tests: `tests/test_deadline_forecast.py` and `tests/test_deadline_timing.py`.
- `src/services/deadline_certificate.py` adds sufficient fixed-calendar certificates,
  not a continuous proof for the adaptive greedy simulator. It requires explicit
  remaining-work, successful-claim-delay and setup bounds; it never derives them from
  local counters or assumes unknown latency is zero. Direct readings still gate input
  validity. Remaining upper bounds cannot exceed directly observed remaining minutes.
  A continuously ready, available higher-ranked blocker must need at least the whole
  target deadline interval even at its lower work bound. Reject any higher/equal-to-blocker
  channel that could credit the target in parallel. This proves baseline misses only
  under declared eligibility/allocation/availability assumptions, not live Twitch behavior.
  `baseline_complete` defaults false and must be explicitly boolean true to certify.
  Unknown coverage returns `unknown_coverage`. Windows must exhaust possible baseline
  credit routes as well as guarantee reserved capacity; a currently tracked subset or
  guaranteed-minimum credit set is not evidence that other parallel credit is impossible.
  Reserve upper work, claim waits and setup for the target's ancestor chain, then ALL
  higher/equal-ranked wanted rewards and their ancestors. Fixed UTC slots intersect
  individual, channel and mining windows; reconnect setup earns no credit. Each setup
  and claim latency may vary independently anywhere within its bound. Any executor
  must follow the reserved calendar, not substitute the adaptive greedy policy or move
  later work earlier. Existing `current_channel_id` means an already established stream;
  unknown readiness must pay setup. Claim bounds assume successful settlement, not merely
  a response arriving. No certificate means not proven, not impossible. Some safe detours
  cannot be certified by this sufficient construction. Preserve shared resource budgets,
  exclusive deadlines, immutable inputs and default-off/manual gates. A live adapter,
  actual selector equivalence, persisted setting, translated status, identity invalidation,
  handoff and end-to-end checks remain required. Tests: `tests/test_deadline_certificate.py`.
- `src/services/deadline_inputs.py` captures selected real campaign/drop data as immutable
  inputs, not a plan or execution authorization. It reads no credentials, performs no
  network calls, changes no claim/progress/settings/selection state, and never caches
  or rebinds earlier captures to replacement models. Scope IDs as JSON campaign/drop
  pairs. Use the shared ignore evaluator and real campaign account eligibility;
  clip individual windows to campaign bounds. Keep unclaimed watch ancestors explicit;
  omit already claimed ancestors as settled, including zero-minute prerequisites.
  Do not reopen branches the real mineability policy rejects. Root benefit filters
  and saved selected game order remain authoritative; this is not proof of equivalence
  to the runtime fallback/channel selector.
  Copy valid direct minutes/source/aware receipt only. Unknown evidence stays null;
  stale receipts stay stale. Never substitute display/inferred/estimated minutes.
  Compilation requires explicit per-drop claim timings and hypothetical viewing windows;
  captured inputs always report baseline coverage false and no established channel.
  Manual or partially populated manual targets refuse automatic capture.
  Mining-hours projection uses the validated wall-clock rules over UTC minute segments
  and an explicit installed IANA name or fixed-offset timezone. It handles ordinary
  DST gaps/folds and non-hour offsets without adding local-day timedeltas. No timezone
  is needed for always-open/equal-edge schedules. Missing zones, subminute offsets or
  observed intra-minute discontinuities refuse instead of rounding or assuming UTC.
  The trusted timezone data must have minute-granularity transitions; arbitrary custom
  TZif behavior is outside this projector's domain. Bound scans to 14 days/20,162
  segments, 512 campaigns, 256 selected raw drops and 256 parents per drop before policy
  evaluation; validate graph IDs, strict claim booleans and nonnegative integer watch
  requirements before policy work. Unknown values must not be treated as settled claims
  or silently omitted from protection. No partial result after refusal.
  Nominal hours do not establish actual enforcement jitter or future channel uptime.
  Channel/availability projection, complete baseline coverage, controller handoff,
  settings/UI and end-to-end runtime checks remain
  unfinished. Tests: `tests/test_deadline_inputs.py`.
- `src/services/deadline_context.py` binds one experimental capture to private runtime
  object identities and bounded non-secret state. It is disconnected from live policy,
  not an execution capability, eligibility grant or baseline/availability certificate.
  Capture/validation both default off; require authenticated, loaded, quiescent
  CHANNEL_SWITCH state, open mining hours, no pending refresh/policy work and no manual
  targets or selected channel. Each new capture or failed validation retires the old
  binding. Inspect the pending selected ID without consuming it, including dangling
  IDs; the normal state machine owns the consuming GUI getter. Restored values cannot
  revive a refusal. Never serialize the private context
  or its retained object references, inspect credentials, or persist/export bindings.
  Preserve auth context epochs on clear, invalidation and real identity adoption, and
  client epochs on queued policy/refresh requests, accepted state requests (including
  a repeat of the same state), auth replacement,
  manual targeting and watch/restart/stop/fetch paths. Compare actual inventory/index,
  campaign/drop/channel/stream identities, campaign progress revisions, explicit mining
  settings and non-secret values; Channel equality by ID is not an identity check.
  Retain every ordered ACL/tracked-channel relationship's identity even when node
  state is deduplicated; bound channel relationships to 4,096 entries per capture.
  Replacements with identical IDs/values must still retire earlier contexts. Keep
  strict type-tagged value copying, all-record budgets and exact index consistency.
  A capture expires after at most 30 seconds (15 by default), clipped at the next
  campaign/drop/mining-window boundary. UTC and monotonic checks are exclusive; charge
  capture and validation processing against that same clipped lifetime and reject
  clock reversal/nonfinite values. Caller UTC time and monotonic clock must be trusted.
  Retain successful clock high-water marks, including capture/validation processing. Charge
  processing against both monotonic lifetime and remaining UTC lifetime at entry.
  Caller ownership of the serialized CHANNEL_SWITCH checkpoint remains necessary;
  enum state cannot prove quiescence for direct concurrent service calls outside it.
  Future consumers must revalidate synchronously immediately before action with no
  intervening await, and still enforce real eligibility, current policy and coverage.
  Current context is not proof of transport readiness, future uptime, allocation,
  successful claims or selector equivalence. No integrated executor, enabled setting, UI or live
  scheduler is delivered here. Tests: `tests/test_deadline_context.py`.
- `src/services/deadline_channels.py` adds optional point-in-time channel inputs through
  `DeadlineContextGuard.capture(include_channels=True)`, still disabled by default.
  Use the real channel/watch services and their owning client; getters may warm derived
  policy caches but must not claim, select, change settings, read credentials or call
  the network. Record tracked insertion order, ACL, streamed category, strict mining
  priority, actual watchability and eligible selected drop/ancestor IDs. Eligibility is
  not simultaneous credit: `allocation_known` and `baseline_complete` stay false.
  Fallback-only work can be watchable with an empty selected-drop set. Keep the watched
  object separate, even outside the tracked table or alongside a same-ID replacement.
  Predict only this serialized automatic checkpoint's switch/keep/idle result, with
  stable runtime priority ordering and current-stream/ACL rules. Do not infer future
  windows, exact decision cadence, transport readiness, claim timing or execution rights.
  All-record limits and a work estimate charge wanted-game and ACL membership plus
  repeated per-campaign policy-key scans before eligibility getters; this is not a
  CPU-time guarantee. No sampling or partial projection. Bind service identities and
  service/model/channel owner identities too, recheck context after
  route reads and charge that work against the original UTC/monotonic lifetime.
  Retain manual/pending-selection precedence without consuming the GUI getter. The
  current snapshot is not a complete live adapter, executor, enabled setting or UI.
  Tests: `tests/test_deadline_channels.py`, including actual mocked CHANNEL_SWITCH steps.
- `src/services/deadline_planner.py` joins guarded real-model/channel captures with
  explicit `PlanningAssumptions` and the continuous fixed-calendar certificate builder.
  It remains source-only, default off and disconnected from live selection/settings/UI.
  The caller must own the serialized checkpoint and supply trusted UTC/monotonic clocks.
  Missing assumptions or any coverage declaration other than boolean True yields
  no certificate.
  Current eligibility is not evidence of future uptime, exhaustive baseline routes,
  successful claim bounds or simultaneous credit. Keep those premises explicit.
  Validate timing record IDs exactly, preserve direct readings and nominal mining
  windows, and require immediate credit routes to match real eligibility, priority,
  insertion order and ACL metadata. Do not omit watchable fallback-only work, merge
  conflicting same-ID tracked/current objects or authorize a currently unknown route.
  Future-only windows are hypotheses, not discoveries. First-slot work must be
  eligible on a current known route. A watched object alone never waives setup cost.
  Revalidate the original context after computation, including refusals and malformed
  input paths; processing cannot extend the clipped lifetime. Cancellation propagates.
  Observations are checked as of capture, not leased through result expiry.
  Results contain no private runtime binding; `valid_until` is a context limit, not
  an execution capability or permission to reuse a calendar whose start time passed.
  Actual future baseline equivalence, availability/allocation evidence, safe queued
  execution/reentry, persisted controls, translations and end-to-end acceptance remain
  required. Tests: `tests/test_deadline_planner.py`.
- Fixed-calendar `certify(not_before=...)` can reserve a future first connection,
  without shifting snapshot time, refreshing observations or changing baseline proof.
  Charge the wait against target/protected deadlines, mining/channel windows, every
  setup and claim wait. First connection must match the reserved instant exactly;
  a delayed calendar does not inherit current transport readiness. Omitted/equal
  starts retain the original hypothetical as-of-capture semantics.
- `ConditionalDeadlinePlanner.plan(decision_delay=...)` accepts a strictly positive
  allowance up to 15 seconds, matching its maximum context age. Guard `expires_at`
  clips but never extends the exclusive UTC/monotonic context lifetime. Reject a
  reservation beyond an earlier known model boundary, charge capture/proof/validation
  work to the original allowance, and discard missed budgets without a certificate.
  Explicit delayed plans require direct readings within the age bound at the booked
  start; never manufacture progress during calculation. Future first slots must still
  pass current real eligibility and identity checks. This is a calendar reservation,
  not a live executor, freshness lease, persisted setting, activation or safe queue
  capability. Future consumers must revalidate evidence/policy/time and execution
  bounds immediately before acting. Tests: `tests/test_deadline_decision_delay.py`.
- `DeadlineCalendarExecutor` is a source-only, explicitly enabled conditional consumer,
  not constructed by production settings or the dashboard. Only the
  serialized CHANNEL_SWITCH owner may reserve/advance it; enum state is not ownership.
  Explicit future coverage/allocation/successful timing assumptions are still required.
  Add dispatch allowance to every setup upper bound, keep the original snapshot and
  baseline, and never start later work early after a fast claim. Every actual watch/stop
  is synchronous after fresh context validation. Refuse missed slots, stale reports,
  changed identity/policy, queued work or clock discontinuities. Cancellation releases
  private bindings, never changes manual selection or claims a reward. The caller must
  handle ordinary-policy fallback; wake_at is not permission for a timer to act.
  Bind runtime wanted-game ordering too. Keep sampled wall-clock high-water separate
  from conservative deadline time; charge final reservation validation/fingerprinting.
  Already-claimed later fragments remain booked idle time. Stops may consume booked
  idle time before the next connection, but must retire a missed remaining calendar
  before acting; use post-validation time for fragment bookkeeping and wake requests.
  Conservative upper age only closes deadline/freshness budgets. A not-before or
  unclaimed work-end boundary requires both sampled UTC and origin-relative elapsed
  time to have reached it; never use upper age to open a slot or shorten its work.
  Keep prevalidation monotonic samples when checking reversals. Wake requests may
  wait for a lagging clock but never rewrite the original certificate's slots.
  Share monotonic high-water validation across executor and delegated guard reads;
  capture cannot reset the entry baseline or hide a transient reversal that recovers.
  This does not integrate inventory replacement reentry, prove transport readiness or future
  bounds, enable automatic rescues or complete runtime/browser acceptance. Keep those
  requirements open. Tests: `tests/test_deadline_execution.py`.
- `DeadlineCheckpointQueue` optionally delivers executor checkpoints inside the actual
  registered `Twitch._run_task` CHANNEL_SWITCH loop. Normal construction leaves
  `_deadline_queue=None`; there is no setting, automatic evidence provider or activation.
  Queue intent, not a precomputed plan. The synchronous bounded provider consumes fresh
  immutable inputs inside the original capture budget; it must do no I/O or mutation.
  Reject malformed and native coroutine results, propagate unexpected failure/cancellation
  after retiring private bindings, and reject reentrant requests while executing.
  Provider-raised validation-shaped exceptions are failures too; distinguish the callback
  boundary from internal snapshot refusals without resetting capture or timing budgets.
  Timers only set the state event, with bounded resampling and generation/account/runner
  guards. Never execute watch/stop from a timer or an unregistered caller. General
  policy/state/auth/refresh/manual/restart invalidations cancel; only owned executor
  watch/stop can retain the calendar. Cancel before changing the client epoch, but change
  that epoch in finally even if cancellation fails. Replay accepted intent after event
  clearing, and check cancellation/replacement after synchronous display callbacks.
  Recheck state, runner ownership and cancellation after every checkpoint return, including
  None, before ordinary fallback. Reentrant watch/stop callbacks must not erase EXIT/IDLE
  requests or give a retired/cancelling runner one last ordinary selection action.
  Use the same runner check after display even if the callback detached the queue;
  never rely on queue presence in the next iteration to retire that old runner.
  Browser identity adoption also invalidates before rotating the auth epoch: same-account
  imported-session renewal can retain the existing runner and must wake normal fallback,
  not leave a retired lower-ranked watch waiting forever on a cancelled timer.
  Preserve GUI selection until ordinary policy consumes it, and skip ordinary automatic
  selection while a valid booked calendar handles the checkpoint, including idle gaps.
  Refusal/completion/cancellation returns to ordinary policy, never automatic rebasing.
  Unrecognized claims and inventory refreshes cancel. Persisted controls, trusted
  premises, translated rescue status and full runtime/browser acceptance remain required. Tests use the
  real public run wrapper and child loop with fake transport and explicit hypothetical
  bounds in `tests/test_deadline_queue.py`; no live progress or deployment is established.
- Confirmed claims on an admitted calendar can request continuation, not a new plan.
  `BaseDrop` offers deferral only after a real successful local transition; the sender
  performs no capture, proof, watch or stop. Recognize original indexed models and
  certificate completion IDs, never a same-ID replacement or an unadmitted proposal.
  The owned queue consumes bounded claim tuples with fresh context and original
  UTC/monotonic settlement bounds; charge claim validation against those budgets too.
  Keep the same certificate, original baseline/capture origin and every protected or
  booked idle interval. Claim-normalized counters do not create direct observations.
  `owns_deadline_claim_followup` skips legacy websocket restart/refresh only for that
  current generation/model's queued or handled claim, not a generic success boolean.
  `_deadline_claim_update_pending` retains ordinary policy work while a valid calendar
  owns it. Completion/refusal/cancellation replays `request_games_update` before waiting;
  generic policy/manual/refresh/renewal still cancels, and close/account replacement
  discards old deferred work. Clear private claim bindings on every retirement.
  No default construction or activation. Full inventory-model replacement recovery,
  real future evidence, controls/status/UI and live/browser acceptance remain OPEN.
  `tests/test_deadline_claim_continuation.py` exercises public claim/websocket paths
  with fake GraphQL and controlled clocks, not actual Twitch credit or settlement.

### GraphQL Operations

Persisted operations are defined in `src/config/operations.py` as `GQL_OPERATIONS`; raw GraphQL payloads such as `sendSpadeEvents` use `GQLQuery`:

- **Inventory** - Fetch in-progress campaigns and claimed benefits
- **Campaigns** - List available active/upcoming campaigns
- **CampaignDetails** - Detailed drop info for a campaign
- **GameDirectory** - Find live streams for a game with drops enabled
- **GetStreamInfo** - Check if channel is online and get stream details
- **CurrentDrop** - Query currently mined drop progress
- **ClaimDrop** - Claim a completed drop
- **AvailableDrops** - Check which campaigns a channel qualifies for (badge validation)
- **NotificationsDelete** - Delete Twitch notifications

### Channel Selection Priority

1. Selected channel (if user clicked one)
2. ACL-based channels over directory channels
3. Game priority order (from settings)
4. Viewer count (descending)
5. Maximum 199 channels tracked simultaneously

### Maintenance Task

Runs in background to trigger:

- Channel cleanup when drops start/end (based on time_triggers)
- Inventory reload every ~60 minutes

### Translation System

**Architecture:**

- All translations stored as JSON files in `lang/` directory (20 languages supported)
- English (`lang/English.json`) is the single source of truth and fallback language
- Strongly typed with TypedDict schema defined in `src/i18n/translator.py`
- Translator class (`src/i18n/translator.py`) handles language loading and fallback
- Singleton instance `_` available via `from src.i18n import _`

**Supported Languages:**

- English, Dansk (Danish), Deutsch (German), Español (Spanish), Français (French)
- Magyar (Hungarian), Indonesian, Italiano (Italian), Nederlandse (Dutch), Polski (Polish), Português (Portuguese)
- Română (Romanian), Türkçe (Turkish), Čeština (Czech)
- Русский (Russian), Українська (Ukrainian), العربية (Arabic)
- 日本語 (Japanese), 简体中文 (Simplified Chinese), 繁體中文 (Traditional Chinese)

**Translation Structure:**

```python
Translation = {
    "language_name": str,      # Display name of language
    "english_name": str,       # English name of language
    "status": StatusMessages,  # Console status messages
    "login": LoginMessages,    # Login-related messages
    "error": ErrorMessages,    # Error messages
    "gui": GUIMessages        # All web GUI text (tabs, settings, help, etc.)
}
```

**Usage:**

```python
from src.i18n import _

# Access translations
status_text = _.t["gui"]["status"]["idle"]  # Returns "Idle"
login_text = _.t["login"]["status"]["logged_in"]  # Returns "Logged in"
```

**Language Persistence:**

- Language selection persisted in `settings.json` (DATA_DIR)
- Dynamic language switching supported in web GUI
- Changes take effect immediately without restart

## Key Files

- **src/config/constants.py** - Core enums (State, WebsocketTopic), logging config, type aliases
- **src/config/operations.py** - GraphQL operation definitions (GQL_OPERATIONS)
- **src/config/paths.py** - Path management and Docker environment detection
- **src/config/client_info.py** - Twitch client info (Client-Id, User-Agent)
- **src/config/settings.py** - Application settings with JSON persistence
- **src/exceptions.py** - Custom exceptions (MinerException, ExitRequest, RequestException, RequestInvalid, WebsocketClosed, LoginException, CaptchaRequired, GQLException)
- **src/drop_history.py** - Claimed-drop history store (`DropHistory`) with atomic JSON
  persistence, filtering, stats, and CSV export; recorded on every successful drop claim
- **src/utils/** - Helper utilities (string_utils, json_utils, async_helpers, rate_limiter, backoff)
- **src/i18n/** - Internationalization package with TypedDict schema and Translator class
  - **translator.py** - Translator class with typed translation schema (Translation TypedDict)
  - **__init__.py** - Exports translation types and `_` (Translator instance)
- **lang/** - Translation JSON files for 20 languages (English.json is the single source of truth)
- **src/version.py** - Version string
- **src/web/app.py** - FastAPI application with REST API and Socket.IO
- **src/web/managers/cache.py** - ImageCache for campaign artwork caching
- **web/** - Frontend assets (index.html, static/app.js, static/styles.css)

## Development Commands

**IMPORTANT: Always activate the virtual environment first!**

The project uses a virtual environment located at `env/`. All Python commands must be run within this environment:

```bash
# Activate the virtual environment (required before any Python commands)
source env/bin/activate
```

### Running the Application

```bash
# Run from source (remember to activate venv first!)
source env/bin/activate && python main.py

# With verbose logging (stackable: -vv, -vvv)
source env/bin/activate && python main.py -v

# Create data dump for debugging
source env/bin/activate && python main.py --dump

# Access the web interface at http://localhost:8080
```

### Development Setup

The application requires:

- Python 3.12+
- Virtual environment at `env/` (must be activated before running commands)
- Dependencies from `pyproject.toml` (includes FastAPI, uvicorn, Socket.IO)
- Node.js 24 for frontend behavior tests

Docker deployment from a source checkout:

```bash
# Build and run the included Compose configuration
docker compose up -d --build

# Access at http://localhost:8080
```

Keep the README's Docker update commands aligned with `docs/installation.md`.
Published-image updates pull before stopping/removing the old container, stop on
command failures, and recreate with the same data mount and custom runtime options.
Retain `--init`, the 30-second shutdown grace period, `--shm-size 256m`, and the
user's timezone. Explain that `latest` and restart policies do not update running
containers automatically. Keep the README and installation guide's published-image
`compose.yaml` examples synchronized, including browser settings and persistent
data/log mounts. Pinned-image Compose updates must explain changing the image tag
before pulling. The included `docker-compose.yml` builds the checkout; use
`docker compose pull` for the published-image configuration without `build:`.
Keep those configurations in separate folders because `compose.yaml` takes
precedence. Migration from `docker run` must preserve the exact data mount, remove
the stopped old container before Compose creates its replacement, and avoid running
two miners against the same data or using `down -v` to update.

## Testing

### Automated Tests

The project includes a test suite in the `tests/` directory:

```bash
# Activate virtual environment and run tests
source env/bin/activate && python -m pytest tests/
```

The suite covers settings and proxy behavior, inventory-filter behavior, API filtering,
GraphQL watch events, batched channel discovery, full-locale translation schema and
placeholder consistency, frontend DOM safety, case-insensitive channel filtering,
watch-drop count and expiry semantics, immediate claim refresh behavior, consecutive
no-campaign console collapsing, contributor README automation, and the claimed-drop
history store with CSV export and API endpoints. Frontend behavior tests
share their JavaScript extraction helper and use Node.js;
the validation workflow provisions Node 24 before running pytest. It also runs the release
script contract tests under `.github/scripts/test/`. Ignore-list coverage includes
normalization and settings persistence, dependency pruning, the combined expiry/ignore
Wanted Queue guard, watch selection, truthful ignored/skipped inventory state, translated
placeholder parity, and frontend rendering. Changes to `web/static/app.js` or
`web/static/styles.css` still require the release workflow to bump the application version
and asset cache key before deployment.

`tests/test_special_game_watch.py` covers Special Events and IRL across streamed categories,
missing category/drops flags, offline and nonparticipating channels, disabled or absent ACLs,
Games to Watch selection, campaign/drop eligibility, active-campaign selection, and fallback
priority and failover. It uses mocked Twitch state and does not verify live Twitch progress.

Session persistence tests retain POSIX 0600/0700 assertions only on POSIX; Windows
credential files inherit their destination directory ACLs, so mode bits cannot prove
Windows privacy. Keep those directories private. Owned Chromium cleanup uses process
groups on POSIX and terminate/kill on Windows without requiring SIGKILL there; retain
both platform branches and graceful/forced-stop regression coverage. Firefox URL
allowlist tests need a realistic capture deadline so scheduling delay does not mask
protocol rejection; preserve rejection and target-cleanup assertions.

### Continuous Integration

- `.github/workflows/validation.yml` runs Ruff, Mypy, the Python test suite, language
  JSON validation, `uv lock --check`, release-script tests, and Docker build validation
  for pull requests and pushes to `main`.
- Docker validation and release workflows pin the Node-24-native Docker Buildx v4.3.0
  and Build Push v7.3.0 action commits. Update both workflows together when changing
  either action so validation and release builds use the same trusted versions.
- `.github/workflows/contributors.yml` credits the human author of each pull request
  merged into `main`, including linked pull request numbers in the alphabetically sorted
  Contributors table in `README.md`.
- The contributor workflow runs with write access through `pull_request_target`. It must
  only check out and execute trusted code from the default branch; never fetch or run
  pull request head code in that workflow.
- Keep the contributor table header and the `<!-- contributors:start -->` and
  `<!-- contributors:end -->` markers in `README.md`; the updater fails closed if the
  table header or either marker is missing, duplicated, or malformed.
- `.github/workflows/wiki.yml` publishes the eight public `docs/` guides to the GitHub
  wiki after relevant `main` pushes or manual runs on `main`. Keep `publish_wiki.py`'s
  explicit allowlist, relative-link rewriting, source-file links, and generated sidebar.
  Reject missing files, symlinks, and links to private or unpublished paths before
  writing output. Preserve unrelated wiki pages and history; never force-push.
  Check out the triggering SHA without stored checkout credentials, export before
  exposing `PUBLISHER_TOKEN`, and skip obsolete runs when the documentation, publisher,
  or workflow differs from current canonical `main`. Unrelated main commits must not
  suppress publication. Never run publishing with PR code or credentials
  available to a PR. `tests/test_wiki_publication.py` covers export boundaries, links,
  idempotence, isolated Git publication, and workflow trust. Keep private `.dev-notes/`
  outside Git, the Docker build context, and the wiki publication surface.
- `.github/workflows/version-release.yml` is the release entry point. It must provision
  `uv`, update `src/version.py`, `pyproject.toml`, and `uv.lock` together, and validate all
  three before creating a release branch or tag.
  Preserve existing data-volume/session migration instructions. Prepare versioned
  frontend cache keys through the authorized release workflow before deployment.
- `.github/workflows/github-release.yml` verifies the existing tag against the
  dispatched source SHA and checks branch/package/lock/source version equality.
  Only the final publishing job has `contents: write`. `publish_release.py` creates
  or resumes a draft and refuses to modify published releases. Native helper builds
  cover Windows x64, Linux x64, macOS arm64 and macOS x64 from the verified source SHA.
  Validate archive contents and checksums before upload, and remote size/digests before
  publication. PR jobs never publish. The helper asks only for the dashboard URL.
- Keep browser/control listeners private; do not revive standalone sidecar URLs or
  unrestricted credential-upload routes. The standard Alpine image owns default login
  and renewal; an admitted desktop helper supplies only an initial validated session.


### Manual Testing

1. Run with `-vvv` for maximum verbosity (levels: -v, -vv, -vvv, -vvvv)
2. Check log files in `./logs/` directory
3. Monitor web GUI console output and browser developer tools

## Web GUI Architecture

The application uses a web-based interface accessible via browser:

### Web GUI Components

**src/web/gui_manager.py** - WebGUIManager class:

- Managers: StatusManager, ConsoleOutputManager, ChannelListManager, CampaignProgressManager, InventoryManager, LoginFormManager, SettingsManager, CacheManager
- Uses WebSocketBroadcaster to push real-time updates to connected clients via Socket.IO
- Pure async/await implementation

**src/web/app.py** - FastAPI application:

- REST API endpoints: `/api/status`, `/api/channels`, `/api/campaigns`, `/api/settings`, `/api/session`, `/api/session/finish`, `/api/session/retry`, `/api/session/logout`, `/api/session/vnc`, `/api/reload`, `/api/cache/clear`, `/api/close`, `/api/version`, `/api/history`, `/api/history/export.csv`, `/api/history/stats`
- Socket.IO server for real-time bi-directional communication
- Serves static web frontend from `web/` directory
- Integrates with WebGUIManager via `set_managers()`

**web/** - Frontend assets:

- `index.html` - Single-page application layout with tabs (Main, Inventory, History, Settings, Help)
- `static/app.js` - Socket.IO client, real-time UI updates, API calls, Inventory Filtering and Drop History logic
- `static/styles.css` - Responsive design with dark mode support

### Communication Protocol

**Server → Client (Socket.IO events):**

- `initial_state` - Full state on connect
- `status_update` - Status bar changes
- `console_output` - New log lines
- `channel_add/update/remove` - Channel list changes
- `drop_progress` - Drop mining progress
- `campaign_add` - New campaign added
- `login_required` - Prompt for credentials
- `settings_updated` - Settings changed

**Client → Server:**

- REST API for actions (login, settings, channel selection)
- Socket.IO for connection management

### Docker Integration

**src/config/paths.py:**

- Detects Docker environment via `DOCKER_ENV` env var or `/.dockerenv` file
- Docker: Uses `/app` for code, `/app/data` for persistent storage
- Development: Uses `<project_root>/data` for persistent storage
- All user data (cookies, settings, cache, logs) stored in DATA_DIR
- Provides `_resource_path()` helper for locating bundled resources

**Dockerfile:**

- Uses floating `python:alpine` and `alpine:latest` base tags, including Chromium for
  internal SDK renewal. Upgrade Alpine packages during builds; security rebuilds must
  pull current base images and bypass cached package-install layers.
  Copy noVNC browser assets from a separate stage; the application's authenticated VNC
  proxy replaces websockify, so its unused server dependencies stay out of the runtime.
- Installs dependencies from `pyproject.toml`, then removes installed pip and its vendored
  modules; Python's bundled `ensurepip` bootstrap wheel remains. Dependency changes
  require rebuilding the image.
- Exposes port 8080
- Health check on the public `/healthz` endpoint

**docker-compose.yml:**

- Volume mounts `./data:/app/data` for persistence
- Port mapping `8080:8080`
- Auto-restart policy
- Timezone configuration

### Key Design Decisions

- **WebSocket for real-time** - Socket.IO chosen for reliability (fallback to polling)
- **Single-page app** - Simpler than full framework (React/Vue), fast load times
- **Direct Docker support** - Environment detection, proper path handling
- **Integrated Twitch authentication** - Sign in through the container browser in the
  dashboard, then validate and renew the saved session. Preserve valid saved Android
  sessions during migration.

## Project Scope

This is a hobby project for personal use on the user's own hardware and home network.
Support is limited to that setup and is provided on a best-effort basis. VPS, cloud,
other third-party hosting environments, and services operated for other users are
outside the support scope. Remote dashboard access and Docker support do not expand
that deployment scope. Keep the README disclaimer and contribution guidance consistent
with this policy when reviewing proposals or documenting deployment options.

**Supported:**

- ✅ Web GUI - browser-based interface with advanced filtering
- ✅ Docker deployment - containerized on the user's own home hardware
- ✅ Remote access - access to the user's home-hosted instance
- ✅ Headless operation - no display server required

**NOT supported:**

- Multi-account support
- Channel points mining
- Desktop GUI

### Claimed Drop History

`DropHistory` records successful claims locally in `data/drop_history.json`, deduplicated
by drop ID. The History tab provides game/date filters, pagination, statistics, CSV export,
and confirmed local deletion. Date-only filters mean midnight UTC; aware timestamps
preserve their instant. CSV attachment names use UTF-8 percent encoding with an ASCII
fallback. History text is defined in `gui.history` for every locale and rendered as text.
Tests cover persistence, filtering, Unicode exports, offsets, and translated UI behavior.
