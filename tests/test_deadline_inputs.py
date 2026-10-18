"""Project real models without treating estimates as direct Twitch evidence."""

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from src.core.client import Twitch
from src.models.campaign import DropsCampaign
from src.services.deadline_certificate import (
    CertifiedDeadlineForecaster,
    DropTimingBounds,
    DurationBounds,
    TimingBounds,
)
from src.services.deadline_forecast import MiningWindow, ViewingWindow
from src.services.deadline_inputs import DeadlineInputAdapter, MiningHoursProjector, ProjectionError
from src.utils.schedule import window_status
from tests.test_deadline_forecast import NOW
from tests.test_drop_ignore_policy import _drop


def client(*, hours=None):
    twitch = object.__new__(Twitch)
    twitch._claim_requests = 0
    twitch.settings = SimpleNamespace(
        games_to_watch=["High", "Urgent"], drop_name_blacklist=[],
        mining_benefits={"DIRECT_ENTITLEMENT": True}, allow_unlinked_campaigns=False,
        priority_list_only=True,
        mining_hours=hours or {"mode": "always", "start": "08:00", "end": "22:00"},
    )
    twitch.inventory = []
    twitch._manual_target_channel = twitch._manual_target_game = None
    return twitch


def campaign(twitch, key, game, *, minutes=0, required=480, deadline=48, drops=None):
    data = _drop("reward", "Reward", required_minutes=required)
    if minutes is not None:
        data["self"] = {
            "dropInstanceID": "test-claim", "isClaimed": False,
            "currentMinutesWatched": minutes,
        }
    result = DropsCampaign(twitch, {
        "id": key, "name": key, "game": {"id": "1" if game == "High" else "2", "name": game},
        "self": {"isAccountConnected": True}, "accountLinkURL": "https://example.test/link",
        "startAt": (NOW - timedelta(days=1)).isoformat(),
        "endAt": (NOW + timedelta(hours=deadline)).isoformat(), "status": "ACTIVE",
        "allow": {"channels": [], "isEnabled": True}, "timeBasedDrops": drops or [data],
    }, {})
    # Receipt time is explicit test evidence; capture must copy it, never refresh it.
    for drop in result.drops:
        if drop.progress_observation is not None:
            drop._progress_observation["received_at"] = NOW.isoformat()
    twitch.inventory.append(result)
    return result


def capture(twitch, **kwargs):
    return DeadlineInputAdapter().capture(twitch, now=NOW, horizon=NOW + timedelta(days=3), **kwargs)


def require_zone(name):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        pytest.skip("Installed IANA timezone data required for this case")


def test_real_model_projection_scopes_ids_clips_campaign_and_retains_unknown_progress():
    twitch = client()
    high = campaign(twitch, "high", "High", minutes=None)
    urgent = campaign(twitch, "urgent", "Urgent", minutes=10, required=120, deadline=4)
    high_drop, urgent_drop = next(iter(high.drops)), next(iter(urgent.drops))
    high_drop.real_current_minutes = 450
    high_drop.extra_current_minutes = 20
    urgent_drop.real_current_minutes = 80
    urgent_drop.extra_current_minutes = 5

    result = capture(twitch)

    assert result.reason == "captured"
    projected = result.inputs
    first, second = projected.drops
    assert first.id != second.id
    assert first.observed_minutes is None and first.observed_at is None
    assert second.observed_minutes == 10 and second.observed_at == NOW
    assert second.ends_at == urgent.ends_at
    assert first.starts_at == high.starts_at
    assert projected.baseline_complete is False
    assert projected.ranked_game_ids == (1, 2)
    assert twitch.settings.games_to_watch == ["High", "Urgent"]
    assert (high_drop.real_current_minutes, high_drop.extra_current_minutes) == (450, 20)
    with pytest.raises(FrozenInstanceError):
        projected.drops = ()


def test_locked_chain_keeps_unclaimed_parent_without_mutating_claim_flags():
    twitch = client()
    parent = _drop("parent", "Parent", has_benefit=False)
    target = _drop("target", "Target", preconditions=("parent",))
    item = campaign(twitch, "chain", "Urgent", drops=[parent, target])
    assert not item.timed_drops["target"].preconditions_met

    projected = capture(twitch).inputs.drops

    assert len(projected) == 2
    assert [drop.wanted for drop in projected] == [False, True]
    assert projected[1].prerequisite_ids == (projected[0].id,)
    assert all(not drop.is_claimed for drop in item.drops)


def test_claimed_zero_minute_parent_is_settled_not_faked_as_watch_work():
    twitch = client()
    parent = _drop("parent", "Parent", claimed=True, required_minutes=0)
    target = _drop("target", "Target", preconditions=("parent",))
    item = campaign(twitch, "chain", "Urgent", drops=[parent, target])
    projected = capture(twitch).inputs.drops
    assert len(projected) == 1 and projected[0].prerequisite_ids == ()
    assert item.timed_drops["parent"].required_minutes == 0


@pytest.mark.parametrize("blocker", ["expired", "invalid", "unlinked", "ignored", "unwanted", "unselected"])
def test_real_policy_gates_exclude_ineligible_roots(blocker):
    twitch = client()
    item = campaign(twitch, "one", "High")
    if blocker == "expired":
        item.ends_at = NOW
    elif blocker == "invalid":
        item._valid = False
    elif blocker == "unlinked":
        item.linked = False
    elif blocker == "ignored":
        twitch.settings.drop_name_blacklist = ["Reward"]
    elif blocker == "unwanted":
        twitch.settings.mining_benefits = {"DIRECT_ENTITLEMENT": False}
    else:
        twitch.settings.games_to_watch = ["Other"]
    assert capture(twitch).inputs.drops == ()


def test_unlinked_override_and_badge_eligibility_use_real_campaign_policy():
    twitch = client()
    item = campaign(twitch, "one", "High")
    item.linked = False
    twitch.settings.allow_unlinked_campaigns = True
    assert len(capture(twitch).inputs.drops) == 1


@pytest.mark.parametrize("manual_channel,manual_game", [(object(), object()), (object(), None), (None, object())])
def test_partial_or_complete_manual_target_blocks_automatic_capture(manual_channel, manual_game):
    twitch = client()
    twitch._manual_target_channel, twitch._manual_target_game = manual_channel, manual_game
    result = capture(twitch)
    assert result.reason == "manual_override" and result.inputs is None


def test_inventory_replacement_produces_new_values_not_rebound_old_projection():
    twitch = client()
    campaign(twitch, "one", "High", minutes=5)
    old = capture(twitch).inputs
    twitch.inventory.clear()
    campaign(twitch, "one", "High", minutes=11)
    assert capture(twitch).inputs.drops[0].observed_minutes == 11
    assert old.drops[0].observed_minutes == 5


def test_real_models_can_feed_conditional_certificate_with_explicit_assumptions_only():
    twitch = client()
    campaign(twitch, "high", "High")
    campaign(twitch, "urgent", "Urgent", required=120, deadline=4)
    projected = capture(twitch).inputs
    views = tuple(ViewingWindow(str(drop.game_id), NOW, projected.horizon,
                                frozenset({drop.id}), allocation_known=True) for drop in projected.drops)
    snapshot = projected.to_forecast(claim_delays={drop.id: timedelta(seconds=20) for drop in projected.drops},
                                     viewing_windows=views)
    bounds = TimingBounds(tuple(DropTimingBounds(
        drop.id, DurationBounds(timedelta(minutes=drop.required_minutes), timedelta(minutes=drop.required_minutes)),
        DurationBounds(timedelta(0), timedelta(seconds=20)),
    ) for drop in projected.drops), DurationBounds(timedelta(0), timedelta(seconds=10)))
    engine = CertifiedDeadlineForecaster()
    assert engine.certify(snapshot, bounds, enabled=True).reason == "unknown_coverage"
    result = engine.certify(snapshot, bounds, enabled=True, baseline_complete=True)
    assert result.certificate.target_id == projected.drops[1].id


def test_claim_timing_is_not_invented_when_compiling_inputs():
    twitch = client()
    campaign(twitch, "one", "High")
    with pytest.raises(ValueError, match="claim timing"):
        capture(twitch).inputs.to_forecast(claim_delays={}, viewing_windows=())


@pytest.mark.parametrize("start,end,expected", [("08:00", "10:00", ((8, 10),)),
                                               ("22:00", "06:00", ((0, 6), (22, 24))),
                                               ("08:00", "08:00", ((0, 24),))])
def test_daily_hours_projection_matches_runtime_wall_clock_rules(start, end, expected):
    begin = datetime(2026, 10, 17, tzinfo=timezone.utc)
    windows = MiningHoursProjector().project(
        {"mode": "range", "start": start, "end": end}, begin, begin + timedelta(days=1), timezone.utc,
    )
    assert windows == tuple(MiningWindow(begin + timedelta(hours=a), begin + timedelta(hours=b)) for a, b in expected)


@pytest.mark.parametrize("day,start,end,expected", [
    ((2026, 3, 8), "02:30", "03:30", ((7, 7.5),)),
    ((2026, 11, 1), "01:30", "02:30", ((5.5, 6), (6.5, 7.5))),
    ((2026, 11, 1), "00:45", "01:15", ((4.75, 5.25), (6, 6.25))),
])
def test_dst_gaps_and_repeated_hours_use_actual_utc_intervals(day, start, end, expected):
    require_zone("America/New_York")
    begin = datetime(*day, tzinfo=timezone.utc)
    windows = MiningHoursProjector().project(
        {"mode": "range", "start": start, "end": end}, begin, begin + timedelta(hours=10), "America/New_York",
    )
    assert windows == tuple(MiningWindow(begin + timedelta(hours=a), begin + timedelta(hours=b)) for a, b in expected)


@pytest.mark.parametrize("zone", [None, "Not/AZone", timezone(timedelta(seconds=21))])
def test_unknown_or_subminute_zone_is_not_assumed_utc(zone):
    with pytest.raises(ProjectionError) as exc:
        MiningHoursProjector().project({"mode": "range", "start": "08:00", "end": "22:00"},
                                       NOW, NOW + timedelta(hours=2), zone)
    assert exc.value.reason == "unknown_timezone"


@pytest.mark.parametrize("zone,begin,start,end", [
    ("America/New_York", datetime(2026, 11, 1, 4, tzinfo=timezone.utc), "00:45", "01:15"),
    ("America/New_York", datetime(2026, 3, 8, 5, tzinfo=timezone.utc), "02:30", "03:30"),
    ("Australia/Lord_Howe", datetime(2026, 4, 4, 12, tzinfo=timezone.utc), "01:45", "02:15"),
    ("Asia/Kathmandu", datetime(2026, 10, 17, 0, 0, 30, 123456, tzinfo=timezone.utc), "08:00", "09:00"),
])
def test_utc_membership_matches_runtime_oracle_across_offset_changes(zone, begin, start, end):
    timezone_data = require_zone(zone)
    finish = begin + timedelta(hours=8)
    windows = MiningHoursProjector().project({"mode": "range", "start": start, "end": end}, begin, finish, zone)
    cursor = begin
    while cursor < finish:
        expected, _ = window_status(start, end, cursor.astimezone(timezone_data))
        assert any(window.starts_at <= cursor < window.ends_at for window in windows) is expected
        cursor += timedelta(seconds=7, microseconds=131)


def test_stale_direct_reading_is_preserved_not_refreshed_or_replaced_by_display():
    twitch = client()
    item = campaign(twitch, "one", "High", minutes=5)
    drop = next(iter(item.drops))
    old = NOW - timedelta(minutes=6)
    drop._progress_observation["received_at"] = old.isoformat()
    drop.real_current_minutes, drop.extra_current_minutes = 50, 4
    projected = capture(twitch).inputs.drops[0]
    assert projected.observed_minutes == 5 and projected.observed_at == old


@pytest.mark.parametrize("field,value", [
    ("minutes", True), ("minutes", -1), ("minutes", 500),
    ("source", "estimated"), ("received_at", "2026-01-01"),
    ("received_at", (NOW + timedelta(seconds=1)).isoformat()),
])
def test_invalid_direct_evidence_remains_unknown(field, value):
    twitch = client()
    item = campaign(twitch, "one", "High", minutes=5)
    next(iter(item.drops))._progress_observation[field] = value
    projected = capture(twitch).inputs.drops[0]
    assert projected.observed_minutes is projected.observed_at is projected.observed_source is None


def test_projection_budgets_refuse_without_partial_results():
    twitch = client()
    campaign(twitch, "one", "High")
    campaign(twitch, "two", "Urgent")
    result = DeadlineInputAdapter(max_records=1).capture(twitch, now=NOW, horizon=NOW + timedelta(days=3))
    assert result.reason == "budget_exceeded" and result.inputs is None
    with pytest.raises(ProjectionError) as exc:
        MiningHoursProjector(max_minutes=1).project(
            {"mode": "range", "start": "08:00", "end": "22:00"}, NOW, NOW + timedelta(hours=2), timezone.utc,
        )
    assert exc.value.reason == "budget_exceeded"


@pytest.mark.parametrize("hours", [None, {}, {"mode": "range", "start": "25:00", "end": "08:00"}])
def test_invalid_hours_do_not_become_always_open(hours):
    with pytest.raises(ProjectionError) as exc:
        MiningHoursProjector().project(hours, NOW, NOW + timedelta(hours=2), timezone.utc)
    assert exc.value.reason == "invalid_snapshot"


def test_claimed_state_and_policy_changes_are_captured_fresh():
    twitch = client()
    item = campaign(twitch, "one", "High")
    old = capture(twitch).inputs
    next(iter(item.drops)).is_claimed = True
    assert capture(twitch).inputs.drops == ()
    assert len(old.drops) == 1


@pytest.mark.parametrize("kind", ["cycle", "missing_parent", "unclaimed_zero"])
def test_branches_rejected_by_real_mineability_policy_are_not_reopened(kind):
    twitch = client()
    if kind == "cycle":
        drops = [_drop("a", "A", preconditions=("b",)), _drop("b", "B", preconditions=("a",))]
    elif kind == "missing_parent":
        drops = [_drop("a", "A", preconditions=("missing",))]
    else:
        drops = [_drop("a", "A", required_minutes=0), _drop("b", "B", preconditions=("a",))]
    item = campaign(twitch, "one", "High", drops=drops)
    assert not item.mineable_drop_ids
    assert capture(twitch).inputs.drops == ()
    assert all(not drop.is_claimed for drop in item.drops)


@pytest.mark.parametrize("change", ["names", "benefits", "inventory", "naive", "horizon"])
def test_invalid_capture_state_never_returns_partial_inputs(change):
    twitch = client()
    campaign(twitch, "one", "High")
    now, horizon = NOW, NOW + timedelta(days=3)
    if change == "names":
        twitch.settings.games_to_watch = [None]
    elif change == "benefits":
        twitch.settings.mining_benefits = {"DIRECT_ENTITLEMENT": "false"}
    elif change == "inventory":
        twitch.inventory = None
    elif change == "naive":
        now = now.replace(tzinfo=None)
    else:
        horizon = NOW + timedelta(days=15)
    result = DeadlineInputAdapter().capture(twitch, now=now, horizon=horizon)
    assert result.reason == "invalid_snapshot" and result.inputs is None


@pytest.mark.parametrize("delays", [{"unknown": timedelta(0)}, {"placeholder": -1}])
def test_compilation_requires_exact_known_claim_map(delays):
    twitch = client()
    campaign(twitch, "one", "High")
    with pytest.raises(ValueError, match="claim timing"):
        capture(twitch).inputs.to_forecast(claim_delays=delays, viewing_windows=())


def test_partial_utc_minutes_and_equivalent_offsets_preserve_instant():
    begin = datetime(2026, 10, 17, 7, 59, 59, 999999, tzinfo=timezone.utc)
    finish = begin + timedelta(microseconds=2)
    zone = timezone(timedelta(hours=4))
    windows = MiningHoursProjector().project(
        {"mode": "range", "start": "08:00", "end": "09:00"},
        begin.astimezone(zone), finish.astimezone(zone), timezone.utc,
    )
    assert windows == (MiningWindow(begin + timedelta(microseconds=1), finish),)


def test_timezone_is_not_needed_for_always_open_or_equal_boundaries():
    engine = MiningHoursProjector()
    for mode, start, end in (("always", "08:00", "22:00"), ("range", "08:00", "08:00")):
        assert engine.project({"mode": mode, "start": start, "end": end}, NOW,
                              NOW + timedelta(hours=1), None) == (MiningWindow(NOW, NOW + timedelta(hours=1)),)


def test_missing_installed_timezone_data_fails_closed(monkeypatch):
    def unavailable(name):
        raise ZoneInfoNotFoundError

    monkeypatch.setattr("src.services.deadline_inputs.ZoneInfo", unavailable)
    twitch = client(hours={"mode": "range", "start": "08:00", "end": "22:00"})
    campaign(twitch, "one", "High")
    result = capture(twitch, local_timezone="America/New_York")
    assert result.reason == "unknown_timezone" and result.inputs is None


def test_oversized_dependency_lists_are_rejected_before_policy_work(monkeypatch):
    twitch = client()
    campaign(twitch, "one", "High", drops=[_drop("reward", "Reward", preconditions=("missing",) * 257)])
    called = []

    def should_not_evaluate(*args):
        called.append(True)
        return SimpleNamespace(mineable_ids=frozenset())

    monkeypatch.setattr("src.services.deadline_inputs.DropIgnorePolicy.evaluate", should_not_evaluate)
    result = capture(twitch)
    assert result.reason == "budget_exceeded" and result.inputs is None
    assert called == []


@pytest.mark.parametrize("field,value", [
    ("is_claimed", None), ("is_claimed", 0), ("is_claimed", 1), ("is_claimed", "false"),
    ("required_minutes", True), ("required_minutes", "15"), ("required_minutes", -1),
    ("precondition_drops", [None]),
])
def test_invalid_graph_values_are_rejected_before_policy_evaluation(monkeypatch, field, value):
    twitch = client()
    item = campaign(twitch, "one", "High")
    setattr(next(iter(item.drops)), field, value)

    def should_not_evaluate(*args):
        raise AssertionError("Invalid graph reached policy evaluation")

    monkeypatch.setattr("src.services.deadline_inputs.DropIgnorePolicy.evaluate", should_not_evaluate)
    result = capture(twitch)
    assert result.reason == "invalid_snapshot" and result.inputs is None
