"""Shared classification of known persisted reads eligible for automatic retry."""

from __future__ import annotations

from src.config.operations import GQL_OPERATIONS


class GQLRetryPolicy:
    _READ_QUERIES = frozenset(
        (GQL_OPERATIONS[name]["operationName"],
         GQL_OPERATIONS[name]["extensions"]["persistedQuery"]["sha256Hash"])
        for name in (
            "GetStreamInfo", "ChannelPointsContext", "Inventory", "CurrentDrop",
            "Campaigns", "CampaignDetails", "AvailableDrops", "PlaybackAccessToken",
            "GameDirectory", "SlugRedirect", "NotificationsList",
        )
    )

    @classmethod
    def is_safe_read(cls, operation: object) -> bool:
        """Accept only the existing allowlist of exact persisted-read fingerprints."""
        try:
            if not isinstance(operation, dict) or not set(operation) <= {"operationName", "variables", "extensions"}:
                return False
            extensions = operation["extensions"]
            if not isinstance(extensions, dict) or set(extensions) != {"persistedQuery"}:
                return False
            query = extensions["persistedQuery"]
            if (not isinstance(query, dict) or set(query) != {"version", "sha256Hash"}
                    or type(query["version"]) is not int or query["version"] != 1):
                return False
            return (operation["operationName"], query["sha256Hash"]) in cls._READ_QUERIES
        except (KeyError, TypeError):
            return False

    @classmethod
    def can_retry(cls, operations: object) -> bool:
        if isinstance(operations, list):
            return bool(operations) and all(cls.is_safe_read(operation) for operation in operations)
        return cls.is_safe_read(operations)
