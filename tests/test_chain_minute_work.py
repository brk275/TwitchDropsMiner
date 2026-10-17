"""Bound repeated chain work without changing the existing minute totals."""

from types import SimpleNamespace

import pytest

from src.services.inventory_service import InventoryService
from tests.test_drop_ignore_policy import _campaign, _drop


def dense_campaign(size=12):
    return _campaign([
        _drop(str(index), f"Reward {index}", preconditions=tuple(str(parent) for parent in range(index)))
        for index in range(size)
    ])


def count_parents(campaign):
    visits = []

    class CountedParents(list):
        def __iter__(self):
            visits.append(True)
            return super().__iter__()

    for drop in campaign.drops:
        drop.precondition_drops = CountedParents(drop.precondition_drops)
    return visits


@pytest.mark.parametrize("metric", ["total_required_minutes", "total_remaining_minutes"])
@pytest.mark.parametrize("completed_parents", [False, True])
def test_shared_acyclic_chains_do_not_expand_every_path(metric, completed_parents):
    size = 12
    campaign = dense_campaign(size)
    if completed_parents:
        for drop in tuple(campaign.drops)[:-1]:
            drop.real_current_minutes = drop.required_minutes
    visits = count_parents(campaign)

    expected = 15 if completed_parents and metric == "total_remaining_minutes" else 15 * size
    assert getattr(campaign.timed_drops[str(size - 1)], metric) == expected
    assert len(visits) <= 4 * size


def test_active_campaign_sorting_retains_choice_with_bounded_minute_work():
    size = 12
    campaign = dense_campaign(size)
    shorter = _campaign([_drop("short", "Short", required_minutes=60)])
    shorter.id = "shorter"
    twitch = campaign._twitch
    shorter._twitch = twitch
    for drop in shorter.drops:
        drop._twitch = twitch
    # Isolate minute calculations from the separately reproduced ignore-policy cost.
    assert len(campaign.mineable_drop_ids) == size
    assert len(shorter.mineable_drop_ids) == 1
    visits = count_parents(campaign)
    channel = SimpleNamespace(game=campaign.game, online=True)
    twitch.wanted_games = [campaign.game]
    twitch.inventory = [campaign, shorter]
    twitch.watching_channel.get_with_default.return_value = channel

    assert InventoryService(twitch).get_active_campaign(channel) is shorter
    assert len(visits) <= 4 * size * size


@pytest.mark.parametrize("metric", ["total_required_minutes", "total_remaining_minutes"])
def test_cycle_tainted_subtotals_are_not_reused_in_another_path(metric):
    campaign = _campaign([
        _drop("a", "A", preconditions=("b", "exit")),
        _drop("b", "B", preconditions=("c",)),
        _drop("c", "C", preconditions=("a",)),
        _drop("exit", "Exit", required_minutes=100),
        _drop("root", "Root", preconditions=("a", "c")),
    ])
    # The first path cuts the cycle before the long exit available from the second.
    assert getattr(campaign.timed_drops["root"], metric) == 145


def test_claimed_cycles_keep_required_totals_but_stop_remaining_work():
    campaign = _campaign([
        _drop("a", "A", preconditions=("b",), claimed=True),
        _drop("b", "B", preconditions=("a",)),
    ])
    target = campaign.timed_drops["b"]
    assert target.total_required_minutes == 30
    assert target.total_remaining_minutes == 15
    campaign.timed_drops["a"].is_claimed = False
    campaign.timed_drops["a"].real_current_minutes = 0
    assert target.total_remaining_minutes == 30


def test_minute_and_claim_changes_recompute_on_each_property_read():
    campaign = _campaign([
        _drop("parent", "Parent"),
        _drop("target", "Target", preconditions=("parent",)),
    ])
    parent, target = campaign.timed_drops["parent"], campaign.timed_drops["target"]
    assert target.total_remaining_minutes == 30
    parent.real_current_minutes = 7
    parent.extra_current_minutes = 2
    assert target.total_remaining_minutes == 21
    parent.is_claimed = True
    assert target.total_remaining_minutes == 15
    assert target.total_required_minutes == 30
    parent.is_claimed = False
    assert target.total_remaining_minutes == 21


def test_negative_remaining_parent_is_not_clamped_by_the_optimization():
    campaign = _campaign([
        _drop("parent", "Parent"),
        _drop("target", "Target", preconditions=("parent",)),
    ])
    parent = campaign.timed_drops["parent"]
    parent.real_current_minutes = 14
    parent.extra_current_minutes = 3
    assert campaign.timed_drops["target"].total_remaining_minutes == 13


def test_missing_parent_and_explicit_visited_prefix_keep_original_totals():
    campaign = _campaign([
        _drop("parent", "Parent"),
        _drop("target", "Target", preconditions=("parent", "missing")),
    ])
    target = campaign.timed_drops["target"]
    assert target.total_required_minutes == 30
    assert target._total_chain_minutes(remaining=False, visiting=frozenset({"parent"})) == 15
    assert target.total_required_minutes == 30
