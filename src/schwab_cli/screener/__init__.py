"""Options VRP screener — executable put-selling premium screener.

Built on the daily volatility-snapshot pipeline. Locates a target
~30 DTE / ~-0.25Δ put per constituent, snapshots its bid-side quote,
hard-filters illiquid / event names, ranks survivors by executable VRP,
and keeps a paper ledger to validate the ranking's discrimination.

Produces a candidate pool only — never places an order.
"""
