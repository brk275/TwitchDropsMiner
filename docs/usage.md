# Using the dashboard

After TDM restores your session or accepts a [browser login](authentication.md), wait
for it to discover campaigns. Choose the games and rewards you want, then leave the
miner running while it selects eligible channels and tracks progress.

Link the correct game account to receive in-game rewards. TDM skips campaigns
reported as Not Linked by default, except for Twitch badge/emote campaigns. Check the campaign
on [Twitch Drops](https://www.twitch.tv/drops/campaigns) for eligibility and linking
instructions. Avoid watching Twitch manually with the same account while TDM runs;
simultaneous viewing can disrupt progress.

## Find your way around

| Tab | Purpose |
| --- | --- |
| Main | Login status, the current campaign and drop, wanted rewards, console output, and channels |
| Inventory | Campaigns and rewards, with status, benefit, and game filters |
| History | Locally recorded claims, statistics, and CSV export |
| Settings | Game priorities, reward types, ignored names, language, login access, and notifications |
| Help | A quick explanation of mining and Telegram setup |

## Choose games and priorities

In **Settings → Games to Watch**, put the games you most want first. Drag a game to
reorder it, or enter a whole-number priority. **1** is highest. Numbers beyond the
list's bounds move the game to the nearest end; blank or fractional values leave its
position unchanged.

Search for a game and press **Enter** or select **Add Game**. An exact name or unique
partial match selects an available game. If several names match, use a more specific
search. You can also confirm adding a manually entered name. Select **Reload** after
adding games to refresh the miner's selection.

**Select All** keeps your existing priority order and manual entries, then adds missing
games. **Deselect All** asks for confirmation. You can cancel a confirmation with
**Escape**. Your choices are saved for future runs.

Version 2.3.0 adds **Settings → General → Priority list only**, enabled by default.
Leave it enabled to mine only games in **Games to Watch**. Disable it to prefer
those games, then mine other eligible games whenever no selected game can currently
be progressed. Other games stay out of your saved list, so you can keep a short list
of favorites. With an empty list, disabling this option allows all eligible games.
Fallback games with campaigns ending soonest are preferred. Account linking, reward
types, ignored names, mining hours and other eligibility rules still apply. Manual
channel selection retains its existing behavior.

**Mining Benefits** controls which reward types the miner targets: items, badges,
emotes, and other rewards. These choices are separate from the display filters on
the Inventory tab.

Source builds target wanted reward branches consistently across channel selection,
queueing and local progress estimates. A disabled reward type can still be mined
when it is a prerequisite for a wanted reward, but an unrelated disabled reward
does not justify watching just because a wanted reward for the same game starts
later. The usual one-hour lookahead applies to the wanted reward itself. Currently
usable channels are retained ahead of future-only work when the tracking limit is
reached; normal game ranking and channel eligibility still apply.

These preferences do not discard progress Twitch reports for other rewards or
prevent claiming already-earned rewards. Local estimates are not evidence of Twitch
credit. Reward selection alone does not change inventory refresh timing or guarantee
that a stream will remain available.

### Smart deadline detours

This fork's source builds support `TDM_SMART_DEADLINES=true` in the container
environment (off by default; restart after changing it). Your saved rankings stay
unchanged. Automatic selection can temporarily prioritize a lower-ranked reward
ending within 24 hours when its remaining safety margin is below 30 minutes or
the estimated time to the next higher-ranked reward. The miner returns to ranked
order after the urgent reward is claimed or loses eligibility/available channels.

Budgets use Twitch-reported minutes, each unclaimed prerequisite's own window and
a 15-minute safety reserve. Local estimated minutes never reduce those budgets.
Independent prerequisites are budgeted serially, which can conservatively reject
work that Twitch might progress in parallel. A detour is rejected if it would put
any currently feasible higher-ranked reward branch outside its window. Equal
deadlines preserve rank. Once selected, a feasible detour is retained until its
claim or loss of eligibility, unless an earlier safe deadline takes precedence.

Only currently available participating channels qualify. Manual mode and restricted
daily mining-hour ranges suspend detours. Unlisted fallback games cannot preempt
selected games. Tracked channels are reconsidered about once a minute, with fresh
discovery about every five minutes while enabled; normal inventory refresh remains
unchanged. It cannot discover a campaign absent from the last Twitch inventory,
guarantee that Twitch credits a particular reward, or guarantee completion through
outages, stale observations or channel availability changes. Already-unfinishable
branches do not trigger a deadline detour; normal ranked mining remains available.

## Channels and manual selection

TDM chooses among live channels that can earn your selected rewards. Game priorities,
campaign participation, and channel eligibility all affect the choice.

You can select an eligible channel from the Main tab. Switching to a different game
while already watching enters **Manual Mode**, which keeps that game as the target.
If the channel becomes unavailable, TDM tries another eligible channel for that game.
Use **Return to Auto Mode** to resume normal game priorities. Manual mode also ends
when its game has no remaining eligible drops or no suitable channel is available.

Source builds keep the manual game pinned across live channel updates. If the
watched channel changes category, TDM tries an eligible replacement for the original
game. Without one, it resumes automatic selection among tracked channels in the
same pass. An explicit eligible channel choice can replace the manual target;
choosing an uncategorized special-event participant clears the previous manual
target. Returning to automatic mode also removes the manual game's temporary
priority boost. Your saved game rankings remain unchanged.

Source builds ignore a delayed bulk channel reply when a newer stream observation,
offline event, channel removal or account retirement has already superseded it.
This also applies to same-broadcast replacements and repeated offline observations.
When rediscovery removes channel subscriptions, pending stream lookups lose their
authority to publish or redirect watching afterward. Existing stream details remain
available until the normal batch dashboard update. Other channels whose subscriptions
remain tracked continue their own refreshes.
It does not change game rankings, refresh intervals or reward eligibility, and does
not establish that Twitch has credited viewing time.

### Now Watching and dashboard updates

**Show thumbnail** is off on each page load. Enabling it displays a Twitch JPEG
thumbnail, refreshed on channel updates at most once per minute, without playing
video or audio. Hover over shortened account or renewal text in the header to read
it in full. Navigation, account controls and action buttons adapt to narrow screens.

Source builds replace channel, campaign and console history snapshots when the
dashboard reconnects, rather than retaining removed rows or replaying the entire
console buffer. Newer translation requests also take precedence over older replies.
These display updates do not establish that Twitch is crediting progress.

### Special Events and IRL campaigns

Include the campaign's category in **Games to Watch**. These campaigns can progress
on their listed participating channels even when a channel streams another category
or lacks the drops-enabled flag. Channels must still be live and eligible. A campaign
without an enabled participating-channel list requires the usual matching category.

Participants streaming categories outside Games to Watch have the lowest automatic
priority within their mining group. When fallback is enabled, eligible selected
campaigns still take precedence over games outside your saved list. If the watched
participant goes offline or becomes ineligible, another
eligible participant can take over.

## Inventory and the wanted queue

Inventory's **Active**, **Upcoming**, and **Expired** filters include campaigns with
any selected status. **Account link → All / Linked / Not Linked** narrows those
results using Twitch-reported link status. All adds no link restriction; Linked and
Not Linked select their respective states. This display filter does not change mining
eligibility or the account-link override. Fully claimed campaigns stay hidden until **Finished** is selected.
Use the benefit filters and game search to narrow the display further.

Zero-minute subscription rewards are omitted from Inventory and the Wanted Drops
Queue because they cannot be earned by watching. The queue also omits individually
expired rewards and rewards the miner cannot target. Upcoming and sequential rewards
can remain visible, so a queued reward is not necessarily earnable immediately.
Successful claims refresh the queue.

## Progress display

A Watching label, a moving local estimate or accepted watch telemetry is not proof
of earning a drop. Compare with your [Twitch Drops inventory](https://www.twitch.tv/drops/inventory).
Upcoming and sequential rewards may be queued before they can actually progress.

Source builds poll progress about once a minute while successfully watching.
Replies superseded by newer observations or inventory replacement are ignored.
Fresh eligible Twitch readings can replace capped local estimates; non-integer
minute updates are ignored, while integer corrections retain their existing bounds.
Local campaign guesses use the runtime wanted queue, but their estimates remain
estimates, not Twitch confirmation. See [recovery behavior](troubleshooting.md#source-build-recovery).

Source builds keep already-claimed rewards out of the active progress display and
clear the remaining-time display when watching stops. A late claim update for a
different reward does not clear the current reward. Inventory and local history
remain available separately.

## Account-link override

In Settings, **Allow mining campaigns reported as Not Linked** is off by default.
Enable it to attempt mining when Twitch reports a campaign as unlinked. Disabling
it restores the default account-link requirement. Changes are saved and refresh
game selection. The override leaves Twitch-reported link badges and Inventory
filters unchanged; all other campaign, drop, channel, priority, and ignore rules
still apply.

This option does not connect accounts or guarantee Twitch progress, claims, or
in-game rewards. TDM can display estimated minutes even without confirmed Twitch
progress. Check your [Twitch Drops inventory](https://www.twitch.tv/drops/inventory)
and link the correct game account to receive rewards. Do not unlink accounts just
to troubleshoot a reported status disagreement.

## Ignore unwanted reward names

In **Settings → Ignored Drop Keywords**, enter one literal part of a drop name per
line. Matching ignores case; these are plain substrings, not regular expressions.
Blank lines and surrounding whitespace are removed, and duplicate entries are combined.
The list is empty by default.

A matching unclaimed reward and unclaimed rewards that depend on it are ignored.
Prerequisite-only branches with no remaining wanted reward are skipped, but a
prerequisite shared by an allowed reward can still be mined. The dashboard distinguishes
ignored and skipped rewards from claimed rewards.

These rules control what TDM intentionally targets. Twitch may still award simultaneous
progress to an ignored reward while another reward advances.

## Drop history

The **History** tab records successful claims made by the miner in
`data/drop_history.json`. Claims that were already completed on Twitch are not backfilled.

Filter by game name or a **claimed on or after** date, browse the pages, and view
per-game and per-month statistics. The date filter begins at midnight UTC on the
selected day; displayed claim times use your browser's local timezone.

**Export CSV** downloads the current filtered view. The file supports Unicode game
names and includes a UTF-8 byte-order mark for spreadsheet compatibility.
**Clear** deletes all local history, including records hidden by a filter. It does
not change your Twitch account or take away rewards.

In source builds, a failed deletion leaves the records visible, and older pending
statistics replies cannot restore totals after a successful deletion. Confirmed claim
history is recorded before waiting for dashboard or notification delivery. Interrupted
alerts are not retried automatically.

Source builds skip malformed saved rows and keep the first valid record per exact
drop ID. Loading does not rewrite the file; a later normal save persists the accepted
records. Back up `data/drop_history.json` before repairing it or recording new claims
after a history-load warning. Newly recorded rows are validated too; malformed rows
are skipped rather than interrupting confirmed-claim handling or later saves.

History storage is best-effort. A disk-write failure can leave a record only in
memory until a later successful save, and a skipped row is not recovered automatically.
A missing local row does not by itself mean the Twitch claim failed. Keep the data
directory writable and check storage errors without deleting session files.

## Refreshing and clearing cache

Use **Reload** after changing the games you want or when you need to refresh campaign
selection. **Settings → Clear All Cache** discards derived campaign and channel data,
then reloads it from Twitch. It preserves Twitch login, settings, and dashboard
password protection.

Source builds accept whole-minute inventory refresh intervals from 1 to 1,440.
Invalid saved intervals use the 30-minute default. An interval does not guarantee
immediate discovery of campaigns announced between refreshes.

Source builds also reassess known campaigns at their start/end times and just after
rewards enter the normal one-hour selection lookahead. This lets earlier required
drops become selectable without waiting for the next inventory read. These wakeups
use normal game selection, claim-ready checks and channel searches; saved rankings,
reward filters, manual mode and mining hours still apply. They do not widen the
lookahead, prove Twitch credit or guarantee that a participating channel is available.

The periodic inventory deadline stays unchanged. Editing its interval applies to
the next cycle. Campaigns not yet returned by Twitch still require an inventory read.

Clearing cache is useful for recovery; it cannot correct inaccurate campaign details
returned by Twitch. If progress or eligibility still looks wrong, follow
[Troubleshooting](troubleshooting.md).

## Mining hours

Version 2.3.0 adds **Settings → Mining Hours**. **All day (24/7)** is the default.
Choose **Custom time range** and enter start and end times to watch only during that
window each day. The start is included and the end is excluded. A range such as
22:00–06:00 crosses midnight; equal start and end times mean all day.

Times use the local timezone of the device running TDM, not the dashboard browser.
For Docker, configure `TZ` to your home internet connection’s timezone, as described
in the [installation guide](installation.md). Clock or daylight-saving changes are
rechecked at least once per minute; normal start/end boundaries are scheduled directly.

Outside the window, the status shows **Outside mining hours** and watching stops.
The miner stays signed in. Reloads, cache clearing, and mining-policy recalculations
wait until the next window, when TDM fetches fresh inventory and resumes selection.
Changing the hours takes effect immediately after a successful save. Blank or invalid
times are rejected; save failures keep the previous schedule and show an error.
Malformed saved schedules are rejected at startup rather than silently mining all day.

This setting pauses watching, not Twitch reward claims: already earned rewards can
still be claimed through existing notifications or work already underway. It does
not guarantee Twitch-side progress. Reload the dashboard after upgrading to load the
versioned frontend controls.

[All guides](README.md) · [Telegram notifications](notifications.md)
