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
- Visual dashboard changes require current screenshots in the PR description, with
  personal information hidden as described in CONTRIBUTING.md. Prefer rendering them
  from mock or sample data; never use screenshots of a real account without masking.
  "Before" images come from the base branch without any PR change. If you cannot attach
  images, host already-redacted images on a separate screenshots-only branch of your
  fork (never the topic branch; pushed images cannot be reliably removed), or state that
  screenshots are pending, keep the PR in draft and ask the user to attach them.

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
     Update concise README summaries or guide links rather than appending per-fix
     changelog paragraphs. Keep user-facing reliability details in the relevant
     existing guides and distinguish checkout behavior from published releases.
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
- Startup logging reports only whether an outbound proxy is configured. Never log
  the proxy URL, userinfo, host, path or query, including malformed values; do not
  modify the actual proxy setting or routing. This does not sanitize old logs or
  all exception paths. Regressions: `tests/test_startup_proxy_redaction.py` execute
  the real entrypoint with mocked miner/server work and inspect log records,
  console output and the rotating file log.

**src/core/client.py** - Central client (`Twitch` class):

- Final shutdown joins current watch and maintenance tasks before closing browser/HTTP
  transports or clearing identity/models. Retain ownership references until the join
  completes, shield it from repeated caller cancellation and re-raise the first
  cancellation before later teardown. A later shutdown call can finish teardown.
  Observe task failures without skipping transport cleanup; never recancel an owner
  already draining. Exclude the calling task to avoid self-join. This covers final
  shutdown after the runtime has exited, not arbitrary concurrent worker replacement
  or a hard cleanup deadline. Regressions: `tests/test_shutdown_mining_ownership.py`.

- State machine: IDLE, INVENTORY_FETCH, GAMES_UPDATE, CHANNELS_CLEANUP, CHANNELS_FETCH, CHANNEL_SWITCH, EXIT
- Channel rediscovery clears a watched owner missing from the retained channels or
  no longer eligible in the refreshed snapshot. Do not continue the old object:
  it may no longer receive offline events. Preserve eligible refreshed owners.
  Reconcile manual channel references too, retaining the manual game intent.
  Regressions in `tests/test_discovery_watch_recovery.py` include actual tracking-cap
  eviction, fresh bans and drops-disabled streams in automatic and manual mode.
- Removing channel subscriptions also retires stream-refresh publication authority
  through the existing Channel helper, including cleanup and tracking-cap eviction.
  Preserve known stream/cache state and batch GUI publication; do not invoke an
  individual GUI removal. Keep cancelled managed workers registered until drained.
  Other channels whose subscriptions remain tracked keep their refreshes. Regressions:
  `tests/test_discovery_refresh_retirement.py`, including real state-machine watch
  ownership and cancellation-suppressing adapters. This does not prove Twitch credit.
- `WatchService.can_watch_in_current_mode()` shares manual-game admission across
  live callbacks and the main selector. Preserve generic `can_watch()` for explicit
  choices, special-category ACL eligibility and progress reconciliation. Revalidate
  the manual target's current category; use an eligible same-game alternative or
  clear manual mode and run normal automatic selection in the same state pass.
  An explicit eligible choice refreshes an existing manual target, including a
  same-game channel change; an uncategorized choice clears prior manual intent.
  Clearing manual intent restores ordinary runtime game priorities before publishing
  the mode change; cache/account teardown skips that recalculation before discarding
  inventory. Do not consume a shutdown request during manual-exit fallthrough.
  Preserve saved rankings, automatic ties, earning gates, mining hours and pending
  requests. This does not establish Twitch progress. Regressions:
  `tests/test_manual_game_selection.py`.
- Composes `_AuthState`, `HTTPClient`, and `GQLClient`
- Delegates to service layer for business logic
- Drop progress monitoring via periodic "watch" payloads
- Manages WebsocketPool and maintenance tasks

**src/services/** - Business logic layer (fully implemented):

- `Channel` owns managed stream refreshes for their entire delay/query/follow-up
  lifetime, separately from the pending-online display marker. Coalesce events
  before dispatch and serialize event-driven follow-ups. Retire publication authority
  on offline/removal and newer applied stream observations, including nulls and
  same-broadcast replacements. Cancellation requests independently prevent late
  publication/name fill. Keep retired workers registered until completion so account
  cleanup joins them; never spawn autonomous retry children or let an old done callback
  clear a replacement. `_stop_channel_tasks()` advances an account generation before
  cancellation; captured reads and follow-ups must reject that retired generation
  even if an adapter calls `uncancel()`. Retry only failed lookups with a queued event,
  not login/exit or publication errors. Preserve direct-call interfaces, playlist cache continuity,
  known stream state on failure and existing display semantics. This is not universal
  discovery/bulk ordering or proof of earning. Regressions:
  `tests/test_channel_refresh_ownership.py`.

- `DeadlineService` is fork-only opt-in through `TDM_SMART_DEADLINES=true`.
  Preserve saved ranks, manual mode, strict/fallback and all earning gates. Budget
  real watched minutes only, all unique unclaimed prerequisites with individual
  start/end windows and a 15-minute reserve. Reject unsafe higher-ranked detours;
  retain a selected feasible branch until claim/eligibility loss, allowing an
  earlier safe deadline. Freeze decisions before both discovery cap and switch
  sorting. Minute reassessment uses a pending safe-boundary flag, not concurrent
  state replacement; five-minute discovery uses normal queued games recalculation.
  Clear account-owned targets during account replacement. Restricted daily mining
  ranges suspend this optional policy. No changes to Twitch progress, claims,
  inventory request frequency or receipt metadata. Tests: `test_deadline_service.py`.

- `ChannelService`: Channel management and selection logic
- `ChannelService.get_live_streams()` treats an explicit null `data.game` as an
  unavailable directory and returns no candidates so discovery can continue to other
  categories. Preserve query flags, no extra retry, existing absent/empty handling,
  malformed non-null failures and cancellation. Do not infer campaign expiry, unlink
  accounts, remove priority games or clear inventory. Regressions:
  `tests/test_unavailable_game_directory.py`, including normalized HTTP/browser GQL.
- `ChannelService.bulk_check_online()` applies matching error-free null-stream replies
  through `Channel.external_update()` so stale online/game/viewer/drop state clears.
  Missing/null users and unmatched IDs remain unknown and retain existing state.
  Preserve batch aggregation, cancellation/draining, later online recovery and the
  caller's existing batch GUI publication; do not add callbacks, requests or claims.
  Regressions: `tests/test_channel_offline_refresh.py`, using real channels/watch policy.
  Errored nulls, including GQL server-error field nullification, remain unknown.
  Capture each input stream object's identity and existing stream/account generation
  before awaiting. A superseded bulk reply must not overwrite an intervening applied
  stream observation, offline event, removal or account retirement, including when
  both states are offline or the broadcast ID is unchanged. Keep the identity guard
  for offline replies and existing batch validation, failure/drain and publication
  behavior. This does not order in-place viewer changes or unpublished concurrent
  reads and does not prove Twitch credit. Regressions:
  `tests/test_bulk_stream_ordering.py`.
- `InventoryService`: Campaign and drop inventory operations
- Admit claimed benefit history only as a list of objects with nonblank string IDs
  and parseable timezone-aware `lastAwardedAt` strings. Empty lists are valid; null,
  false/zero/string/object collections and malformed rows/timestamps raise a fixed
  redacted `GQLException` before catalog/detail requests or inventory/GUI/maintenance
  replacement. Preserve exact opaque IDs, timezone offsets and existing last-row-wins
  duplicate handling. Do not fabricate timestamps, claims or history, skip malformed
  entries silently, retry these errors or broaden auth recovery. This does not validate
  every inventory field or make later GUI publication transactional. Tests:
  `tests/test_claimed_benefit_admission.py`.
- Inventory and catalog campaign collections accept only lists of objects with unique,
  nonblank string IDs, or explicit null using the existing empty-list fallback. Reject
  false/zero/empty strings/objects and malformed or duplicate rows with fixed redacted
  `GQLInventoryListInvalid` before detail fan-out, model replacement, GUI clearing or maintenance
  cancellation. Preserve opaque IDs, inventory precedence and ACTIVE/UPCOMING catalog
  filtering. Admission does not validate every envelope/model field, change auth
  handling or make GUI replacement transactional. Tests:
  `tests/test_inventory_campaign_list_admission.py`, using real imported GQL dispatch.
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
  campaigns from catalog-only metadata. Validate batch size, requested unique string
  IDs and required response traversal; malformed/missing user/data or unexpected IDs
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
  `tests/test_detail_null_guards.py` combines same-owner imported-session detail
  nulls/server-error nullification with real GUI publication and retained-input guards.
  Catalog metadata must not mask invalid retained minutes, claims or requirements.
- `WatchService`: Drop mining watch payload logic
- `MaintenanceService`: Periodic maintenance tasks
- `MINING_LOOKAHEAD` is the existing fixed one-hour targeting horizon, shared by
  campaign selection, queueing, manual eligibility and discovery. Keep actual raw
  `time_triggers` separate from `DropsCampaign.mining_time_triggers`, which also
  includes each campaign/drop start minus the horizon plus `timedelta.resolution`:
  equality is excluded by selection. Skip unrepresentable pre-minimum admission
  timestamps without losing raw events. Inventory retains all known timing events
  independently of mutable preferences, deduplicates/sorts them and trims past events.
  Maintenance queues `request_games_update()` rather than replacing the active state;
  normal claim-ready checks and discovery can consequently run earlier. Coalesce
  elapsed events after a delayed wake; retain and re-arm an event until its timestamp
  is actually due, including after early timer wakes or clock corrections.
  Preserve periodic inventory deadline precedence,
  next-cycle interval edits, manual intent, off-hours deferral, pending inventory and
  EXIT priority, account task ownership and claim/progress rules. This does not widen
  selection, discover unknown campaigns or establish earning. Regressions:
  `tests/test_campaign_wakeups.py` and `tests/test_campaign_wakeup_runtime.py`.
- `MessageHandlerService`: Websocket message routing and handling
- Claim-ready cooldown polling reuses `WatchService._current_drop_reading()` and the
  watch poll's five-second request timeout. Unusable returned data, GQLException
  (including GQLInvalidResponse), or timeout ends only this advisory wait, then runs
  the existing campaign eligibility-based restart/refresh decision. It does not
  resend the claim, update progress, infer completion or alter the four-second initial
  delay, eight-read cap or two-second same-ID waits. Catch parser ValueError separately
  from query/model exceptions; cancellation, auth/account failures and unexpected
  errors retain their existing propagation. Tests: `tests/test_claim_followup_reliability.py`.


**src/models/channel.py** - Channel and Stream:

- `Channel` class: Twitch channel with online/offline status
- `Stream` class: Active stream with game, viewers, drop status
- Stream URL fetching and validation
- ACL-based vs directory channels
- `Channel.viewers` assignments accept only plain nonnegative integers. Invalid
  live viewcount updates retain the previous count without coercion or changing
  stream identity/status; later valid counts, including zero, remain accepted.
  This guards live assignments, not initial GraphQL stream metadata or already
  corrupted models. Regressions: `tests/test_channel_viewer_updates.py`.
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
- `DropsCampaign.mining_selection()` separates local targeting from raw eligibility.
  Select unclaimed wanted watch rewards within the usual one-hour lookahead, then
  include their valid unclaimed prerequisite branches even when those prerequisite
  benefit types are disabled. Claimed prerequisites short-circuit; ignored, missing,
  cyclic, non-watch or unavailable unclaimed branches cannot justify mining. Recompute
  on time, preference and claim changes without a persistent selection cache.
  `can_mine`, `can_mine_within`, `first_mining_drop` and wanted remaining minutes
  share this selection across queueing, discovery, watching, local guesses, estimates
  and claim continuation. Presently usable channels precede future-only/unusable
  candidates before the 199-channel cap; retain game rank, ACL and viewer ordering
  within usable tiers. Do not conflate this policy with raw `can_earn`, ignore
  mineability, `can_reconcile_progress`, authoritative sibling updates or claims.
  Disabled-type progress and already-earned claims remain valid; local estimates
  stay limited to selected work and are not Twitch confirmation. Tests:
  `tests/test_benefit_selection.py`, including real discovery/cap and watch-loop paths.
- Admit `timeBasedDrops` only as a list of objects with unique nonblank string IDs
  within that campaign. Refuse missing/null/scalar/object collections and malformed
  or duplicate rows with fixed `MinerException` before constructing drop models or
  replacing inventory/GUI state. Preserve exact IDs, order, valid empty lists and
  zero-minute rewards; do not coerce, silently drop rows, choose a duplicate winner
  or mutate raw data. This does not validate all fields or cross-campaign identity
  uniqueness, add recovery or make later GUI publication transactional. Regressions:
  `tests/test_campaign_drop_admission.py`, including real imported detail refreshes.
- Time-based eligibility and progress tracking
- Special Events (`509663`) and IRL (`509672`) are identified by `Game.is_special()`.
  Their campaigns can progress across categories only on live channels in a non-empty,
  enabled ACL. Without an ACL, the streamed category must still match. Explicit
  `ignore_channel_status=True` checks retain their discovery-only status bypass.
- `WatchService.can_watch()` requires the campaign's game in `wanted_games`, a live
  channel, and `campaign.can_mine(channel)`. Special categories bypass the channel's
  drops-enabled flag; regular campaigns still require it. Account eligibility, campaign
  and drop timing, prerequisites, claims, and ignore rules remain enforced.
- Channel priority still uses the streamed category; channels outside `wanted_games`
  retain `MAX_INT` fallback priority. Preserve special-category eligibility when changing
  watch selection; do not reintroduce an unconditional campaign/channel game equality gate.
- `WatchService.should_switch()` allows replacement of an unwatchable current channel
  before comparing priorities. Healthy streams keep the existing priority rules; tied
  `MAX_INT` participants must still take over after an offline or ineligible stream.

**src/models/drop.py** - Drop types:

- `ClaimState` retains only accepted claim acknowledgements by campaign/drop identity
  for one authenticated account lifetime. Bind each campaign to that owner. Reconcile
  replacement inventory and the installed model synchronously before optional delivery;
  normalize claimed minutes without replaying history or alerts. Cache clearing and
  same-account credential renewal retain evidence. Authentication replacement retires
  the old owner after its work drains, even on cancellation, then creates an empty one.
  Never use account-unbound persisted history as claim authority, fabricate confirmation
  after cancellation or replay ambiguous writes. This evidence is process-local, not
  progress receipt metadata or persistence across restarts. Inventory display accepts
  only its exact campaign/drop owner. Serialize its sends, reject superseded queued
  snapshots and finish an admitted emit before its replacement; no delivery retry or
  hard timeout is added. Regressions: `tests/test_claim_inventory_ownership.py`.
- `TimedDrop`: Drops with minute requirements and progress
- `TimedDrop.update_minutes()` accepts only a plain integer before arithmetic or
  state changes. Ignore booleans, floats (including NaN/infinity), strings,
  containers and integer subclasses without coercion, counter/estimate mutation
  or a counter-change event from this method. Transports may still display the
  unchanged state. Preserve existing integer correction/clamping, unchanged-reading
  and unequal-sibling underflow behavior. This guards live update values, not
  inventory ingestion, already-corrupt models or whole transport payload shapes.
  Regressions: `tests/test_progress_reading_types.py`, including real websocket
  handling and watch-loop polling followed by a valid reading.
  Refuse invalid types before advancing the campaign revision, so they cannot retire
  a valid pending poll or suppress its existing null/error/timeout fallback.
  `tests/test_numeric_progress_ordering.py` covers rejected values and valid integer
  supersession, including unchanged and clamped readings.
- Validate raw `requiredMinutesWatched` at `TimedDrop` construction: accept only a
  plain nonnegative integer, including zero for non-watch/subscription rewards.
  Missing/null, booleans, floats, strings, containers and negative values raise a
  fixed `MinerException` without echoing data, coercion or a zero default. Refuse
  before base construction and before inventory installation, retaining the prior
  inventory/index/GUI state at this failure boundary. Preserve valid claim/progress
  normalization and estimate semantics. This is not general model/GUI transactionality
  or automatic retry/auth recovery. Tests: `tests/test_drop_watch_requirements.py`.
- Validate non-null inventory `currentMinutesWatched` as a plain nonnegative integer
  before base construction and inventory publication. Reject malformed/negative
  values with fixed `MinerException`, even for claimed rewards. Missing/null keeps
  local zero fallback without inventing a claim; preserve valid integer overshoot,
  claim normalization and later local estimate behavior. This does not validate all
  model fields, guarantee whole GUI transactionality or add automatic recovery.
  Tests: `tests/test_inventory_minute_admission.py`, including real InventoryManager
  preservation and same-owner loopback imported-session refusal/fresh-read recovery.
- `BaseDrop`: Base class with claim logic
- Present inventory drop `self` must be an object with boolean `isClaimed` and
  `dropInstanceID` explicitly null or an opaque nonblank string. Reject malformed
  values with a fixed MinerException before inventory publication; absent `self`
  retains benefit inference. Do not trim valid IDs or coerce booleans or strings.
  Absent-self inference requires nonempty benefits and an in-window award for every
  benefit, not merely every history entry that happens to be present. Shared or partial
  rewards must not mark the whole drop claimed or normalize minutes to completion.
  Preserve inclusive start/exclusive end, explicit claim-state precedence, single-benefit
  behavior and no inferred claim ID/history/revision side effects. This remains an
  inference from award history, not an authoritative claim acknowledgement. Tests:
  `tests/test_benefit_claim_inference.py`, including real imported inventory publication.
  `update_claim()` returns false for unusable live IDs without replacing an existing
  ID. Claim-ready handlers return before claiming, publishing, waiting or refreshing.
  `can_claim` also rejects unusable IDs; claim-ready logs must not echo ID payloads.
  This does not validate whole websocket envelopes or add recovery/retries or change
  claim grace. Tests: `tests/test_claim_input_reliability.py`.
- Precondition chains for sequential drops
- `BaseDrop.claim()` applies successful state synchronously through `_apply_claim_result()`
  before awaiting dashboard, Telegram or Discord delivery. `TimedDrop` normalizes minutes,
  records first-transition history, and publishes inventory in that hook. Preserve
  cancellation propagation, failure/repeat behavior and one-time effects for overlapping
  confirmations; do not postpone persistence until optional notifications finish.
  Regression coverage: `tests/test_claim_state_order.py`. This does not add notification
  retries or guarantee persistence after a filesystem failure.
  Preserve the synchronous campaign revision increment before optional delivery too.
  `tests/test_claim_reliability_ordering.py` covers notification cancellation while a
  progress query is pending, using persisted history and stale-reply rejection.
- `BaseDrop._claim()` confirms only an object reply with no legacy root `error`,
  absent/null/empty-list root and data `errors`, object data/claimDropRewards, and
  exact string status `ELIGIBLE_FOR_ALL` or `DROP_INSTANCE_ALREADY_CLAIMED`.
  Missing, malformed or errored confirmations return false without changing claim
  state/history or retrying. False is unconfirmed, not proof that Twitch did not
  execute the request; a later explicit attempt can still confirm it. Preserve early
  claimed/ineligible returns, the existing GQLException catch and other exception/
  cancellation propagation. This model-boundary validation does not harden earlier
  transport/GraphQL processing or change claim grace/notification ordering.
  Regression coverage: `tests/test_claim_response_shapes.py`, including BaseDrop,
  TimedDrop, repeated/later claims and loopback imported-session GQL dispatch.
  `tests/test_claim_confirmation_ordering.py` combines rejected/accepted responses
  with campaign revisions, persisted history and pending CurrentDrop replies.
- Chain-minute totals reuse only cycle-free subtotals within one property evaluation.
  A subtotal truncated by a cycle depends on its ancestor path and must not be reused
  on another path. Required totals still include claimed work; remaining totals stop
  at claimed nodes. Preserve missing-parent handling, supplied visited prefixes and
  negative remaining values rather than clamping them. No persistent cache: progress,
  estimates and claim changes must be reflected on the next read. The shared ignore
  evaluator has different graph semantics and is not a substitute for this calculation.
  Regression coverage: `tests/test_chain_minute_work.py`, including active-campaign sorting.
  `tests/test_chain_minute_reliability.py` combines shared totals with real progress
  updates, estimates and persisted confirmed claims during cancelled delivery.

**src/web/gui_manager.py** - Web GUI:

- `WebGUIManager`: Main GUI coordinator
- Settings notification and game-filter action groups wrap within their section.
  Keep the rule scoped to `.settings-section`; do not hide overflow, truncate labels,
  reorder controls or change actions to fit mobile layouts. Preserve DOM keyboard
  order and desktop single-row layout where space permits. Source contract tests:
  `tests/test_settings_action_layout.py`; verify actual geometry in a rendered browser
  across locales and narrow viewports too. A stylesheet cache-key bump through the
  authorized release workflow is required before deployment.
- Composes individual managers for different UI concerns (status, console, channels, campaigns, inventory, login, settings, cache)
- Uses `WebSocketBroadcaster` for real-time Socket.IO updates
- Pure asyncio, no tkinter dependency
- `CampaignProgressManager.update()` refuses drops already marked claimed at admission. Clear only
  the exact currently displayed model when its claim is redisplayed; an older model
  with identical IDs must not clear its replacement. Reconnect snapshots omit drops
  claimed since the last display without broadcasting or mutating claim state.
  Explicit `update(None, ...)` uses the normal stop event. Unclaimed local counters,
  even at 100%, are not claim confirmation. Regression coverage:
  `tests/test_claimed_progress_display.py` includes real websocket claim continuations.
  Stopping also zeros the cached remaining time used by readiness checks;
  ignored claims for other models must preserve the current countdown.
  `tests/test_claim_display_recovery.py` combines this guard with real confirmed
  claims, persisted history and unusable advisory follow-up reads. Do not reactivate
  a claimed model or clear a replacement display during request recovery.
  Serialize progress/stop broadcasts within this manager. Each admitted transition
  advances a display revision; waiting sends recheck it, model identity and claim state
  before dispatch. Skip superseded sends, but do not retract an already-started emit.
  A yielding emit must finish, fail or be cancelled before its replacement can emit.
  Cancellation/errors release the send lock; this does not add retries, delivery
  guarantees, hard timeouts or serialization of other managers or reconnect snapshots.
  Regression coverage: `tests/test_progress_broadcast_ordering.py`.

**src/web/app.py** - FastAPI application:

- REST API endpoints: `/api/status`, `/api/channels`, `/api/campaigns`, `/api/settings`, `/api/session`, `/api/session/finish`, `/api/session/retry`, `/api/session/logout`, `/api/session/vnc`, `/api/reload`, `/api/cache/clear`, `/api/close`, `/api/version`, `/api/history`, `/api/history/export.csv`, `/api/history/stats`
- `/api/version` uses the declared `semver` parser for numeric/prerelease precedence,
  ignoring build metadata. Accept only one optional leading `v` on the remote tag;
  invalid release or installed versions leave comparison unknown, never infer an
  upgrade or echo parser input in logs. Preserve response keys, safe release-list
  fallback, request timeout and cancellation. Tests: `tests/test_update_version.py`.
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
- Topic compaction retains ownership of retired shutdown tasks until completion.
  Pool shutdown drains active and retired sockets, including retirements admitted
  while it waits, before propagating a failure. Shield owned joins from caller
  cancellation and propagate the first cancellation only after draining all groups.
  Socket shutdown cancels and drains receive/callback tasks even if close fails,
  then clears connection references and applies requested topic removal.
  `stop_nowait()` returns a joinable task; observe retired shutdown failures without
  logging exception payloads. Do not replay cancelled claims or replace auth state
  before old callbacks drain. These are cooperative barriers, not hard deadlines or
  guaranteed physical closure after failed I/O. Regression coverage:
  `tests/test_websocket_shutdown_ownership.py`, using only current-main test helpers.
  `tests/test_websocket_claim_retirement.py` combines this barrier with confirmed
  claims and persisted history during dashboard/notification cancellation. Cleanup
  must observe the old identity; do not undo a confirmed claim or replay its request.

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
- `json_save()` stages complete settings in a unique private sibling, flushes,
  fsyncs and closes it before atomic replacement. Preserve custom serialization,
  formatting and existing symlinks by resolving the target first. Pre-publication
  failures leave the prior file intact and remove the temporary file when possible.
  New POSIX files are owner-only; Windows ACL privacy depends on the parent directory.
  Atomic replacement needs a writable parent and must not fall back to in-place writes.
  This is not general live-settings rollback or a power-loss durability guarantee.
  Regression coverage: `tests/test_settings_persistence.py`.
  `tests/test_atomic_settings_reliability.py` combines real failed publication with
  the API handler's fixed errors, existing protected-policy rollback and the owned
  games-update queue hook. A later explicit retry publishes before notification;
  EXIT still refuses queue admission. This does not roll back every setting field.
- Inventory refresh intervals accept plain integers from 1 to 1440 minutes, matching
  the dashboard input. Share `RefreshIntervalSettings` validation across the API,
  manager and persistence boundaries. Invalid saved intervals use the existing
  30-minute default; reject invalid live updates before other mutation or persistence.
  Failed saves restore the previous live interval without logging success or broadcasting.
  Keep null/omitted updates and next-cycle scheduling semantics. Regression coverage:
  `tests/test_refresh_interval.py`, including actual maintenance delay and HTTP redaction.
  The dashboard uses a dedicated native-validating save, never truncates fractional
  input, restores confirmed values on failure and renders translated errors as text.
  Successful HTTP acknowledgements merge only the interval while the captured settings
  object remains current. Preserve newer socket snapshots and unrelated pending controls;
  do not apply the acknowledgement's complete settings object after a newer observation.
  General autosaves omit this setting so stale requests cannot undo explicit changes.
  Preserve `tests/test_refresh_interval_frontend.py` and the release/cache-key gate
  before deploying changed `app.js` assets.
  `tests/test_refresh_atomic_reliability.py` verifies real HTTP save failures retain
  both atomic settings bytes and the previous maintenance delay. Explicit retries
  publish the bounded interval for the next cycle without forcing an immediate reload.
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
  Notifier and claim-wrapper logs must not include exception text, bot URLs, recipient
  IDs, message text or response bodies. Retain fixed failure messages and numeric HTTP
  status; existing diagnostics sanitize responses before retention. Keep cancellation,
  the ten-second request timeout and no automatic notification retries unchanged.
  The Telegram test API must use its generic failure response for unexpected errors,
  never reflect exception text or stored credentials to the browser.
  Regressions: `tests/test_telegram_error_redaction.py`, including loopback HTTP.
  Telegram response bodies are unnecessary without diagnostics. When enabled, use
  the existing bounded `Diagnostics.capture_http()` for successful and rejected
  responses, consuming at most 256 KiB plus one truncation byte. Do not call full
  text/read methods or decode and slice afterward. Optional capture ClientError or
  timeout retains HTTP status metadata and the status-based notification result;
  cancellation still propagates and contexts close. No retry or claim-state change.
  Regressions: `tests/test_telegram_response_bounds.py`, including open-ended loopback
  responses, capture failures and real confirmed-claim/history/Discord continuation.
- The Telegram form reuses the saved token when its input is blank. Clearing the chat ID
  and saving disables alerts. Test Connection waits for settings persistence before showing
  success; HTTP, network, and application save failures must remain visible as errors.
- Discord alerts also originate in the shared `BaseDrop.claim()` transition and must
  preserve Telegram, history and successful Twitch claims on notification failure.
  Claim embeds may show the first usable benefit image from HTTPS
  `static-cdn.jtvnw.net`, with no credentials/query/fragment, a 2048-character bound
  and text-only fallback. Never download artwork on the miner or forward arbitrary
  hosts, signed URLs or user data. Keep all bundle reward names and image fields intact
  through embed text escaping. `tests/test_discord_reward_images.py` covers this path.
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
and skipped drops are never counted as claimed. Twitch can still award simultaneous
progress to an ignored drop while the miner intentionally targets another reward.

`DropIgnorePolicy.evaluate()` reuses immutable prerequisite branch results only within
one evaluation, avoiding repeated expansion of shared dependency graphs. Preserve
cycle, missing-parent, non-watch and ignore behavior. Claimed prerequisites short-circuit
before cycle checks. Never mutate cached branch sets or retain them across evaluations;
claim and policy changes must be reflected on the next call. Regression coverage in
`tests/test_drop_ignore_policy.py` includes dense fan-in and changes between calls.

### State Machine Flow

Inventory refresh recovery catches `GQLRequestFailed`, the fixed GQLException
subtype for `SessionRequestExhausted` after safe imported reads exhaust transient
retries, and `GQLInventoryListInvalid`, raised only at campaign-list admission before
inventory replacement. Generic REQUEST, invalid JSON, TLS, non-5xx HTTP and ambiguous
mutation failures do not qualify. Try at most three complete
refreshes with 30/60-second waits. Preserve cancellation, authentication failures,
other malformed replies and model errors without retry. Do not retry claims or other state
steps. Waits wake on EXIT or mining-hours closure without accelerating on unrelated
state events; defer refresh until reopening. Keep successful inventory publication
and ordinary request-level safe-read limits unchanged. Tests:
`tests/test_inventory_refresh_recovery.py` and `tests/test_inventory_list_recovery.py`.
This is bounded recovery, not a promise
of unattended collection during persistent outages or a transactional GUI refresh.

1. **IDLE** - Waiting for campaigns or user action
2. **INVENTORY_FETCH** - Fetch campaigns from GraphQL, claim completed drops
3. **GAMES_UPDATE** - Determine wanted games based on priority/exclude lists
4. **CHANNELS_CLEANUP** - Remove channels not streaming wanted games
5. **CHANNELS_FETCH** - Discover channels via ACL lists or game directories
6. **CHANNEL_SWITCH** - Select best channel to watch based on priority/ACL
7. Loop between CHANNEL_SWITCH and periodic INVENTORY_FETCH (hourly)

### Authentication and integrated browser login

- The temporary interactive Chromium profile disables password saving before startup.
  Its bundled completion extension observes only Twitch login-cookie presence, closes
  the owned profile's windows normally, and never exports cookie values. Treat this as
  a completion hint: retain all capture/PID/account/catalog checks and manual Finish.
  Grant cookie host access to both `*://twitch.tv/*` and `*://www.twitch.tv/*`
  so root-domain events and non-Secure cookies' HTTP event URLs are visible. Keep
  lookups on HTTPS and do not broaden access to subdomain wildcards or unrelated hosts.
  Cookie scope and presence trigger completion regardless of its Secure attribute;
  they are not proof of a valid session. Test domain-wide and host-only cookies in real
  Chromium with synthetic data, including Secure and non-Secure domain cookies.
  Disable extensions during capture. Clipboard transfers are explicit, bounded to
  65,536 characters, page-local and cleared on send/panel close/viewer disconnect; remote
  clipboard events are ignored unless the panel is open. Never log or persist them.
  Keep preference-content and launch-argument tests portable: assert POSIX file modes
  only on POSIX and mock UID lookup in argument-only tests; retain real Linux isolation tests.
- SDK integrity issuance uses a separate header contract with a fresh 32-character
  client request ID and canonical X-Device-Id, while renewed GraphQL contexts retain
  the original allowlisted headers and never store that request ID. Keep downstream
  catalog validation mandatory; mocked issuance does not prove Twitch accepts a proof.
- `OwnedChromium` uses `xvfb-run` when available for server SDK issuance and renewal,
  with a private authenticated display (1280×800) and ordinary 1200×760 browser window.
  Docker includes `xvfb-run` and `xauth`. Never reuse the interactive VNC display or
  disable X authentication. Keep profiles owner-only, CDP loopback-only and process-group
  cleanup on failure/cancellation. Keep Xauthority inside the owned temporary profile
  so forced shutdown removes it too. Hosts without the runner retain the headless fallback.
  A valid integrity response alone does not prove account/catalog acceptance.

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
  Own the entire pre-admission drain in a shielded task. Remember first caller
  cancellation and propagate it only after every drain phase, without yielding for
  installation. Keep old identity and pause state until the drain finishes. Cancel
  worker/channel/avatar owners once, including already-draining owners; exclude the
  original caller from worker joining. After a websocket/channel/avatar drain error,
  attempt remaining phases before surfacing the first error. Caller cancellation
  takes precedence over observed drain errors. This is cooperative serialized account
  replacement, not a hard deadline or rollback of a session already installed after
  admission. Tests: `tests/test_auth_change_cancellation.py`.
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

### GraphQL Reply Validation

- `GQLClient._response_rows()` validates single-object replies or object batches
  matching the request count captured before dispatch. Validate all rows before
  normalization or retry. Present `errors` must be nonempty lists of objects with
  nonempty string messages; legacy error/message fields must be nonempty strings.
  `GQLInvalidResponse` is a fixed GQLException subtype. Map only imported dispatch
  SessionError RESPONSE to it, not auth-validation or ordinary transport exceptions.
  Imported dispatch SessionError REQUEST becomes a fixed ordinary GQLException
  except safe-read transient exhaustion (`SessionRequestExhausted`), which becomes
  `GQLRequestFailed` for bounded inventory recovery. Do not retry again in GQLClient;
  ambiguous mutations remain unconfirmed. Preserve other session codes, initial
  authentication failures, ordinary transport exceptions and cancellation unchanged.
  Regression coverage: `tests/test_gql_request_failures.py`.
  Watch polling skips invalid replies/RESPONSE without estimating, then accepts
  later valid progress. REQUEST retains guarded request-failure estimation, which
  is not Twitch-confirmed progress. Claims stay unconfirmed. Imported auth-rejection checks refuse malformed
  message types without changing definitive rejection/data/path guards.
  Known persisted reads retain existing service-error retry timing and limits. The
  shared `GQLRetryPolicy` preserves ImportedSession's read fingerprint allowlist;
  mutations, unknown requests and mixed batches never get service-error retries.
  Deep-copy request payloads before dispatch and recheck safety after retry waits and
  authentication validation. Optional response operation labels cannot authorize replay.
  Server-error normalization uses the checked path helper documented below.
  At the HTTP response.json boundary only, JSONDecodeError, UnicodeDecodeError and
  aiohttp ContentTypeError become GQLInvalidResponse without retry or raw context.
  Preserve original decoding failures for HTTP401/403 and leave authentication,
  browser/session failures, request/context entry/exit errors, cancellation and other
  exceptions unchanged. Watch polling skips these unusable bodies without estimating
  and can accept the next valid reading; claims remain unconfirmed without replay.
  Regressions: tests/test_gql_http_decoding.py with real loopback aiohttp responses.
  Debug logging is unchanged. This does not validate operation
  data, redact all logs or guarantee exactly-once requests.
  Regression coverage: `tests/test_gql_reply_validation.py` and
  `tests/test_gql_retry_safety.py`.

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

- `HTTPClient.close()` closes its session and connector even when legacy cookie
  cleanup or persistence fails. Preserve the storage error, successful-save behavior,
  browser mode's legacy-cookie persistence bypass and idempotent completed cleanup.
  `tests/test_http_client_close.py` covers real filesystem failures and owned loopback
  connections without Twitch credentials or external requests.
  `tests/test_http_shutdown_persistence.py` combines real HTTP cleanup with miner
  owner draining, repeated caller cancellation, failed owner cleanup and explicit
  teardown retry. Storage errors still propagate; this is not automatic recovery
  or a guarantee that later identity teardown runs despite an error.

- `Channel.send_watch()` polls the lowest-bandwidth HLS playlist and sends HEAD requests
  for every new media segment, without downloading stream audio or video. `WatchService`
  polls about every ten seconds, within the rolling playlist window.
- Deduplicate successful segments in a bounded 256-entry cache per channel/broadcast;
  retain it and the cached playlist URL across same-broadcast metadata refreshes. Reset
  on a new broadcast; account replacement drains watch tasks and derived channel state.
  Retry failed segments, refresh expired playlist URLs, and stop stale batches when the
  selected channel or broadcast changes or goes offline. Bound the entire poll and
  each request; propagate cancellation and process exit. Never log signed URLs or bodies.
  GET/HEAD401/403/404 invalidates the failed cached playlist URL on both the request's
  stream object and a same-broadcast metadata replacement that copied that URL. Do not
  erase a different/newer cached URL or another broadcast. Reacquire on a later poll,
  without extra same-poll requests, clearing deduplication or resetting telemetry.
  This changes URL-cache retirement only, not stream observation ordering. Regressions:
  `tests/test_playlist_cache_invalidation.py`.
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
- `DropsCampaign.progress_revision` advances on every plain-integer `TimedDrop.update_minutes()`
  observation, including unchanged values, and before awaiting notification work on a
  successful claim transition. Sibling progress shares this revision. CurrentDrop polling
  snapshots drop identity and campaign revision before awaiting Twitch; discard replies
  superseded by observations, claims or model replacement/removal without estimating a
  minute. Snapshot the fallback campaign before the request too; null, errored or timed-out
  replies may estimate only against that same campaign and unchanged revision. Fresh
  polls can still correct progress downward. Keep this separate from estimated-minute
  bumps. Regression coverage: `tests/test_stale_progress.py`.
  `tests/test_progress_integration.py` also covers unchanged observations recovering
  capped rewards during pending polls: superseded session/null/error/timeout replies
  must not undo recovery or estimate another minute, while a fresh downward poll works.
- `DropsCampaign.bump_minutes()` must update all eligible selected-branch drops before testing whether
  any reached the estimate limit. Evaluate `_bump_minutes()` eagerly; a short-circuiting
  generator leaves later rewards unchanged. Preserve eligibility and the estimate cap,
  request at most one channel switch per batch, and display only after the batch.
  Regression coverage: `tests/test_campaign_estimates.py`.
  `tests/test_mining_fallback_integration.py` exercises the actual watch loop with
  selected/unselected campaigns, shared prerequisites and estimate-limit handoff;
  authoritative readings must remain distinct from fallback estimates.
  It also checks whole-batch cap recovery from fresh polls and websocket observations,
  including zero-delta corrections, stale replies and null/error/timeout fallbacks.
  Playlist/transport, lifecycle, and controlled-clock regressions live in
  `tests/test_playlist_watch.py`, `tests/test_watch_transport.py`, and
  `tests/test_watch_poll_cadence.py`.
- `WatchService._current_drop_reading()` validates returned CurrentDrop envelopes,
  nested user/session objects, nonempty string IDs and plain-integer minutes. An
  explicit error-free null session retains existing estimated fallback; malformed
  or errored replies skip that poll without progress updates or estimated bumps.
  Keep playlist/minute cadence, stream freshness, model eligibility and integer
  clamping intact. Do not trust partial data accompanying errors or log the reply.
  Catch parser failures only at that boundary, not exceptions from model updates or
  arbitrary transport failures. Existing GQLException/timeout fallback, cancellation
  and critical-task handling for unexpected failures remain unchanged. This does
  not harden every malformed HTTP/GraphQL response before it reaches the service.
  Regression coverage: `tests/test_current_drop_responses.py`, including real-model
  recovery, mocked-HTTP GQLClient paths, no-estimation and cancellation controls.
  `tests/test_mining_fallback_integration.py` combines unusable replies with capped
  batches and websocket observations, asserting unchanged state until the next fresh
  poll, continued playlist cadence, no critical shutdown and no owned task leaks.

### GraphQL Operations

- `GQLClient._nullify_server_error()` handles only the existing `server error`
  normalization branch. Response data must be an object and its path a nonempty
  list of nonempty string object keys or plain nonnegative in-range list indices.
  Traverse existing parents only; missing/null/scalar parents and unusable paths
  raise a fixed redacted `GQLException` without mutating the failed response or
  replaying the request. Preserve existing missing-leaf null insertion and unrelated
  batch entries. Do not coerce booleans, numeric strings, negative indices or floats.
  Retry/rate policies, cancellation and all other error branches remain unchanged.
  This does not validate every raw response envelope, normalize every error in a
  multi-error list, make batch mutations atomic or prove live progress recovery.
  Regression coverage: `tests/test_gql_server_error_paths.py` with real GQLClient,
  mocked HTTP/browser transports, safe controls and no mutation-request replay.
  `tests/test_gql_path_reliability.py` additionally checks real counter/estimate
  recovery and failed claims against the combined boundary/retry/finalization guards.

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

- Queued game recalculation at known starts/ends and strict one-hour admission events
  from `mining_time_triggers`, including normal claim-ready checks and discovery.
- Inventory reload at the cycle's configured interval, not an unconditional hourly
  read. Local wakeups keep that deadline anchored; interval edits apply next cycle.
- Early wakes retain events; late wakes coalesce elapsed events. Manual intent,
  mining hours, current inventory work and EXIT retain their normal precedence.

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

- Development tests require `pytest-asyncio>=1.4.0`; synchronize the locked `dev`
  extra before testing. Version 1.3 can orphan a restored default loop when a later
  unittest runner, `asyncio.run()` or `asyncio.Runner` clears it. This is a
  development-only dependency requirement, not a miner runtime change.
  `tests/test_asyncio_test_lifecycle.py` checks these boundaries, Runner reuse and
  Runner failure in fresh child pytest processes, with pytest-only and intentional-leak
  controls. Assert ownership before closing child-owned loops;
  never hide leaks with warning suppression, early cleanup or weakened assertions.
  Do not change the parent process's loop policy for these probes.

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

- The validation workflow runs `ruff check src/` without automatic fixes. CI must
  verify the submitted source rather than repair one job's checkout while other
  jobs build unchanged files. Preserve fail-fast behavior and do not make Ruff
  advisory. `tests/test_ci_lint_verification.py` executes the actual workflow step
  against clean, fixable and invalid fixtures, checking status and file preservation.

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
- `ChannelListManager.get_channels()` projects `watching` from the current watched
  ID instead of cached row flags, so REST and initial Socket.IO snapshots agree after
  start, switch and clear without another channel display update. Return fresh outer
  row dictionaries; preserve order, identity fields and existing watch events without
  mutating previously serialized payloads or changing mining selection. Regressions:
  `tests/test_channel_watching_snapshot.py`.
  The frontend replaces channel rows when an initial-state channel list is present,
  including an empty list, so reconnect cannot retain removed watched channels.
  Preserve omitted/null partial-event handling and incremental events. Regressions:
  `tests/test_channel_snapshot_frontend.py`. Release/cache-key bump required before
  deploying the changed app.js through the authorized release workflow.
  Initial-state campaign lists likewise replace browser inventory, including empty
  lists, so campaigns absent from the current server snapshot cannot linger after
  reconnect. Preserve filters, omitted/null partial-event behavior, incremental
  campaign/drop events and batch updates. This changes display synchronization only,
  not campaign eligibility, progress, claims or server inventory. Regressions:
  `tests/test_inventory_snapshot_frontend.py`.
- Full `initial_state.console` lists replace the displayed rolling history, including
  an empty list, instead of replaying old lines on reconnect. Preserve literal text,
  repeated real messages, live append events, omitted/null partial-event handling and
  the existing 1,000-line limit. Do not deduplicate log text or change backend logging.
  Regressions: `tests/test_console_snapshot_frontend.py`. An authorized release/cache-key
  bump is required before deployment of the changed frontend asset.
- `fetchAndApplyTranslations()` admits only the latest request's completion, checking
  after JSON decoding and before logging failures. Non-success HTTP responses retain
  existing labels instead of replacing translations with an error document. Preserve
  normal translation application and current-request diagnostics; do not mutate
  settings, language persistence or language-list loading. Regressions:
  `tests/test_translation_response_ordering.py`. The frontend release/cache-key gate
  still applies before deployment. This is not a settings transaction or response
  schema validation change.
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
- Keep metadata-only `BUILD_DATE`, `VCS_REF` and `VERSION` declarations and OCI
  labels after filesystem-building instructions so metadata changes do not invalidate
  dependency/source caches. Retain all label names, empty defaults and runtime
  configuration. These arguments do not set the Python package version or refresh
  packages; security rebuilds must still use `--pull --no-cache`. The Docker release
  workflow sets `pull: true` and `no-cache: true` explicitly; validation retains its
  cache and does not publish. Never dispatch a release as part of cache testing.
  Regressions:
  `tests/test_docker_metadata_cache.py` plus actual metadata-only builds on both
  architectures, verifying labels, unchanged runtime configuration and shared layers.
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
Validate the persisted history envelope and every row before publishing entries.
After validation, keep the first valid row for each exact drop ID in insertion order.
Invalid rows must not reserve IDs; duplicate records must not inflate counts, filters,
statistics or CSV/API results. Do not normalize opaque IDs or deduplicate by names or
benefits. Log separate aggregate invalid/duplicate counts without row values. Loading
never rewrites the file; a later normal save persists the accepted unique records.
Regressions: `tests/test_history_duplicate_loading.py`.
Skip malformed rows while preserving valid insertion order; require aware claim times
and normalize them to UTC without inventing dates or claim fields. Loading never
rewrites the original file. A later normal save writes the accepted records plus new
claims; this is not a backup or repair facility. Reject boolean/fractional schema
versions and minute counts, but retain zero-minute records and empty benefit lists.
Require UTF-8 encodable text, including benefit names; escaped lone surrogates are
invalid even when the JSON file itself decodes. Keep valid supplementary Unicode.
Project only documented history fields, never arbitrary persisted extras. Log fixed
shape errors or aggregate counts, never persisted values or parser errors.
`DropHistory.clear()` returns success only after the empty history file is atomically
replaced. On storage failure restore the previous in-memory records and return false;
the DELETE endpoint returns a fixed HTTP 503 error so the existing UI retains its data.
Keep ordinary claim recording best-effort; save failures must not undo successful
Twitch claims. Save errors must not expose filesystem paths or exception text.
New claim records use the same row parser before appending or saving. Reject invalid
text, minute requirements and benefit names with a fixed warning and no raw values;
do not poison the live history, reserve the rejected ID, overwrite the saved file or
interrupt confirmed-claim finalization. Preserve duplicate short-circuiting, valid
Unicode/zero-minute records, ordinary best-effort save behavior and later valid
records. This does not validate all live campaign metadata or recover skipped history.
Regressions: `tests/test_history_record_admission.py`.
History statistics requests use their own generation counter. Closing/reopening the
panel or successfully deleting history invalidates older success and failure replies,
including delayed JSON decoding. A failed or cancelled deletion must not invalidate
the current statistics request. Preserve the separate history-list request guard.
Regressions: `tests/test_history_stats_ordering.py`. No polling or server mutations
are added; the existing frontend asset version/cache-key release gate still applies.

- Existing-session admission accepts only a bounded sole `token` field through
  dashboard-authenticated, same-origin `POST /api/session/token`. Never echo inputs.
  Validate WEB identity before fresh SDK bootstrap and revalidate account/catalog
  before atomic installation; logout and dashboard revocation invalidate admission.
  Expired SDK cookies are not reused: validate the saved OAuth identity and bootstrap
  a new owned context. OAuth identity alone never marks a session ready.
- Native noVNC lives in a same-origin owned iframe, fixed to `/api/session/vnc` with
  reconnection disabled. Parent messages contain fixed viewer events only. Override
  native clipboard logging, bound transfers, ignore remote text while the panel is
  closed, and clear text on send/close/disconnect. Preserve attempt teardown.
