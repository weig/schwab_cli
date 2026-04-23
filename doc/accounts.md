# `accounts`

List every brokerage account your Schwab session has access to. For a
single account by number or last-N-digit suffix, see [account](account.md).

## Usage

```
schwab_cli accounts [--json | --md]
```

## Flags

| Flag | Purpose |
| --- | --- |
| `--json` | Emit JSON envelope (account numbers included in full). |
| `--md` | Emit GitHub-flavoured markdown. |

## Example: HUMAN (sanitised)

```
$ schwab_cli accounts
ACCOUNT   TYPE              EQUITY       CASH     DAY Δ     DAY Δ%
…###1234  Brokerage       $123,456.78   $5,000   +$1,234     +1.01%
…###5678  Roth IRA         $45,000.00   $2,000     -$120     -0.27%
…###9012  Individual       $12,345.67     $250      +$45     +0.37%
```

## Example: `--json`

```json
[
  {
    "accountNumber": "########1234",
    "hashValue": "AAABBBCCC...",
    "type": "BROKERAGE",
    "nickname": "Main brokerage",
    "equity": 123456.78,
    "cash": 5000.00,
    "dayChange": 1234.00,
    "dayChangePct": 1.01
  }
]
```

## Example: `--md`

```markdown
| Account | Type | Equity | Cash | Day Δ | Day Δ% |
| --- | --- | ---: | ---: | ---: | ---: |
| ####1234 | Brokerage | $123,456.78 | $5,000.00 | +$1,234.00 | +1.01% |
| ####5678 | Roth IRA | $45,000.00 | $2,000.00 | -$120.00 | -0.27% |
```

## Notes

- Account numbers are shown with the first 4 digits masked in HUMAN and
  MD output; `--json` carries them unmasked so you can pipe into scripts.
- The `hashValue` in JSON is Schwab's opaque account identifier used
  by other endpoints (transactions, orders) — treat it as a secret.
