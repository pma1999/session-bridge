"""Session Bridge: independent local session exchange."""

import os as _os
import sys as _sys

__version__ = "0.7.1"


def _release_launcher() -> None:
    """Take the Windows launcher's own .exe off the import path once it has started Python.

    pip's ``sessionbridge.exe`` runs Python with itself on ``sys.path``, as a zip. Anything that
    scans the path for installed packages, such as pydantic looking for plugins, then keeps that
    .exe open, and Windows cannot rename it while it runs, so an update could not replace it.
    """
    for entry in list(_sys.path):
        if isinstance(entry, str) and entry.lower().endswith(".exe") and _os.path.isfile(entry):
            _sys.path.remove(entry)
            _sys.path_importer_cache.pop(entry, None)


_release_launcher()
