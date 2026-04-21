from __future__ import annotations

from enum import Enum


class Format(Enum):
    HUMAN = "human"
    JSON = "json"
    MD = "md"


class FormatError(Exception):
    """Raised when incompatible format flags are combined."""


def pick_format(json: bool, md: bool) -> Format:
    if json and md:
        raise FormatError("--json and --md are mutually exclusive.")
    if json:
        return Format.JSON
    if md:
        return Format.MD
    return Format.HUMAN
