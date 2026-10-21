"""Every text exists in every language, reads as it did, and none is written outside the catalogs."""

import ast
import json
import os
import re
import sys
from pathlib import Path

import pytest

from session_bridge import i18n
from session_bridge.errors import BridgeError

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "session_bridge"
GOLDEN = json.loads((ROOT / "tests/fixtures/i18n_legacy_es.json").read_text(encoding="utf-8"))
CATALOGS = {lang: i18n._catalog(lang) for lang in i18n.available()}


def _sources():
    return [p for p in sorted(SOURCE.rglob("*.py")) if "i18n" not in p.relative_to(SOURCE).parts]


def _error_calls():
    for path in _sources():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            func = getattr(node, "func", None)
            name = getattr(func, "id", getattr(func, "attr", None))
            if isinstance(node, ast.Call) and name == "BridgeError":
                yield path, node


@pytest.mark.parametrize("row", GOLDEN, ids=lambda row: row["key"])
def test_spanish_error_texts_read_exactly_as_before_they_moved_to_the_catalog(row):
    params = {"max_messages": 9362} if "max_messages" in row["details"] else {}
    assert i18n.t(row["key"], lang="es", **params) == row["text"]


def test_every_error_code_and_key_raised_in_the_code_has_a_text_in_every_language():
    missing = []
    for path, node in _error_calls():
        codes = [n.value for n in ast.walk(node.args[0]) if isinstance(n, ast.Constant)] if node.args else []
        keys = [f"error.{code}" for code in codes]
        if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
            keys.append(node.args[1].value)
        for key in keys:
            for lang, catalog in CATALOGS.items():
                if key not in catalog.MESSAGES:
                    missing.append(f"{path.name}:{node.lineno} {lang} {key}")
    assert not missing


def test_errors_carry_no_inline_text():
    inline = [
        f"{path.name}:{node.lineno}"
        for path, node in _error_calls()
        if len(node.args) > 1
        and isinstance(node.args[1], ast.Constant)
        and not i18n.is_key(node.args[1].value)
    ]
    assert not inline


def test_an_error_reads_in_the_language_in_use_when_it_is_shown(lang):
    error = BridgeError("native_limit", "error.native_limit.opencode_messages", max_messages=9362)
    assert "9362 mensajes" in error.message and str(error) == error.message
    lang("en")
    assert "9362 messages" in error.message
    assert error.as_dict()["key"] == "error.native_limit.opencode_messages"


def test_an_error_with_only_a_code_reads_as_that_code_summary():
    assert BridgeError("sync_worker_io").message == i18n.t("error.sync_worker_io")


def test_text_received_from_an_older_process_is_recognised_or_kept(lang):
    lang("en")
    assert i18n.text("Operación cancelada.") == "Operation cancelled."
    assert i18n.text("A wording no catalog has.") == "A wording no catalog has."


# The catalogs agree with each other -------------------------------------------------------------

FIELD = re.compile(r"\{([a-z_][a-z0-9_]*)(?::[a-z]+)?\}")


def _fields(message):
    texts = message.values() if isinstance(message, dict) else [message]
    return {field for text in texts for field in FIELD.findall(text)}


@pytest.mark.parametrize("lang", [lang for lang in i18n.available() if lang != i18n.DEFAULT])
def test_every_language_has_the_same_keys_and_parameters_as_english(lang):
    english, other = CATALOGS[i18n.DEFAULT].MESSAGES, CATALOGS[lang].MESSAGES
    assert sorted(set(english) ^ set(other)) == []
    differing = [key for key in english if _fields(english[key]) != _fields(other[key])]
    assert differing == []
    plural = [key for key in english if isinstance(english[key], dict) != isinstance(other[key], dict)]
    assert plural == []


@pytest.mark.parametrize("lang", i18n.available())
def test_plural_texts_have_every_form_their_language_uses(lang):
    catalog = CATALOGS[lang]
    forms = {catalog.plural(n) for n in range(0, 200)}
    incomplete = [
        key
        for key, message in catalog.MESSAGES.items()
        if isinstance(message, dict) and set(message) != forms
    ]
    assert incomplete == []


@pytest.mark.parametrize("lang", i18n.available())
def test_every_text_renders_with_its_own_parameters(lang):
    for key, message in CATALOGS[lang].MESSAGES.items():
        params = {field: 2 for field in _fields(message)}
        count = params.pop("count", None)
        i18n.t(key, count, lang=lang, **params)


@pytest.mark.parametrize("lang", i18n.available())
def test_a_catalog_names_its_language_and_its_punctuation(lang):
    catalog = CATALOGS[lang]
    assert catalog.NAME and len(catalog.QUOTES) == 2 and catalog.DECIMAL in ".,"


# The code uses the catalogs, and only the catalogs, for what people read -------------------------

# Keys the code builds from a code or a state at run time, as ``f"{prefix}{value}"``.
DYNAMIC = (
    "error.",
    "warning.",
    "field.",
    "time.",
    "link.state.",
    "link.reason.",
    "link.endpoint.",
    "transfer.state.",
    "worker.state.",
    "home.group.",
    "language.source.",
    "profile_prompt.",
)


def _trees():
    for path in sorted(SOURCE.rglob("*.py")):
        if path.parent.name == "i18n" and path.stem in i18n.available():
            continue
        source = path.read_text(encoding="utf-8")
        yield path, source.splitlines(), ast.parse(source)


def _constants():
    for path, _, tree in _trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                yield path, node


# File names look like keys (``sync.sqlite3``); they end in an extension no key ends in.
FILE_NAME = re.compile(r".*\.(json|sqlite3?|lock|md|sbridge|db|exe)")


def test_every_key_the_code_names_exists():
    spaces = {key.split(".", 1)[0] for key in CATALOGS[i18n.DEFAULT].MESSAGES}
    missing = [
        f"{path.name}:{node.lineno} {node.value}"
        for path, node in _constants()
        if i18n.is_key(node.value)
        and node.value.split(".", 1)[0] in spaces
        and not FILE_NAME.fullmatch(node.value)
        and not node.value.startswith("error.")  # codes are checked against the raised errors above
        and not i18n.has(node.value, i18n.DEFAULT)
    ]
    assert missing == []


def test_every_catalog_text_is_used_by_the_code():
    named = {node.value for _, node in _constants()}
    built = set()
    for path, _, tree in _trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr) and node.values and isinstance(node.values[0], ast.Constant):
                built.add(node.values[0].value)
    assert set(DYNAMIC) - built == set(), "a dynamic family no code builds any more"
    codes = {
        constant.value
        for _, node in _error_calls()
        if node.args
        for constant in ast.walk(node.args[0])
        if isinstance(constant, ast.Constant)
    } | {"invalid_language", "cancelled"}
    unused = []
    for key in CATALOGS[i18n.DEFAULT].MESSAGES:
        if key in named:
            continue
        if key.startswith("error."):
            if key.split(".")[1] in codes or key.split(".")[1] in named:
                continue
        elif key.startswith(DYNAMIC):
            continue
        unused.append(key)
    assert unused == []


# Calls whose text reaches a person, and the argument positions that carry it.
SINKS = {
    "print": None,
    "echo": None,
    "secho": None,
    "_note": 0,
    "_status": 0,
    "_ask": 0,
    "_ask_text": 0,
    "_ask_folder": 0,
    "prompt": 0,
    "confirm": 0,
    "Choice": None,
    "BadParameter": 0,
    "add_column": 0,
    "Panel": None,
    "Text": None,
}
TEXT_KEYWORDS = {"help", "epilog", "short_help", "placeholder", "empty", "noun", "label", "detail", "brief"}
MARKUP = re.compile(r"\[/?[a-z ]*\]")


def _shown(node):
    """The literal pieces of an expression that end up in the text, not those used to compute it."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node
    elif isinstance(node, ast.JoinedStr):
        for part in node.values:
            yield from _shown(part)
    elif isinstance(node, ast.BinOp):
        yield from _shown(node.left)
        yield from _shown(node.right)
    elif isinstance(node, ast.IfExp):
        yield from _shown(node.body)
        yield from _shown(node.orelse)
    elif isinstance(node, ast.BoolOp):
        for value in node.values:
            yield from _shown(value)


def _is_prose(value: str) -> bool:
    value = MARKUP.sub("", value)
    if i18n.is_key(value) or re.fullmatch(r"[a-z_]+(?:\.[a-z0-9_]+)*\.", value):
        return False  # a key, or the start of a dynamic key
    return bool(re.search(r"[^\W\d_]{2,}", value))


def test_no_text_people_read_is_written_outside_the_catalogs():
    found = []
    for path, lines, tree in _trees():
        if path.parent.name == "i18n":
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", getattr(node.func, "attr", None))
            carriers = [keyword.value for keyword in node.keywords if keyword.arg in TEXT_KEYWORDS]
            if name in SINKS:
                position = SINKS[name]
                carriers += node.args if position is None else node.args[position : position + 1]
            if name == "_rows":
                # Rows of (label, value): the label is text.
                carriers += [
                    row.elts[0]
                    for rows in node.args
                    if isinstance(rows, ast.List)
                    for row in rows.elts
                    if isinstance(row, ast.Tuple)
                ]
            for carrier in carriers:
                for constant in _shown(carrier):
                    marked = any(
                        "i18n: verbatim" in lines[line - 1]
                        for line in range(max(1, constant.lineno - 1), constant.end_lineno + 1)
                    )
                    if _is_prose(constant.value) and not marked:
                        found.append(f"{path.relative_to(SOURCE)}:{constant.lineno} {constant.value!r}")
    assert found == []


def test_the_scan_sees_text_written_inline():
    tree = ast.parse('_note(f"Hola {name}")\nprint("ok " + x)\n_note(i18n.t("menu.back"), "dim")')
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") in SINKS
    ]
    shown = [c.value for call in calls for arg in call.args[:1] for c in _shown(arg)]
    assert [value for value in shown if _is_prose(value)] == ["Hola ", "ok "]


# Which language a run uses -------------------------------------------------------------------------


@pytest.fixture
def no_preference(monkeypatch, tmp_path):
    """No option, variable, saved choice or system language: every source is off until a test sets it."""
    monkeypatch.setenv("SESSIONBRIDGE_HOME", str(tmp_path / "home"))
    for variable in ("SESSIONBRIDGE_LANG", "LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(i18n.sys, "platform", "linux")
    return monkeypatch


def test_the_option_wins_over_the_variable_the_setting_and_the_system(no_preference):
    from session_bridge.i18n import settings

    no_preference.setenv("LANG", "es_ES.UTF-8")
    assert i18n.resolve() == ("es", "system")
    settings.save_language("en")
    assert i18n.resolve() == ("en", "setting")
    no_preference.setenv("SESSIONBRIDGE_LANG", "es-MX")
    assert i18n.resolve() == ("es", "environment")
    assert i18n.resolve(["list", "--lang", "en"]) == ("en", "option")
    assert i18n.resolve(["--lang=EN_us"]) == ("en", "option")
    # After ``--`` nothing is an option any more.
    assert i18n.resolve(["show", "--", "--lang", "en"]) == ("es", "environment")


def test_without_any_preference_the_language_is_english(no_preference):
    assert i18n.resolve() == ("en", "default")


@pytest.mark.parametrize(
    ("variables", "expected"),
    [
        ({"LANG": "es_ES.UTF-8"}, "es"),
        ({"LANGUAGE": "fr:es", "LANG": "en_US.UTF-8"}, "es"),  # the first listed that has a catalog
        ({"LANGUAGE": "fr", "LANG": "es_ES.UTF-8"}, "es"),  # none listed has one: the locale decides
        ({"LANG": "fr_FR.UTF-8"}, None),
        ({"LC_ALL": "C", "LANG": "es_ES.UTF-8"}, None),  # C means no preference, and LC_ALL decides
        ({"LC_MESSAGES": "en_GB", "LANG": "es_ES"}, "en"),
        ({"LANG": "POSIX"}, None),
    ],
)
def test_posix_systems_are_read_as_posix_reads_them(no_preference, variables, expected):
    for variable, value in variables.items():
        no_preference.setenv(variable, value)
    assert i18n._system_language() == expected


@pytest.mark.parametrize(
    ("lcid", "expected"), [(0x0C0A, "es"), (0x080A, "es"), (0x0409, "en"), (0x040C, None)]
)
def test_windows_is_read_from_its_display_language(no_preference, lcid, expected):
    import ctypes
    from types import SimpleNamespace

    no_preference.setattr(i18n.sys, "platform", "win32")
    kernel = SimpleNamespace(GetUserDefaultUILanguage=lambda: lcid)
    no_preference.setattr(ctypes, "windll", SimpleNamespace(kernel32=kernel), raising=False)
    assert i18n._system_language() == expected


def test_an_unknown_language_is_an_error_that_lists_the_ones_there_are(no_preference):
    with pytest.raises(BridgeError) as error:
        i18n.resolve(["--lang", "fr"])
    assert error.value.code == "invalid_language"
    assert error.value.details["available"] == list(i18n.available())
    # From the variable or the saved file it is ignored instead: an old value never stops the program.
    no_preference.setenv("SESSIONBRIDGE_LANG", "fr")
    assert i18n.resolve() == ("en", "default")


def test_an_unreadable_settings_file_means_no_saved_choice(no_preference, tmp_path):
    from session_bridge.i18n import settings

    path = tmp_path / "home" / "settings.json"
    assert settings.language() is None and not path.parent.exists()  # reading creates nothing
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert settings.language() is None
    settings.save_language("es")
    assert json.loads(path.read_text(encoding="utf-8")) == {"language": "es"}
    settings.save_language(None)
    assert json.loads(path.read_text(encoding="utf-8")) == {}


def test_child_processes_inherit_the_language_without_this_process_changing(lang):
    lang("en")
    assert i18n.child_environment({"PATH": "x"}) == {"PATH": "x", "SESSIONBRIDGE_LANG": "en"}
    assert os.environ["SESSIONBRIDGE_LANG"] == "es"


def test_numbers_sizes_quotes_and_lists_follow_the_language(lang):
    assert (i18n.number(1.5), i18n.size(1536), i18n.quote("x")) == ("1,5", "1,5 KiB", "«x»")
    assert i18n.join(["a", "b", "c"]) == "a, b y c"
    lang("en")
    assert (i18n.number(1.5), i18n.size(128 * 1024 * 1024), i18n.quote("x")) == ("1.5", "128 MiB", "“x”")
    assert i18n.join(["a", "b"]) == "a and b" and i18n.join(["a"]) == "a"


# The ``language`` command --------------------------------------------------------------------------


def _main(monkeypatch, capsys, *argv):
    from session_bridge import cli

    monkeypatch.setattr(sys, "argv", ["sessionbridge", *argv])
    try:
        cli.main()
    except SystemExit as exc:
        code = exc.code or 0
    else:
        code = 0
    out = capsys.readouterr()
    return code, " ".join(out.out.split()), out.err


def test_language_shows_the_current_one_and_where_it_comes_from(no_preference, capsys):
    no_preference.setenv("LANG", "es_ES.UTF-8")
    code, out, _ = _main(no_preference, capsys, "language")
    assert code == 0
    assert "Idioma: Español (es)" in out and "idioma del sistema" in out and "en (English)" in out


def test_language_saves_the_choice_and_auto_forgets_it(no_preference, capsys, tmp_path):
    no_preference.setenv("LANG", "es_ES.UTF-8")
    code, out, _ = _main(no_preference, capsys, "language", "en")
    assert code == 0 and "Language: English (en)" in out  # this run already follows the new choice
    assert json.loads((tmp_path / "home" / "settings.json").read_text(encoding="utf-8")) == {"language": "en"}
    envelope = json.loads(_main(no_preference, capsys, "--json", "language")[1])
    assert envelope["data"] == {"language": "en", "source": "setting", "available": ["en", "es"]}
    code, out, _ = _main(no_preference, capsys, "language", "auto")
    assert code == 0 and "Idioma: Español (es)" in out


def test_language_with_an_unknown_value_fails_in_the_language_in_use(no_preference, capsys):
    code, out, err = _main(no_preference, capsys, "--lang", "en", "language", "fr")
    assert code == 2
    assert "invalid_language" in out + err and "fr" in out + err and "Idioma" not in out + err


# Nothing reaches the screen without going through a catalog ----------------------------------------


def _commands():
    import typer

    from session_bridge.cli import app

    root = typer.main.get_command(app)

    def walk(command, path):
        yield path, command
        for name, sub in sorted(getattr(command, "commands", {}).items()):
            yield from walk(sub, [*path, name])

    yield from walk(root, [])


def _names(command) -> set[str]:
    """Words a help page shows that are names, not text: commands, options, metavars and choices."""
    words = set(re.findall(r"[^\W\d_]+", " ".join(getattr(command, "commands", {}) or ())))
    for param in command.params:
        for value in (*param.opts, *param.secondary_opts, param.name or "", param.metavar or ""):
            words.update(re.findall(r"[^\W\d_]+", value))
        words.update(getattr(param.type, "choices", ()) or ())
    words.update(re.findall(r"[^\W\d_]+", " ".join(i18n.available())))
    return words


@pytest.mark.parametrize("path", [path for path, _ in _commands()], ids=lambda path: " ".join(path) or "root")
def test_every_help_page_comes_from_the_catalog(pseudo, path):
    from typer.testing import CliRunner

    from session_bridge.cli import app

    command = dict((tuple(p), c) for p, c in _commands())[tuple(path)]
    result = CliRunner().invoke(app, [*path, "--help"], env={"COLUMNS": "200"}, prog_name="sessionbridge")
    assert result.exit_code == 0, result.output
    allowed = (
        _names(command)
        | set(re.findall(r"[^\W\d_]+", " ".join(path)))
        | {"sessionbridge", "help", "str", "int", "float", "path", "file", "bool"}
    )  # type metavars
    stray = set(re.findall(r"[^\W\d_]{2,}", pseudo(result.output))) - allowed
    assert stray == set(), result.output


@pytest.mark.parametrize(
    ("lang", "own", "foreign"),
    [
        ("es", ["Uso:", "Opciones", "Muestra esta ayuda"], ["Usage:", "Options", "Show this message"]),
        ("en", ["Usage:", "Options", "Show this message"], ["Uso:", "Opciones", "Muestra", "conversación"]),
    ],
)
def test_the_installed_entry_point_writes_its_help_in_one_language(tmp_path, lang, own, foreign):
    import subprocess

    env = {
        **os.environ,
        "SESSIONBRIDGE_HOME": str(tmp_path),
        "PYTHONPATH": str(ROOT / "src"),
        "COLUMNS": "120",
    }
    for args in (["--help"], ["convert", "--help"], ["sync", "--help"]):
        result = subprocess.run(
            [sys.executable, "-c", "from session_bridge.cli import main; main()", "--lang", lang, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert all(text in result.stdout for text in own[:2]), result.stdout
        assert not [text for text in foreign if text in result.stdout], result.stdout


# What the first review found ----------------------------------------------------------------------


def test_shell_completion_shows_the_help_text_not_its_key(tmp_path):
    import subprocess

    env = {
        **os.environ,
        "SESSIONBRIDGE_HOME": str(tmp_path),
        "PYTHONPATH": str(ROOT / "src"),
        "SESSIONBRIDGE_LANG": "en",
        "_SESSIONBRIDGE_COMPLETE": "complete_zsh",
        "_TYPER_COMPLETE_ARGS": "sessionbridge list --",
    }
    script = "import sys; sys.argv = ['sessionbridge']; from session_bridge.cli import main; main()"
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, encoding="utf-8", env=env, check=False
    )
    assert '"--profile":"Only one profile, for example codex-win."' in result.stdout, result.stdout
    assert "help." not in result.stdout


def test_an_error_key_is_always_a_key_even_when_a_text_came_instead():
    recognised = BridgeError("native_unsupported", i18n.t("capability.unverified_version", lang="es"))
    assert recognised.key == "capability.unverified_version"
    unknown = BridgeError("wsl_helper_error", "Ein Text, den kein Katalog kennt.")
    assert unknown.key == "error.wsl_helper_error" and unknown.message == "Ein Text, den kein Katalog kennt."
    assert unknown.as_dict()["key"] == "error.wsl_helper_error"
    assert BridgeError("cancelled", "").message == i18n.t("error.cancelled")


def test_a_detail_named_like_a_rendering_option_does_not_break_the_text():
    error = BridgeError("cancelled", lang="zz", count=3)
    assert error.message == i18n.t("error.cancelled") and error.details["lang"] == "zz"


def test_language_choices_in_help_come_from_the_catalogs(lang):
    from session_bridge.i18n import typer_chrome

    lang("en")
    assert typer_chrome._text("help.language.choice").startswith("en, es or auto;")
    lang("es")
    assert typer_chrome._text("help.language.choice").startswith("en, es o auto;")


def test_an_older_record_with_a_warning_sentence_reads_in_the_language_in_use(lang):
    from session_bridge import ui

    lang("en")
    old = "Hay elementos conservados únicamente en el archivo portable."
    assert ui.warning_text(old) == i18n.t("warning.archive_only_present")
    assert ui.warning_text("source_truncated:3") == i18n.t("warning.source_truncated", detail="3")
    assert (ui.value_text(True), ui.value_text(False), ui.value_text(None)) == ("yes", "no", "—")


@pytest.mark.parametrize("row", GOLDEN, ids=lambda row: row["key"])
def test_every_text_an_older_version_sent_is_recognised_with_the_details_it_sent(row):
    # An older helper sent this exact Spanish text, with the details of its error.
    params = {"max_messages": 9362, "message_count": 10000} if "max_messages" in row["details"] else {}
    found = i18n.recognise(row["text"], params)
    assert found is not None
    assert i18n.t(found, lang="es", **params) == row["text"]


def test_a_text_with_values_is_recognised_only_when_the_details_rebuild_it_exactly(lang):
    sent = i18n.t("error.native_limit.opencode_messages", lang="es", max_messages=9362)
    lang("en")
    error = BridgeError("native_limit", sent, max_messages=9362, message_count=10000)
    assert error.key == "error.native_limit.opencode_messages"
    assert error.message == i18n.t("error.native_limit.opencode_messages", max_messages=9362)
    # Details that do not rebuild the text (another value, or none) leave it as it came.
    for details in ({"max_messages": 500}, {}):
        kept = BridgeError("native_limit", sent, **details)
        assert kept.message == sent and kept.key == "error.native_limit"


def test_an_older_wording_with_values_kept_in_a_catalog_is_recognised(monkeypatch):
    spanish = i18n._catalog("es")
    monkeypatch.setattr(
        spanish,
        "LEGACY",
        {"error.native_limit.opencode_messages": "Antes: {max_messages} mensajes."},
        raising=False,
    )
    i18n.register("es", spanish)  # clears the indexes built from the catalogs
    try:
        assert (
            i18n.recognise("Antes: 9362 mensajes.", {"max_messages": 9362})
            == "error.native_limit.opencode_messages"
        )
    finally:
        monkeypatch.undo()
        i18n.register("es", spanish)
