"""Every test sees Spanish unless it asks for another language, and a missing text fails loudly."""

import os

import pytest

# Set before any test module imports the CLI, so nothing depends on the machine's language.
os.environ["SESSIONBRIDGE_LANG"] = "es"

from session_bridge import i18n  # noqa: E402


@pytest.fixture(autouse=True)
def spanish_and_strict(monkeypatch):
    monkeypatch.setenv("SESSIONBRIDGE_LANG", "es")
    i18n.use("es", source="environment")
    i18n.strict(True)
    yield
    i18n.strict(False)
    i18n.use("es", source="environment")


@pytest.fixture
def lang():
    """Switch the language inside one test: ``lang("en")``."""

    def switch(value):
        i18n.use(value, source="option")

    return switch


PSEUDO = "xx"


def _pseudo_catalog():
    """English with every text wrapped in ⟦ ⟧, so any text shown outside the markers is not localized."""
    import types

    english = i18n._catalog("en")

    def wrap(message):
        if isinstance(message, dict):
            return {category: f"⟦{text}⟧" for category, text in message.items()}
        return f"⟦{message}⟧"

    module = types.ModuleType(PSEUDO)
    module.NAME, module.QUOTES, module.DECIMAL = "⟦Pseudo⟧", ("‹", "›"), ","
    module.plural = english.plural
    module.MESSAGES = {key: wrap(message) for key, message in english.MESSAGES.items()}
    return module


@pytest.fixture
def pseudo():
    """Show everything in the pseudo-language; ``pseudo(text)`` returns what lies outside the markers."""
    import re

    i18n.register(PSEUDO, _pseudo_catalog())
    i18n.use(PSEUDO, source="option")

    def outside(text):
        # Texts nest (an error inside "Error [code]: …"); remove the innermost until none is left.
        previous = None
        while previous != text:
            previous, text = text, re.sub(r"⟦[^⟦⟧]*⟧", " ", text)
        # A text cut to fit a column ends in … before its closing marker.
        text = re.sub(r"⟦[^⟦⟧\n]*…", " ", text)
        # Identifiers and codes (ln_abc, rv_0c79fa39, sync_worker_io) are data, not text.
        return re.sub(r"[\w:.-]*[\d_][\w:.-]*", " ", text)

    yield outside
    i18n.unregister(PSEUDO)
