"""Identifiers for the resources the server stores."""

import secrets

__all__ = ["new_id"]


def new_id(prefix: str) -> str:
    """Return a new random identifier, like `"ses_Zk3uY0mWq1x8Jr5cTt2aLw"`.

    The part after the underscore is 128 random bits, so a caller can't guess the identifier
    of a resource it wasn't given.

    Args:
        prefix: The kind of resource: `"chr"`, `"adv"`, `"ses"`, `"sav"`, or `"rol"`.

    Returns:
        The prefix, an underscore, and 22 URL-safe characters.

    Examples:
        ```python
        from bxapi.ids import new_id

        session_id = new_id("ses")
        assert session_id.startswith("ses_")
        assert len(session_id) == 26
        ```
    """
    return f"{prefix}_{secrets.token_urlsafe(16)}"
