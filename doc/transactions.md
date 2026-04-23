# `transactions`

Account activity — trades, dividends, journals, cash transfers — across
one account or all of them.

## Usage

```
schwab_cli transactions [ACCOUNT] [--range=...] [--type=...] [--json | --md]
```

## Argument

| Arg | Purpose |
| --- | --- |
| `ACCOUNT` | Optional. Full account number or last-N-digit suffix. Omit to fan out across every account. |

## Flags

| Flag | Default | Purpose |
| --- | --- | --- |
| `--range` | `-7d..now` | Date window. See [time](time.md). |
| `--type` | `TRADE` | Filter: `TRADE`, `DIVIDEND_OR_INTEREST`, `JOURNAL`, `RECEIVE_AND_DELIVER`, or `ALL`. |
| `--json` | | JSON array of transactions. |
| `--md` | | Markdown table. |

## Example: HUMAN, single account (sanitised)

```
$ schwab_cli transactions 1234 --range=-30d..now --type=TRADE
Account ####1234 · TRADE · 30 days

DATE        ACTION       SYMBOL     QTY     PRICE     AMOUNT      FEES
2026-04-15  BUY          NVDA        50    $150.00  -$7,500.00    $0.00
2026-04-17  SELL          NVDA…C230    2      $3.50    +$700.00    $1.30
2026-04-20  BUY_TO_OPEN  NVDA…C250    3      $5.40  -$1,620.00    $1.95
```

## Example: HUMAN, all accounts, mixed types

```
$ schwab_cli transactions --type=ALL
ACCOUNT     DATE        ACTION          SYMBOL                   AMOUNT      DESC
####1234    2026-04-16  DIVIDEND        NVDA                        +$5.00  NVIDIA Corp
####1234    2026-04-17  TRADE           NVDA  270115C00250000   -$1,620.00  buy to open
####5678    2026-04-18  JOURNAL         —                          +$500.00  Transfer in
####5678    2026-04-19  DIVIDEND        SPY                        +$42.15  SPDR S&P 500
####5678    2026-04-20  DIVIDEND        AAPL                        +$8.00  Apple Inc.
```

**Note**: in earlier versions, dividend symbols rendered as
`CURRENCY_USD`. The CLI now preferentially shows the issuer name for
dividend and journal legs where the `transferItem` is the currency
leg — so `AAPL`, `SPY` etc. appear as the symbol.

## Example: `--json`

```json
[
  {
    "accountNumber": "########1234",
    "transactionId": 1234567890,
    "date": "2026-04-17T14:32:18Z",
    "type": "TRADE",
    "action": "BUY_TO_OPEN",
    "symbol": "NVDA  270115C00250000",
    "underlying": "NVDA",
    "quantity": 3,
    "price": 5.40,
    "amount": -1620.00,
    "fees": 1.95,
    "description": "NVDA Jan 15 2027 250 CALL"
  }
]
```

## Type filter semantics

| Filter | Meaning |
| --- | --- |
| `TRADE` | Buys, sells, open/close of any instrument. |
| `DIVIDEND_OR_INTEREST` | Cash dividends and interest credits. |
| `JOURNAL` | Cash transfers between your Schwab accounts. |
| `RECEIVE_AND_DELIVER` | ACATs / stock-by-stock transfers. |
| `ALL` | No filter — all of the above. |

## Range examples

```bash
# Default: last 7 days of trades
schwab_cli transactions

# All activity for April
schwab_cli transactions --range=20260401..20260430 --type=ALL

# Year-to-date dividends across every account
schwab_cli transactions --range=ytd --type=DIVIDEND_OR_INTEREST
```

## Notes

- Multi-account mode fans out one API call per account, so it's slower
  than single-account queries.
- Transaction timestamps are UTC in JSON; human output converts to
  NY-market-time.
- Short trades are negative quantities; sell-to-close is also negative
  in the `quantity` field of JSON output.
