from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

_SPEC_RE = re.compile(
    r"^(?P<date>\d{6})(?P<type>[PC])?(?:\*(?P<strike>\d+(?:\.\d+)?)?)?$"
)


@dataclass(frozen=True)
class OptionSpec:
    expiry: date
    contract_type: Literal["CALL", "PUT", "ALL"]
    strike: float | None


class OptionSpecError(ValueError):
    """Raised when the spec string doesn't match the grammar or has an invalid date."""

    def __init__(self, message: str, *, kind: str = "invalid") -> None:
        super().__init__(message)
        self.kind = kind


def parse_option_spec(spec: str, *, today: date | None = None) -> OptionSpec:
    match = _SPEC_RE.match(spec or "")
    if match is None:
        raise OptionSpecError(
            f"Invalid option spec {spec!r}. "
            "Expected YYMMDD[P|C]*[strike] — e.g. '270115*250' or '270115P*'.",
            kind="invalid",
        )

    date_str = match.group("date")
    year = 2000 + int(date_str[0:2])
    month = int(date_str[2:4])
    day = int(date_str[4:6])
    try:
        expiry = date(year, month, day)
    except ValueError as e:
        raise OptionSpecError(
            f"Invalid expiry date in {spec!r}: {e}",
            kind="bad_date",
        ) from e

    now = today or date.today()
    if expiry < now:
        raise OptionSpecError(
            f"Expiry {expiry.isoformat()} is in the past.",
            kind="expired",
        )

    type_letter = match.group("type")
    if type_letter == "P":
        contract_type: Literal["CALL", "PUT", "ALL"] = "PUT"
    elif type_letter == "C":
        contract_type = "CALL"
    else:
        contract_type = "ALL"

    strike_str = match.group("strike")
    strike = float(strike_str) if strike_str is not None else None

    return OptionSpec(expiry=expiry, contract_type=contract_type, strike=strike)
