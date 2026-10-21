"""Typer's own help texts in the language in use, rendered each time help is shown.

Session Bridge declares help as catalog keys (``help="help.cmd.list"``). Typer and Click add
texts of their own: panel titles, ``Usage:``, ``[default: …]``, the ``--help`` and completion
options, the usage metavars, and the help shell completion shows next to each choice. This is
the one place that knows where they live in the installed Typer (0.27, with its bundled Click),
so the CLI needs no other change to be localized.
Installing it is language-neutral: it only reads the language when help is rendered.
"""

from __future__ import annotations

import re

from . import available, is_key, join, t

_INSTALLED = False
# Texts Typer or Click write for options they add themselves, by parameter name.
_BUILTIN_OPTIONS = {
    "help": "cli.help.help_option",
    "install_completion": "cli.help.install_completion",
    "show_completion": "cli.help.show_completion",
}


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    from typer import rich_utils
    from typer._click import formatting, shell_completion

    original_help = rich_utils.rich_format_help
    original_usage = formatting.HelpFormatter.write_usage
    usage_patterns = list(rich_utils.OptionHighlighter.highlights)

    def rich_format_help(*, obj, ctx, markup_mode):
        _localize_constants(rich_utils, usage_patterns)
        _localize_command(obj, ctx)
        return original_help(obj=obj, ctx=ctx, markup_mode=markup_mode)

    def write_usage(self, prog, args="", prefix=None):
        return original_usage(self, prog, args, t("cli.help.usage") if prefix is None else prefix)

    original_item = shell_completion.CompletionItem.__init__

    def completion_item(self, value, type="plain", help=None, **kwargs):
        original_item(self, value, type, _text(help) if is_key(help) else help, **kwargs)

    rich_utils.rich_format_help = rich_format_help
    formatting.HelpFormatter.write_usage = write_usage
    shell_completion.CompletionItem.__init__ = completion_item
    _INSTALLED = True


def _localize_constants(rich_utils, usage_patterns) -> None:
    rich_utils.ARGUMENTS_PANEL_TITLE = t("cli.help.arguments")
    rich_utils.OPTIONS_PANEL_TITLE = t("cli.help.options")
    rich_utils.COMMANDS_PANEL_TITLE = t("cli.help.commands")
    # Typer fills these with str.format, so the value stays a positional field.
    rich_utils.DEFAULT_STRING = t("cli.help.default", value="{}")
    rich_utils.ENVVAR_STRING = t("cli.help.envvar", value="{}")
    rich_utils.REQUIRED_LONG_STRING = t("cli.help.required")
    rich_utils.DEPRECATED_STRING = t("cli.help.deprecated") + " "
    prefix = re.escape(t("cli.help.usage"))
    rich_utils.OptionHighlighter.highlights = [*usage_patterns, rf"(?P<usage>{prefix})"]


def _text(key: str) -> str:
    # Help texts may name the languages there are, so a new catalog needs no edit to the others.
    return t(key, languages=join([*available(), "auto"], choice=True))


def _render(item, attribute: str) -> None:
    """Replace a catalog key with its text, remembering the key so the next render can redo it."""
    saved = f"_i18n_{attribute}"
    key = getattr(item, saved, None)
    if key is None:
        value = getattr(item, attribute, None)
        if not is_key(value):
            return
        key = value
        setattr(item, saved, key)
    setattr(item, attribute, _text(key))


def _localize_command(command, ctx) -> None:
    for attribute in ("help", "short_help", "epilog"):
        _render(command, attribute)
    if getattr(command, "options_metavar", None):
        command.options_metavar = t("cli.help.options_metavar")
    if hasattr(command, "subcommand_metavar"):
        command.subcommand_metavar = t("cli.help.subcommand_metavar")
    for param in command.get_params(ctx):
        builtin = _BUILTIN_OPTIONS.get(param.name or "")
        if builtin and not is_key(getattr(param, "_i18n_help", None)):
            param._i18n_help = builtin
        _render(param, "help")
    for sub in (getattr(command, "commands", None) or {}).values():
        # A group lists its commands with their short help; their own help renders when shown.
        for attribute in ("help", "short_help"):
            _render(sub, attribute)
