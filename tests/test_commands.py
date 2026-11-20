from osrlib.crawl.commands import ALL_COMMAND_CLASSES

from bxapi.commands import PLAY_COMMAND_TYPES, CommandKind, command_kind


def _documented_as_referee(command_class: type) -> bool:
    # osrlib starts the summary of every referee command's docstring with "Referee:".
    return (command_class.__doc__ or "").lstrip().startswith("Referee:")


def test_play_commands_match_osrlib_documentation() -> None:
    documented_play = {
        command_class.model_fields["command_type"].default
        for command_class in ALL_COMMAND_CLASSES
        if not _documented_as_referee(command_class)
    }
    assert PLAY_COMMAND_TYPES == documented_play


def test_osrlib_still_marks_referee_commands() -> None:
    referee = [command_class for command_class in ALL_COMMAND_CLASSES if _documented_as_referee(command_class)]
    assert len(referee) >= 23


def test_unknown_commands_count_as_referee() -> None:
    assert command_kind("move_party") is CommandKind.PLAY
    assert command_kind("grant_item") is CommandKind.REFEREE
    assert command_kind("a_command_from_the_future") is CommandKind.REFEREE
