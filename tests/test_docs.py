"""The documentation stays true to the program: links resolve, commands exist, generated pages are current."""

import importlib.util
import re
import shlex
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _tracked_markdown() -> list[Path]:
    try:
        listed = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.md"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        # A release ZIP has no Git metadata; its documents are the ones it ships.
        listed = [str(path.relative_to(ROOT)) for path in ROOT.glob("*.md")]
        listed += [str(path.relative_to(ROOT)) for path in (ROOT / "docs").glob("*.md")]
    return sorted(ROOT / name for name in listed if (ROOT / name).is_file())


DOCUMENTS = _tracked_markdown()
FENCE = re.compile(r"^(```|~~~).*?^\1", re.MULTILINE | re.DOTALL)
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)|!\[[^\]]*\]\(([^)\s]+)\)")
HTML_SOURCE = re.compile(r"""\b(?:src|srcset|href)=["']([^"']+)["']""")


def _builder():
    spec = importlib.util.spec_from_file_location("build_docs", ROOT / "scripts" / "build_docs.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _prose(text: str) -> str:
    return FENCE.sub("", text)


def _slug(heading: str) -> str:
    """GitHub's anchor for a heading: lower case, punctuation dropped, spaces as hyphens."""
    text = re.sub(r"`|\*\*|__", "", heading.strip()).lower()
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    seen: dict[str, int] = {}
    anchors = set()
    for line in _prose(path.read_text(encoding="utf-8")).splitlines():
        match = re.match(r"^#{1,6}\s+(.*?)\s*#*$", line)
        if not match:
            continue
        slug = _slug(match.group(1))
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        anchors.add(slug if count == 0 else f"{slug}-{count}")
    anchors |= set(re.findall(r"""<a\s+(?:name|id)=["']([^"']+)["']""", path.read_text(encoding="utf-8")))
    return anchors


def _targets(path: Path) -> list[str]:
    text = _prose(path.read_text(encoding="utf-8"))
    found = [first or second for first, second in LINK.findall(text)]
    found += HTML_SOURCE.findall(text)
    return [target for target in found if not re.match(r"^[a-z][a-z0-9+.-]*:", target)]


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda path: str(path.relative_to(ROOT)))
def test_every_relative_link_and_anchor_resolves(document):
    broken = []
    for target in _targets(document):
        location, _, anchor = target.partition("#")
        file = (document.parent / location).resolve() if location else document
        if not file.exists():
            broken.append(target)
        elif anchor and file.suffix == ".md" and anchor not in _anchors(file):
            broken.append(target)
    assert broken == []


def _command_lines(text: str):
    """``sessionbridge …`` as shown in code blocks and inline code."""
    for block in FENCE.finditer(text):
        for line in block.group(0).splitlines():
            line = line.strip()
            if line.startswith("sessionbridge"):
                yield line.split("  #")[0].split(" # ")[0]
    yield from re.findall(r"`(sessionbridge(?: [^`]*)?)`", _prose(text))


def _options(command) -> dict:
    return {name: param for param in command.params for name in [*param.opts, *param.secondary_opts]}


def _resolve(tokens, root):
    """Follow a documented line through the command tree: its command, or why it does not exist."""
    from typer._click import Context

    command, context = root, Context(root, info_name="sessionbridge")
    tokens, problems = list(tokens[1:]), []
    while tokens:
        token = tokens.pop(0)
        if token.startswith("-"):
            name = token.split("=")[0]
            param = _options(command).get(name) or _options(root).get(name)
            if param is None and name != "--help":
                problems.append(f"unknown option {name}")
            elif param is not None and not param.is_flag and "=" not in token and tokens:
                tokens.pop(0)  # its value
            continue
        if not hasattr(command, "list_commands"):
            continue  # an argument value
        child = command.get_command(context, token)
        if child is not None:
            command, context = child, Context(child, info_name=token, parent=context)
        elif not token.isupper():  # COMMAND and the like are placeholders
            problems.append(f"no command {token!r}")
            break
    return problems


def test_documented_commands_and_options_exist():
    import typer

    from session_bridge import cli

    root = typer.main.get_command(cli.app)
    problems = []
    for document in DOCUMENTS:
        if document.name == "CHANGELOG.md":
            continue  # History may name options that no longer exist.
        for line in _command_lines(document.read_text(encoding="utf-8")):
            try:
                tokens = shlex.split(line, posix=False)
            except ValueError:
                problems.append(f"{document.name}: cannot parse {line!r}")
                continue
            problems += [f"{document.name}: {line!r}: {problem}" for problem in _resolve(tokens, root)]
    assert problems == []


def test_command_reference_is_current():
    reference = (ROOT / "docs" / "cli-reference.md").read_text(encoding="utf-8")
    assert reference == _builder().cli_reference(), "run: python scripts/build_docs.py"


def test_screenshots_are_drawn_by_the_real_screens(tmp_path):
    from session_bridge import i18n

    written = _builder().screenshots(tmp_path)
    assert sorted(path.name for path in written) == sorted(path.name for path in (ROOT / "docs/images").glob("*.svg"))
    home = (tmp_path / "home-en-dark.svg").read_text(encoding="utf-8")
    assert home.startswith("<svg") and "Checkout" in home and "Codex" in home
    assert "Actualizar" in (tmp_path / "update-es-light.svg").read_text(encoding="utf-8")
    for path in written:
        text = path.read_text(encoding="utf-8")
        assert str(Path.home()) not in text and "Users" not in text
    # The language in use is left as it was.
    assert i18n.current() == "es"


def _outline(path: Path):
    text = _prose(path.read_text(encoding="utf-8"))
    sections = re.findall(r"^## ", text, re.MULTILINE)
    links = [target.replace("-es-", "-en-").replace("README.es.md", "README.md") for target in _targets(path)]
    # Links within the page differ by language, because headings do.
    return len(sections), sorted(link for link in links if link != "README.md" and not link.startswith("#"))


def test_the_spanish_readme_follows_the_english_one():
    english, spanish = ROOT / "README.md", ROOT / "README.es.md"
    assert _outline(spanish) == _outline(english)
