# `account`

Show one Schwab account's full detail — balances, positions, and
instrument-level breakdown.

## Usage

```
schwab_cli account <ACCOUNT_NUMBER> [--json | --md]
```

## Argument

| Arg | Purpose |
| --- | --- |
| `ACCOUNT_NUMBER` | Either the full 8-digit number or the last N digits (≥ 4). `1234` matches `########1234` if unambiguous among your accounts. |

## Flags

Same output format flags as [accounts](accounts.md): `--json`, `--md`.

## Example: HUMAN (sanitised)

```
$ schwab_cli account 1234
Account ####1234 · Brokerage · Main brokerage
Total equity:   $123,456.78    Cash:       $5,000.00
Day change:     +$1,234.00  (+1.01%)

POSITIONS
SYMBOL    QTY     AVG COST    MKT VALUE     UNREALIZED    DAY Δ
NVDA       50      $150.00    $10,112.50     +$2,612.50   +$62.50
AAPL      100      $180.00    $19,500.00       -$500.00  +$125.00
SPY        25      $520.00    $13,150.00       +$150.00   +$37.50

CASH & BUYING POWER
Available for trading:    $5,000.00
Cash balance:             $5,000.00
Margin buying power:     $16,000.00
```

## Example: `--json` (sanitised)

```json
{
  "accountNumber": "########1234",
  "type": "BROKERAGE",
  "equity": 123456.78,
  "cash": 5000.00,
  "dayChange": 1234.00,
  "dayChangePct": 1.01,
  "positions": [
    {
      "symbol": "NVDA",
      "assetType": "EQUITY",
      "longQuantity": 50,
      "averagePrice": 150.00,
      "marketValue": 10112.50,
      "currentDayProfitLoss": 62.50,
      "longOpenProfitLoss": 2612.50
    }
  ],
  "buyingPower": {
    "cashAvailableForTrading": 5000.00,
    "cashBalance": 5000.00,
    "marginBuyingPower": 16000.00
  }
}
```

## Suffix matching

`schwab_cli account 1234` resolves to the account whose number ends in
`1234`. Ambiguous suffixes (matching 2+ accounts) exit with code 2 and
a list of candidates. Use more digits to disambiguate.
