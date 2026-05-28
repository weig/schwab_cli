"""Long-lived auth-maintenance server for schwab_cli.

``schwab server`` (bare) runs a maintenance loop that keeps the OAuth
refresh token alive: when the refresh token nears expiry it triggers a
full (browser/auto-login) re-auth; otherwise it just ensures the access
token is fresh via the pure-HTTP service layer. ``server install`` wires
it up as a macOS launchd LaunchAgent.
"""
