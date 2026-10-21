"""The language every human-readable text is shown in, and the catalogs that hold that text.

Code refers to text by key (``t("menu.convert")``); each language is one module in this package
with a ``MESSAGES`` dict, a ``plural`` rule and its native ``NAME``. Adding a language is adding
one such module to ``_CATALOGS``; nothing else names a language.

What is stored or exchanged stays language-neutral: error codes, warning codes and keys. Text is
rendered when it is shown, so it always follows the language in use, even for an error raised
before the language was known or by another process.
"""

from __future__ import annotations

import os
import re
import string
import sys
from importlib import import_module
from types import ModuleType
from typing import Any

DEFAULT = "en"
_CATALOGS = ("en", "es")
_KEY = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+")
_ENVIRONMENT = "SESSIONBRIDGE_LANG"

_loaded: dict[str, ModuleType] = {}
_state: dict[str, Any] = {"lang": None, "source": None, "strict": False}
_reverse: dict[str, str] | None = None


class MissingText(LookupError):
    """A key or a parameter the catalogs do not have; raised only in strict mode (tests)."""


def _catalog(lang: str) -> ModuleType:
    if lang not in _loaded:
        _loaded[lang] = import_module(f"{__name__}.{lang}")
    return _loaded[lang]


def register(lang: str, module: ModuleType) -> None:
    """Make a catalog module available under ``lang`` (tests use it for a pseudo-language)."""
    global _reverse
    _loaded[lang] = module
    _reverse = None


def available() -> tuple[str, ...]:
    return _CATALOGS


def name(lang: str) -> str:
    return _catalog(lang).NAME


def normalize(value: str | None) -> str | None:
    """``es_ES.UTF-8``, ``es-MX`` or ``ES`` → ``es``; None for anything without a catalog."""
    if not value:
        return None
    base = re.split(r"[._@-]", value.strip(), maxsplit=1)[0].casefold()
    return base if base in _CATALOGS or base in _loaded else None


def _argv_language(argv) -> str | None:
    argv = list(argv)
    for index, item in enumerate(argv):
        if item == "--":
            break
        if item == "--lang" and index + 1 < len(argv):
            return argv[index + 1]
        if item.startswith("--lang="):
            return item.split("=", 1)[1]
    return None


def _system_language() -> str | None:
    if sys.platform == "win32":
        try:
            import ctypes
            import locale

            lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            found = normalize(locale.windows_locale.get(lcid))
            if found:
                return found
        except (AttributeError, OSError, ValueError):
            pass
    # LANGUAGE is a list of preferences (``fr:es``): the first one with a catalog wins, and if none
    # has one the locale decides. Of the locale variables the first one set decides, as in POSIX;
    # C or POSIX there means no preference.
    for part in os.environ.get("LANGUAGE", "").split(":"):
        found = normalize(part)
        if found:
            return found
    for variable in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(variable, "")
        if value:
            return None if value.split(".")[0] in ("C", "POSIX") else normalize(value)
    return None


def resolve(argv=()) -> tuple[str, str]:
    """The language to use and where it came from: option, environment, setting, system or default."""
    from . import settings

    requested = _argv_language(argv)
    if requested is not None:
        return validated(requested), "option"
    if os.environ.get(_ENVIRONMENT):
        found = normalize(os.environ[_ENVIRONMENT])
        if found:
            return found, "environment"
    saved = normalize(settings.language())
    if saved:
        return saved, "setting"
    found = _system_language()
    if found:
        return found, "system"
    return DEFAULT, "default"


def validated(value: str) -> str:
    """The catalog code for ``value``; an unknown language is an error that lists the ones there are."""
    found = normalize(value)
    if found is None:
        from ..errors import BridgeError

        raise BridgeError("invalid_language", requested=value, available=list(_CATALOGS))
    return found


def bootstrap(argv=()) -> str:
    """Choose the language for this process; an explicit ``--lang`` that is unknown is an error."""
    lang, source = resolve(argv)
    use(lang, source=source)
    return lang


def use(lang: str, *, source: str = "option") -> None:
    _state["lang"] = validated(lang)
    _state["source"] = source


def child_environment(environment: dict[str, str]) -> dict[str, str]:
    """``environment`` for a child process (the sync worker, the WSL helper) that writes in our language."""
    return {**environment, _ENVIRONMENT: current()}


def current() -> str:
    if _state["lang"] is None:
        try:
            bootstrap()
        except Exception:  # noqa: BLE001 - an unreadable preference never stops the program
            _state["lang"], _state["source"] = DEFAULT, "default"
    return _state["lang"]


def source() -> str:
    current()
    return _state["source"]


def strict(enabled: bool = True) -> None:
    _state["strict"] = enabled


def is_key(value: object) -> bool:
    return isinstance(value, str) and bool(_KEY.fullmatch(value))


def _lookup(key: str, lang: str):
    for candidate in (lang, DEFAULT):
        found = _catalog(candidate).MESSAGES.get(key)
        if found is not None:
            return found, candidate
    return None, lang


def has(key: str, lang: str | None = None) -> bool:
    return _catalog(lang or current()).MESSAGES.get(key) is not None


class _Formatter(string.Formatter):
    """Named fields only, with a few typed formats: ``{n:number}``, ``{size:bytes}``, ``{x:quote}``."""

    def __init__(self, lang):
        self.lang = lang

    def get_field(self, field_name, args, kwargs):
        if not field_name.isidentifier():
            raise MissingText(f"field {field_name!r} is not a plain name")
        return self.get_value(field_name, args, kwargs), field_name

    def get_value(self, key, args, kwargs):
        if key not in kwargs:
            raise MissingText(f"parameter {key!r}")
        return kwargs[key]

    def format_field(self, value, format_spec):
        if format_spec == "number":
            return number(value, lang=self.lang)
        if format_spec == "bytes":
            return size(value, lang=self.lang)
        if format_spec == "quote":
            return quote(str(value), lang=self.lang)
        if format_spec:
            raise MissingText(f"format {format_spec!r}")
        return str(value)


def t(key: str, /, count: int | None = None, *, lang: str | None = None, **params) -> str:
    """The text for ``key`` in the language in use, with ``params`` filled in.

    A message with plural forms is a dict chosen by the language's rule for ``count``; ``count``
    is also available to the text as ``{count}``.
    """
    lang = lang or current()
    message, found_in = _lookup(key, lang)
    if message is None:
        if _state["strict"]:
            raise MissingText(f"key {key!r}")
        return key
    if count is not None:
        params["count"] = count
    if isinstance(message, dict):
        category = _catalog(found_in).plural(count if count is not None else 0)
        message = message.get(category) or message.get("other", "")
    try:
        return _Formatter(found_in).vformat(message, (), params)
    except (MissingText, ValueError, IndexError) as exc:
        if _state["strict"]:
            raise MissingText(f"{key}: {exc}") from None
        return message


def _reverse_index() -> dict[str, str]:
    """Every catalog text without parameters → its key; it recognises text another process rendered."""
    global _reverse
    if _reverse is None:
        _reverse = {}
        for lang in (*_CATALOGS, *(lang for lang in _loaded if lang not in _CATALOGS)):
            catalog = _catalog(lang)
            entries = list(catalog.MESSAGES.items()) + list(getattr(catalog, "LEGACY", {}).items())
            for key, message in entries:
                if isinstance(message, str) and "{" not in message:
                    _reverse.setdefault(message, key)
    return _reverse


def recognise(message: str) -> str | None:
    """The key of a text some catalog (or an older wording) holds, if it has no parameters."""
    return _reverse_index().get(message)


def text(value: str | None, /, **params) -> str:
    """Render a key, or keep a text that is no key (older records, a newer helper's wording)."""
    if not value:
        return ""
    if is_key(value):
        return t(value, **params)
    key = recognise(value)
    return t(key, **params) if key else value


def quote(value: str, *, lang: str | None = None) -> str:
    lang = lang or current()
    open_mark, close_mark = _catalog(lang).QUOTES
    return f"{open_mark}{value}{close_mark}"


def number(value, *, lang: str | None = None) -> str:
    lang = lang or current()
    if isinstance(value, bool) or not isinstance(value, int | float):
        return str(value)
    if isinstance(value, float) and not value.is_integer():
        rendered = f"{value:g}"
    else:
        rendered = str(int(value))
    return rendered.replace(".", _catalog(lang).DECIMAL)


def size(value, *, lang: str | None = None) -> str:
    """Bytes in binary units, as the limits are defined (``128 MiB``)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return str(value)
    amount, unit = float(value), "B"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            break
        amount /= 1024
    shown = round(amount, 1)
    return f"{number(int(shown) if shown.is_integer() else shown, lang=lang)} {unit}"


def join(items, *, lang: str | None = None, choice: bool = False) -> str:
    """``a, b and c`` in the language in use; ``a, b or c`` when they are alternatives (``choice``)."""
    items = [str(item) for item in items]
    if len(items) < 2:
        return "".join(items)
    key = "fmt.list_choice" if choice else "fmt.list"
    return t(key, lang=lang, head=", ".join(items[:-1]), last=items[-1])
