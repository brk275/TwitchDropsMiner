"""The automatic switch comparison shared by runtime and conditional forecasts."""

from __future__ import annotations


class ChannelSwitchPolicy:
    @staticmethod
    def should_switch(priority: tuple[int, int], acl_based: bool, *,
                      current_priority: tuple[int, int] | None = None,
                      current_acl_based: bool = False) -> bool:
        """A missing priority means there is no eligible current channel.

        Candidate discovery order is separate. An eligible current stream survives
        an equal-priority candidate unless the latter promotes it to an ACL channel.
        """
        return (current_priority is None or priority < current_priority
                or priority == current_priority and acl_based > current_acl_based)
