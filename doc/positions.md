# `positions`

List positions across one account or all accounts.

## Usage

```
schwab_cli positions [ACCOUNT_NUMBER] [--json | --md]
```

## Argument

| Arg | Purpose |
| --- | --- |
| `ACCOUNT_NUMBER` | Optional. Full number or last-N-digit suffix. When omitted, positions from every account are fanned out and tagged with their account. |

## Flags

| Flag | Purpose |
| --- | --- |
| `--json` | JSON envelope keyed by account. |
| `--md` | Markdown table with an extra `Account` column. |

## Example: HUMAN, single account (sanitised)

```
$ schwab_cli positions 1234
Account ####1234

SYMBOL            QTY   AVG COST   MKT VALUE   UNREALIZED      DAY Δ    DAY Δ%
NVDA               50    $150.00   $10,112.50   +$2,612.50    +$62.50     +0.62%
AAPL              100    $180.00   $19,500.00     -$500.00   +$125.00     +0.64%
NVDA…C00250000      2      $4.50      $1,000.00   +$100.00     -$10.00     -0.99%
```

## Example: HUMAN, all accounts

```
$ schwab_cli positions
ACCOUNT    SYMBOL   QTY   AVG COST   MKT VALUE  UNREALIZED    DAY Δ
####1234   NVDA      50    $150.00   $10,112.50  +$2,612.50   +$62.50
####1234   AAPL     100    $180.00   $19,500.00    -$500.00  +$125.00
####5678   SPY       25    $520.00   $13,150.00    +$150.00   +$37.50
####5678   QQQ       30    $450.00   $13,725.00    +$225.00   +$60.00
```

## Example: `--json` (sanitised)

```json
{
  "positions": [
    {
      "accountNumber": "########1234",
      "symbol": "NVDA",
      "assetType": "EQUITY",
      "longQuantity": 50.0,
      "averagePrice": 150.00,
      "marketValue": 10112.50,
      "dayChange": 62.50,
      "dayChangePct": 0.62,
      "unrealized": 2612.50
    },
    {
      "accountNumber": "########1234",
      "symbol": "NVDA  270115C00250000",
      "assetType": "OPTION",
      "longQuantity": 2.0,
      "averagePrice": 4.50,
      "marketValue": 1000.00,
      "underlying": "NVDA",
      "strike": 250.0,
      "expiry": "2027-01-15",
      "side": "C"
    }
  ]
}
```

## Notes

- Option positions surface the underlying, strike, expiry, and side as
  separate fields in JSON so downstream scripts don't have to re-parse
  the OSI symbol.
- Short positions are shown with negative quantities.
- Mutual funds and other instrument types share the same row shape; the
  `assetType` field is the authoritative classifier.
