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


def work_path(*relative):
    """Absolute path of a file under the work directory, or None if it is absent."""
    base = work_dir()
    if base is None:
        return None
    path = os.path.join(base, *relative)
    return path if os.path.exists(path) else None


def requires_file(*relative):
    """Skip a test unless the given file under the work directory exists."""
    rel = "/".join(relative)
    return unittest.skipUnless(
        work_path(*relative) is not None,
        "game data file not found: " + rel + " (set CARNEVIL_WORK or mount the share)",
    )
