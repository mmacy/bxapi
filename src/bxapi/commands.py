"""Which osrlib commands a player may send, and which only a referee may.

osrlib has two kinds of command. A play command is an action the players choose, like moving
the party or attacking. A referee command is an action the referee takes, like granting an
item or spawning monsters. A player who could send referee commands could give their party
anything, so the command endpoint accepts a referee command only from a caller with the
referee [`Role`][bxapi.settings.Role].

osrlib marks the kind only in each command's documentation, so this module lists the play
commands by name. A test compares the list with osrlib's documentation, and fails when a new
osrlib release adds a command this module doesn't classify.
"""

from enum import StrEnum

__all__ = ["PLAY_COMMAND_TYPES", "CommandKind", "command_kind"]


class CommandKind(StrEnum):
    """Who may send a command."""

    PLAY = "play"
    """A player or a referee."""
    REFEREE = "referee"
    """Only a referee."""


PLAY_COMMAND_TYPES: frozenset[str] = frozenset(
    {
        "cast_spell",
        "close_door",
        "drop_items",
        "engage_battle",
        "enter_dungeon",
        "equip_item",
        "evade",
        "extinguish_source",
        "force_door",
        "give_items",
        "inspect_treasure",
        "learn_spell",
        "light_source",
        "listen_at_door",
        "move_party",
        "open_door",
        "parley",
        "pick_lock",
        "prepare_spells",
        "purchase_equipment",
        "purchase_healing",
        "remove_treasure_trap",
        "reorder_party",
        "resolve_battle_round",
        "rest",
        "search",
        "sell_treasure",
        "take_treasure",
        "travel_to_town",
        "turn_party",
        "turn_undead",
        "unequip_item",
        "use_item",
        "use_stairs",
        "wait",
        "wedge_door",
    }
)
"""The `command_type` of every play command."""


def command_kind(command_type: str) -> CommandKind:
    """Return whether a command is a play command or a referee command.

    A command type that isn't in
    [`PLAY_COMMAND_TYPES`][bxapi.commands.PLAY_COMMAND_TYPES] counts as a referee command, so
    a command this module has never heard of is refused to players.

    Args:
        command_type: The command's `command_type`, like `"move_party"`.

    Returns:
        The command's kind.

    Examples:
        ```python
        from bxapi.commands import CommandKind, command_kind

        assert command_kind("move_party") is CommandKind.PLAY
        assert command_kind("grant_item") is CommandKind.REFEREE
        ```
    """
    return CommandKind.PLAY if command_type in PLAY_COMMAND_TYPES else CommandKind.REFEREE
