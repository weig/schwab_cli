"""Helpers for the ``--doc`` flag — find the user-facing markdown page
for a command and print it alongside Click's auto-generated help.

Doc layout:

* **Dev mode** (`uv run schwab_cli …`) — pages live at the project root
  in ``doc/``. That's the source-of-truth location humans edit.
* **Installed tool** (`uv tool install …`) — pages are bundled inside
  the wheel under ``schwab_cli/doc/`` via hatch's ``force-include``
  (see ``pyproject.toml``). This lookup is the fallback.

The lookup tries dev-mode first so an edit to ``doc/vol.md`` in the
working copy is visible immediately to ``uv run schwab_cli vol --doc``
without needing a wheel rebuild.
"""

from __future__ import annotations

from pathlib import Path

import click
import typer

_PKG_ROOT = Path(__file__).parent
_REPO_DOC = _PKG_ROOT.parent.parent / "doc"   # <repo>/doc/
_BUNDLED_DOC = _PKG_ROOT / "doc"              # <wheel>/schwab_cli/doc/

_SEPARATOR = "=" * 72


def _find_doc(name: str) -> Path | None:
    """Return the path to ``doc/<name>.md`` from dev or bundled location."""
    for candidate in (_REPO_DOC / f"{name}.md", _BUNDLED_DOC / f"{name}.md"):
        if candidate.exists():
            return candidate
    return None


def _show_doc(ctx: click.Context, value: bool) -> None:
    """Print Click's help then append the matching doc page."""
    if not value or ctx.resilient_parsing:
        return
    click.echo(ctx.get_help())
    # info_name is "schwab_cli" at the app level; map to the index page.
    name = ctx.info_name or "index"
    if name == "schwab_cli":
        name = "index"
    doc = _find_doc(name)
    click.echo()
    click.echo(_SEPARATOR)
    click.echo()
    if doc is not None:
        click.echo(doc.read_text())
    else:
        click.secho(f"(no doc page for {name!r})", fg="yellow")
    ctx.exit()


def doc_option() -> typer.models.OptionInfo:
    """Return an ``--doc`` typer option suitable as a parameter default.

    Usage in a command signature::

        doc: bool = doc_option(),
    """
    return typer.Option(
        False,
        "--doc",
        help="Show command help plus the user documentation page.",
        is_eager=True,
        callback=_show_doc,
    )
