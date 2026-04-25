"""prompt_toolkit-backed input helpers used by the editor + templates.

Two real :class:`Prompter` implementations live here:

* :class:`PromptToolkitPrompter` — uses :func:`prompt_toolkit.prompt`
  for the top-level wizard (name, default action, etc.) before the
  interactive list-editor Application starts. Gives autocomplete and
  inline validators.
* :class:`StdinPrompter` — uses plain :func:`input` and is the only
  safe choice while the editor's Application is already running. A
  nested ``prompt_toolkit.prompt`` call inside ``run_in_terminal``
  raises ``asyncio.run() cannot be called from a running event loop``
  because the outer app already owns the loop.

Tests exercise the templates with a stub implementation of the same
protocol; the two classes here are only used in real interactive
sessions.
"""

from __future__ import annotations

from typing import Iterable

from prompt_toolkit import prompt
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.validation import ValidationError, Validator

from schwab_cli.order_policy.profile_new.templates import Prompter


class PromptToolkitPrompter(Prompter):
    """Real-terminal implementation of :class:`Prompter`."""

    def text(self, label: str, *, default: str = "") -> str:
        return prompt(f"{label}: ", default=default).strip()

    def select(
        self, label: str, choices: list[str], *,
        default: str | None = None,
    ) -> str:
        completer = WordCompleter(choices, ignore_case=True)
        validator = _OneOfValidator(choices)
        out = prompt(
            f"{label} [{'/'.join(choices)}]: ",
            completer=completer,
            validator=validator,
            default=default or "",
        ).strip()
        # Validator already enforced membership.
        return out

    def integer(
        self, label: str, *,
        default: int | None = None,
        min_value: int | None = None,
    ) -> int | None:
        suffix = f" [{default}]" if default is not None else ""
        validator = _IntegerOrEmptyValidator(min_value=min_value)
        raw = prompt(
            f"{label}{suffix}: ",
            validator=validator,
            default=str(default) if default is not None else "",
        ).strip()
        if raw == "":
            return default
        return int(raw)

    def number(
        self, label: str, *,
        default: float | None = None,
    ) -> float | None:
        suffix = f" [{default}]" if default is not None else ""
        validator = _FloatOrEmptyValidator()
        raw = prompt(
            f"{label}{suffix}: ",
            validator=validator,
            default=str(default) if default is not None else "",
        ).strip()
        if raw == "":
            return default
        return float(raw)

    def yes_no(self, label: str, *, default: bool = False) -> bool:
        d = "y" if default else "n"
        choices = "[Y/n]" if default else "[y/N]"
        out = prompt(f"{label} {choices}: ", default=d).strip().lower()
        if not out:
            return default
        return out in ("y", "yes")


class StdinPrompter(Prompter):
    """Plain :func:`input`-backed implementation. Loses autocomplete /
    inline validators but is safe to use inside the editor's running
    prompt_toolkit Application (where a nested ``prompt`` would crash).
    Re-prompts on bad input rather than raising."""

    def text(self, label: str, *, default: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        out = input(f"{label}{suffix}: ").strip()
        return out or default

    def select(
        self, label: str, choices: list[str], *,
        default: str | None = None,
    ) -> str:
        suffix = f" [{'/'.join(choices)}]"
        if default:
            suffix += f" (default {default})"
        while True:
            out = input(f"{label}{suffix}: ").strip()
            if not out and default is not None:
                return default
            if out in choices:
                return out
            print(f"  must be one of: {', '.join(choices)}")

    def integer(
        self, label: str, *,
        default: int | None = None,
        min_value: int | None = None,
    ) -> int | None:
        suffix = f" [{default}]" if default is not None else ""
        while True:
            raw = input(f"{label}{suffix}: ").strip()
            if not raw:
                return default
            try:
                v = int(raw)
            except ValueError:
                print("  must be an integer (or blank)")
                continue
            if min_value is not None and v < min_value:
                print(f"  must be ≥ {min_value}")
                continue
            return v

    def number(
        self, label: str, *,
        default: float | None = None,
    ) -> float | None:
        suffix = f" [{default}]" if default is not None else ""
        while True:
            raw = input(f"{label}{suffix}: ").strip()
            if not raw:
                return default
            try:
                return float(raw)
            except ValueError:
                print("  must be a number (or blank)")

    def yes_no(self, label: str, *, default: bool = False) -> bool:
        choices = "[Y/n]" if default else "[y/N]"
        out = input(f"{label} {choices}: ").strip().lower()
        if not out:
            return default
        return out in ("y", "yes")


# ---- validators ----------------------------------------------------------


class _OneOfValidator(Validator):
    def __init__(self, allowed: Iterable[str]) -> None:
        self._allowed = list(allowed)

    def validate(self, document) -> None:
        text = document.text.strip()
        if text not in self._allowed:
            raise ValidationError(
                message=f"must be one of: {', '.join(self._allowed)}",
                cursor_position=len(document.text),
            )


class _IntegerOrEmptyValidator(Validator):
    def __init__(self, *, min_value: int | None = None) -> None:
        self._min = min_value

    def validate(self, document) -> None:
        text = document.text.strip()
        if not text:
            return
        try:
            v = int(text)
        except ValueError:
            raise ValidationError(
                message="must be an integer (or blank)",
                cursor_position=len(document.text),
            )
        if self._min is not None and v < self._min:
            raise ValidationError(
                message=f"must be ≥ {self._min}",
                cursor_position=len(document.text),
            )


class _FloatOrEmptyValidator(Validator):
    def validate(self, document) -> None:
        text = document.text.strip()
        if not text:
            return
        try:
            float(text)
        except ValueError:
            raise ValidationError(
                message="must be a number (or blank)",
                cursor_position=len(document.text),
            )
