"""Public, content-free operational errors."""

from typing import Any

from . import i18n


class BridgeError(Exception):
    """An operational failure: a stable ``code``, a catalog ``key`` for its text, and safe ``details``.

    The text is rendered when it is read, in the language in use, with the details as parameters.
    ``key`` defaults to ``error.<code>``. A text in place of a key (what an older WSL helper sends)
    becomes the key of the catalog text it matches; one no catalog has is shown as it came, and
    ``key`` is then ``error.<code>``, so ``key`` is always a key.
    """

    def __init__(self, code: str, key: str | None = None, /, **details: Any):
        super().__init__(code)
        self.code = code
        self.details = details
        self._text = None
        if not key:
            key = None
        elif not i18n.is_key(key):
            found = i18n.recognise(key)
            if found is None:
                self._text = key
            key = found
        self.key = key or f"error.{code}"

    @property
    def message(self) -> str:
        if self._text is not None:
            return self._text
        return i18n.t(self.key, **_params(self.details))

    def __str__(self) -> str:
        return self.message

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details, "key": self.key}


# Names ``i18n.t`` takes for itself; a detail with one of them (from a helper, say) is not a parameter.
_RESERVED = {"lang", "count"}


def _params(details: dict) -> dict:
    # Only plain values reach a text; lists and records stay in the details.
    return {
        name: value
        for name, value in details.items()
        if name not in _RESERVED and isinstance(value, str | int | float)
    }
