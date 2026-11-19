"""Locate the (never committed) game data for tests that need it."""
import os
import unittest

DEFAULT_WORK = "/Volumes/archive/reincarnevilation/work"


def work_dir():
    """Return the game-data work directory, or None if it is not present."""
    path = os.environ.get("CARNEVIL_WORK", DEFAULT_WORK)
    return path if os.path.isdir(path) else None


requires_gamedata = unittest.skipUnless(
    work_dir() is not None,
    "game data not found (set CARNEVIL_WORK or mount the share)",
)
