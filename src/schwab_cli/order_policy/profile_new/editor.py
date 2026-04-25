"""Top-level orchestrator + prompt_toolkit list editor.

The TTY-only interactive driver. Pure state mutation lives in
:mod:`state`; this module is the prompt_toolkit shell.

Public entry: :func:`run_interactive`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer
from prompt_toolkit import Application, prompt
from prompt_toolkit.formatted_text import to_formatted_text
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl

from schwab_cli.order_policy.profile_new.questionnaire import (
    PromptToolkitPrompter,
)
from schwab_cli.order_policy.profile_new.save import (
    ProfileExistsError, atomic_save,
)
from schwab_cli.order_policy.profile_new.state import (
    EditorState,
    add_policy,
    delete_at_cursor,
    mark_saved,
    move_down,
    move_up,
    render_list,
    undo_delete,
)
from schwab_cli.order_policy.profile_new.templates import (
    TEMPLATES, by_key,
)


def run_interactive(*, base_dir: Path) -> int:
    """Drive the full ``profile new --type=order`` flow.

    Returns the desired CLI exit code (0 on save, 0 on quit-without-
    save, 2 on TTY check failure or other usage error).
    """
    if not sys.stdin.isatty():
        typer.secho(
            "profile new requires an interactive TTY.",
            fg=typer.colors.RED, err=True,
        )
        typer.secho(
            "hint: run from a regular terminal, or author profiles by hand "
            "under ~/.config/schwab_cli/profiles/order/<name>.json.",
            fg=typer.colors.YELLOW, err=True,
        )
        return 2

    prompter = PromptToolkitPrompter()

    # ---- top-level questionnaire ----
    typer.echo("\n=== Create a new order profile ===\n", err=True)
    name = _prompt_unique_name(prompter, base_dir=base_dir)
    description = prompter.text("Description (optional)", default="")
    default_action = prompter.select(
        "Default action (when no policy matches)",
        ["allow", "deny"], default="deny",
    )
    notify_on_override = prompter.yes_no(
        "Send a Telegram ping on every override?",
        default=True,
    )

    typer.echo("", err=True)
    typer.secho(
        f"Profile '{name}' — entering policy editor. Press 'c' to add the "
        "first policy.",
        fg=typer.colors.CYAN, err=True,
    )

    # ---- vim-key list editor ----
    state = EditorState()
    saved_path = _list_editor_loop(state=state, base_dir=base_dir, profile_name=name,
                                   description=description,
                                   default_action=default_action,
                                   notify_on_override=notify_on_override,
                                   prompter=prompter)
    if saved_path is None:
        typer.echo("\nprofile not saved.", err=True)
        return 0
    typer.secho(f"\nsaved {saved_path}", fg=typer.colors.GREEN, err=True)
    return 0


# ---- name prompt -------------------------------------------------------


def _prompt_unique_name(prompter, *, base_dir: Path) -> str:
    """Repeat-prompt until the user picks a name that doesn't collide."""
    import re
    name_re = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    while True:
        name = prompter.text("Profile name").strip()
        if not name_re.match(name):
            typer.secho(
                "  invalid: must be 1-64 chars, alphanumerics + _ . -",
                fg=typer.colors.RED, err=True,
            )
            continue
        target = base_dir / f"{name}.json"
        if target.exists():
            typer.secho(
                f"  profile {name!r} already exists at {target} — pick another name.",
                fg=typer.colors.RED, err=True,
            )
            continue
        return name


# ---- list editor (prompt_toolkit Application) -------------------------


def _list_editor_loop(
    *,
    state: EditorState,
    base_dir: Path,
    profile_name: str,
    description: str,
    default_action: str,
    notify_on_override: bool,
    prompter,
) -> Path | None:
    """Drive the vim-key list editor until the user saves or quits.

    Returns the saved path on `s`, or None on `q` (or `q` after
    discard-confirm).
    """
    bindings = KeyBindings()
    result: dict = {"saved_path": None, "quit": False}

    def header_text() -> str:
        return (
            f"profile: {profile_name}   default_action: {default_action}\n"
            f"description: {description or '(none)'}\n"
            f"notify_on_override: {notify_on_override}\n\n"
            f"policies ({len(state.policies)}):\n"
            + render_list(state)
            + "\n\n"
            "keys: j/k = move down/up   c = create   d = delete   u = undo\n"
            "      s = save              q = quit\n"
            + ("\n[unsaved changes]" if state.dirty else "")
        )

    @bindings.add("j")
    @bindings.add("down")
    def _(event):
        move_down(state)
        event.app.invalidate()

    @bindings.add("k")
    @bindings.add("up")
    def _(event):
        move_up(state)
        event.app.invalidate()

    @bindings.add("d")
    def _(event):
        delete_at_cursor(state)
        event.app.invalidate()

    @bindings.add("u")
    def _(event):
        undo_delete(state)
        event.app.invalidate()

    @bindings.add("c")
    def _(event):
        # Run the template-pick + parameter prompts in the terminal,
        # then re-enter the editor.
        def runner():
            try:
                policy = _run_template_picker(prompter)
                if policy is not None:
                    add_policy(state, policy)
            except Exception as e:  # noqa: BLE001 — show & continue
                typer.secho(
                    f"  error creating policy: {e}",
                    fg=typer.colors.RED, err=True,
                )
        event.app.run_in_terminal(runner)

    @bindings.add("s")
    def _(event):
        # Same trick — leave the editor briefly to show the path /
        # any error.
        def runner():
            profile_data = {
                "description": description,
                "default_action": default_action,
                "notify_on_override": notify_on_override,
                "policies": list(state.policies),
            }
            try:
                p = atomic_save(
                    profile_name=profile_name,
                    profile_data=profile_data,
                    base_dir=base_dir,
                )
            except ProfileExistsError as e:
                typer.secho(
                    f"  cannot save: {e} already exists",
                    fg=typer.colors.RED, err=True,
                )
                return
            mark_saved(state, str(p))
            result["saved_path"] = p
            event.app.exit()
        event.app.run_in_terminal(runner)

    @bindings.add("q")
    def _(event):
        if not state.dirty:
            result["quit"] = True
            event.app.exit()
            return
        # Confirm discard.
        def runner():
            ans = prompt("discard unsaved changes? [y/N]: ").strip().lower()
            if ans in ("y", "yes"):
                result["quit"] = True
                event.app.exit()
        event.app.run_in_terminal(runner)

    @bindings.add("c-c")
    def _(event):
        # Treat Ctrl+C as "q without confirm" — same as a vim user
        # would expect.
        result["quit"] = True
        event.app.exit()

    control = FormattedTextControl(text=lambda: to_formatted_text(header_text()))
    layout = Layout(Window(content=control))
    app = Application(
        layout=layout,
        key_bindings=bindings,
        full_screen=False,
        mouse_support=False,
    )
    app.run()
    return result["saved_path"]


# ---- template picker ---------------------------------------------------


def _run_template_picker(prompter) -> dict | None:
    """Show the template menu, run the chosen template's prompts,
    and return the assembled policy dict (or None if the user
    aborted the wizard mid-flow)."""
    typer.echo("", err=True)
    typer.secho("Pick a template:", fg=typer.colors.CYAN, err=True)
    for i, t in enumerate(TEMPLATES, start=1):
        typer.echo(f"  [{i}] {t.label} — {t.description}", err=True)
    raw = prompt("template number (or 'q' to cancel): ").strip().lower()
    if raw in ("", "q"):
        return None
    try:
        idx = int(raw) - 1
    except ValueError:
        typer.secho(f"  not a number: {raw!r}", fg=typer.colors.RED, err=True)
        return None
    if not (0 <= idx < len(TEMPLATES)):
        typer.secho(
            f"  out of range: {raw!r} (1..{len(TEMPLATES)})",
            fg=typer.colors.RED, err=True,
        )
        return None
    template = TEMPLATES[idx]
    typer.echo("", err=True)
    typer.secho(f"-- {template.label} --", fg=typer.colors.CYAN, err=True)
    policy = template.build(prompter)
    # Brief preview + confirm.
    typer.echo("\nbrief:", err=True)
    typer.echo(f"  name:       {policy.get('name', '?')}", err=True)
    typer.echo(f"  effect:     {policy.get('effect', '?')}", err=True)
    typer.echo(f"  match:      {policy.get('match')}", err=True)
    typer.echo(f"  conditions: {policy.get('conditions', [])}", err=True)
    if not prompter.yes_no("\nadd this policy?", default=True):
        return None
    return policy
