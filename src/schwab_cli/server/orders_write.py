"""Order MUTATIONS for the resource server (webauth P4b).

``POST /api/v1/orders`` places an order through the SAME battle-tested
pipeline the CLI uses, minus its interactive/rendering rules:

    LoadProfile → fetch(balances/quote/chain, FORCED) → DetectOpenClose
    → SchwabPreview (FORCED) → ComputeAnalytics → PolicyEvaluate →
    PolicyDenyGate → PreviewRejectGate → PlaceOrder

Everything protective is inherited, not reimplemented: profile policy
evaluation + deny gate, Schwab's preview-reject gate, the audit trail,
and the per-day/per-ticker counters (bumped inside PlaceOrderRule).
"FORCED" rules normally skip under ``--yes`` (the CLI's scripted mode
trusts the human who typed the flag); over REST nobody is watching, so
the account fetch (drives the open/close leg rewrite) and Schwab's
preview validation always run. Deliberately ABSENT relative to the
CLI: the override ceremony — there is no ``--override`` over REST; a
policy deny is final (use the CLI for the human-ceremony path).

Scope model (per the agreed design): the request MUST name a profile
explicitly (no fallback — an order writer must consciously pick the
policy it runs under) and the caller must hold ``order:<profile>``
(or ``order:*``). The profile's policy then decides whether THIS order
may be placed — the scope is the entry ticket, the policy is the law.
A profile that fails to load is a hard 403, at the pre-check AND if it
vanishes mid-pipeline; additionally the gateway refuses to report
success unless the pipeline produced an APPROVED policy decision
(defense in depth against future rule reordering).

Idempotency: pass ``idempotency_key`` in the body — replays within the
process's TTL window return the original 201 without re-running the
pipeline (a retry after a timed-out response must not double-place).

``DELETE /api/v1/accounts/{account}/orders/{order_id}`` cancels an
order. Cancel is risk-reducing, so it skips policy evaluation but
still requires an ``order:``-domain scope and leaves an audit trail.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import re
import threading
import time
from typing import Any

import click

from schwab_cli.order_pipeline.runner import PipelineExit, run_pipeline
from schwab_cli.service.base import BaseService
from schwab_cli.webauth.scopes import scope_satisfied

_ACCOUNT_RE = re.compile(r"[0-9]{1,12}$")
_ORDER_ID_RE = re.compile(r"[0-9]{1,20}$")
_IDEMPOTENCY_TTL_S = 24 * 3600


def order_write_routes() -> list:
    from starlette.routing import Route

    return [
        Route("/api/v1/orders", _place, methods=["POST"]),
        Route(
            "/api/v1/accounts/{account}/orders/{order_id}",
            _cancel, methods=["DELETE"],
        ),
    ]


# ---------------------------------------------------------------------------
# Scope checks (dynamic: the required scope depends on the request body)
# ---------------------------------------------------------------------------


def _principal(request):
    return request.scope.get("state", {}).get("principal")


def _profile_scope_denial(request, profile_name: str):
    """``order:<profile>`` (or ``order:*``) admits the request; the
    profile's policy still decides the order's fate afterwards."""
    principal = _principal(request)
    if principal is None:
        return None  # legacy mode: loopback-only, pre-webauth contract
    if scope_satisfied(principal.scopes, f"order:{profile_name}"):
        return None
    return _json(
        {"error": f"missing required scope: order:{profile_name}"},
        status=403,
    )


def _order_domain_denial(request):
    """Cancel needs any ``order:`` grant — it names no profile."""
    principal = _principal(request)
    if principal is None:
        return None
    if any(s == "order:*" or s.startswith("order:") for s in principal.scopes):
        return None
    return _json({"error": "missing required scope: order:<profile>"}, status=403)


# ---------------------------------------------------------------------------
# Idempotency (per-process; the daemon is the only writer)
# ---------------------------------------------------------------------------


class _IdempotencyCache:
    """Replays of a successful placement return the original response.

    In-memory with a TTL: survives exactly as long as the daemon — a
    restart clears it, which errs on the side of the caller having to
    check `GET /api/v1/orders` before retrying across restarts.
    """

    def __init__(self, ttl_s: float = _IDEMPOTENCY_TTL_S) -> None:
        self._ttl_s = ttl_s
        self._lock = threading.Lock()
        self._store: dict[str, tuple[float, dict]] = {}

    def get(self, key: str) -> dict | None:
        now = time.time()
        with self._lock:
            self._store = {
                k: v for k, v in self._store.items() if now - v[0] < self._ttl_s
            }
            hit = self._store.get(key)
        return hit[1] if hit else None

    def put(self, key: str, response: dict) -> None:
        with self._lock:
            self._store[key] = (time.time(), response)


_idempotency = _IdempotencyCache()


# ---------------------------------------------------------------------------
# Place
# ---------------------------------------------------------------------------


async def _place(request):
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 — malformed body is a client error
        return _json({"error": "request body must be JSON"}, status=400)
    if not isinstance(payload, dict):
        return _json({"error": "request body must be a JSON object"}, status=400)

    profile_name = payload.get("profile")
    if not isinstance(profile_name, str) or not profile_name:
        # No fallback: order placement must consciously name the policy
        # profile it runs under.
        return _json({"error": "profile is required"}, status=400)
    denial = _profile_scope_denial(request, profile_name)
    if denial is not None:
        return denial

    account = payload.get("account")
    if not isinstance(account, str) or not _ACCOUNT_RE.fullmatch(account):
        return _json(
            {"error": "account is required (digits, full or suffix)"},
            status=400,
        )

    return await asyncio.to_thread(
        _place_sync, payload, account=account, profile_name=profile_name,
    )


def _place_sync(payload: dict, *, account: str, profile_name: str):
    # Imports deferred: commands.order pulls typer/rich; only the
    # order-write path pays that cost.
    from schwab_cli import order_limits
    from schwab_cli.commands.order import (
        EXIT_POLICY_REJECTED,
        EXIT_REJECTED,
        EXIT_USAGE,
        _audit,
        _build_body,
        _spec_from_flags,
        _spec_from_ticket,
        _validate_combo,
        _validate_session_combo,
    )
    from schwab_cli.order_pipeline.context import OrderContext
    from schwab_cli.order_policy import load_profile
    from schwab_cli.order_ticket import TicketParseError, parse_ticket
    from schwab_cli.service.auth import (
        ApiError,
        NotAuthenticated,
        NotConfigured,
        SessionExpired,
    )

    _audit(
        "place", "invoked",
        source="rest",
        account=account,
        profile=profile_name,
        flags={"ticket": payload.get("ticket"), "order": payload.get("order")},
    )

    idem_key = payload.get("idempotency_key")
    if idem_key is not None:
        if not isinstance(idem_key, str) or not idem_key:
            return _json(
                {"error": "idempotency_key must be a non-empty string"},
                status=400,
            )
        replay = _idempotency.get(idem_key)
        if replay is not None:
            _audit("place", "idempotent_replay", source="rest",
                   account=account, order_id=replay.get("order_id"))
            return _json({**replay, "replayed": True}, status=201)

    # The profile must LOAD — over REST an unpoliced order can never
    # slip through (the CLI's no-profile mode is a preview convenience).
    try:
        load_profile(profile_name)
    except Exception as e:  # noqa: BLE001 — missing/broken profile file
        _audit("place", "profile_unavailable", source="rest",
               account=account, profile=profile_name,
               error=f"{type(e).__name__}: {e}")
        return _json(
            {"error": f"profile {profile_name!r} unavailable: "
                      f"{type(e).__name__}"},
            status=403,
        )

    # CLI validators echo their reason to stderr and raise Exit —
    # capture the text so the API caller sees WHY the combo is invalid.
    captured = io.StringIO()
    try:
        with contextlib.redirect_stderr(captured):
            spec, body = _build_spec_and_body(
                payload,
                parse_ticket=parse_ticket,
                spec_from_ticket=_spec_from_ticket,
                spec_from_flags=_spec_from_flags,
                validate_combo=_validate_combo,
                validate_session_combo=_validate_session_combo,
                build_body=_build_body,
            )
    except TicketParseError as e:
        return _json({"error": str(e)}, status=400)
    except _BadRequest as e:
        return _json({"error": str(e)}, status=400)
    except click.exceptions.Exit:
        detail = captured.getvalue().strip() or "invalid order parameters"
        return _json({"error": detail}, status=400)
    except (KeyError, TypeError, ValueError) as e:
        return _json(
            {"error": f"invalid order parameters: {type(e).__name__}"},
            status=400,
        )

    gateway = _PlacementGateway()
    try:
        ctx = gateway.run(
            body=body, spec=spec, account=account,
            profile_name=profile_name, limits_mod=order_limits,
            order_context_cls=OrderContext,
        )
    except _AccountResolution as e:
        return _json({"error": str(e)}, status=400)
    except (NotConfigured, NotAuthenticated) as e:
        return _json({"error": type(e).__name__}, status=503)
    except (ApiError, SessionExpired) as e:
        # Upstream detail can carry account/order data — audit it
        # server-side, return a generic body.
        _audit("place", "place_failed", source="rest",
               account=account, error=_err(e))
        return _json({"error": "upstream API error"}, status=502)
    except PipelineExit as e:
        return _pipeline_exit_response(
            e.exit_code, gateway.last_ctx,
            policy_code=EXIT_POLICY_REJECTED, reject_code=EXIT_REJECTED,
            usage_code=EXIT_USAGE,
        )
    except click.exceptions.Exit as e:
        # _handle_api_error inside PlaceOrderRule maps API failures to
        # CLI exits; translate them back to HTTP.
        code = getattr(e, "exit_code", 1)
        return _pipeline_exit_response(
            code, gateway.last_ctx,
            policy_code=EXIT_POLICY_REJECTED, reject_code=EXIT_REJECTED,
            usage_code=EXIT_USAGE,
        )

    # Defense in depth: success REQUIRES an approved policy decision.
    # The pipeline guarantees this today (LoadProfileRule halts real
    # placements without a profile); this guard makes the invariant
    # load-bearing against any future rule reordering.
    decision = getattr(ctx, "policy_decision", None)
    if decision is None or not getattr(decision, "approved", False):
        _audit("place", "post_place_invariant_violated", source="rest",
               account=account, profile=profile_name,
               order_id=getattr(ctx, "placed_order_id", None))
        return _json(
            {"error": "internal policy invariant violated — check audit log"},
            status=500,
        )

    response = {
        "order_id": ctx.placed_order_id,
        "account": ctx.account.account_number[-4:],
        "profile": profile_name,
        "policy_rule": decision.rule_name,
    }
    if idem_key is not None:
        _idempotency.put(idem_key, response)
    return _json(response, status=201)


class _BadRequest(Exception):
    """Request-shape problem detected before the pipeline — HTTP 400."""


class _AccountResolution(Exception):
    """Account didn't resolve (not found / ambiguous) — HTTP 400.

    resolve_account's messages are last-4 masked, safe to surface.
    """


def _build_spec_and_body(
    payload: dict, *, parse_ticket, spec_from_ticket, spec_from_flags,
    validate_combo, validate_session_combo, build_body,
):
    ticket = payload.get("ticket")
    order = payload.get("order") or {}
    if ticket is not None:
        if not isinstance(ticket, str):
            raise _BadRequest("ticket must be a string")
        spec = spec_from_ticket(parse_ticket(ticket))
    else:
        if not isinstance(order, dict) or not order.get("symbol"):
            raise _BadRequest("order.symbol (or a ticket string) is required")
        legs = order.get("legs") or []
        if not isinstance(legs, list) or not all(
            isinstance(s, str) for s in legs
        ):
            raise _BadRequest("order.legs must be a list of leg strings")
        validate_combo(
            parse_string=None,
            symbol=order.get("symbol"),
            order_type=order.get("order_type"),
            price=order.get("price"),
            quantity=order.get("quantity"),
            side=order.get("side"),
            duration=order.get("duration"),
            leg_specs=legs,
            complex_strategy=order.get("complex_strategy"),
        )
        spec = spec_from_flags(
            symbol=order["symbol"],
            side=order.get("side") or "BUY",
            quantity=int(order.get("quantity") or 1),
            order_type=order.get("order_type") or "LIMIT",
            price=order.get("price"),
            duration=order.get("duration") or "DAY",
            session=order.get("session"),
            leg_specs=legs,
            complex_strategy=order.get("complex_strategy") or "AUTO",
            stop_price=order.get("stop_price"),
            trailing_offset=order.get("trailing_offset"),
            trailing_basis=order.get("trailing_basis"),
            trailing_type=order.get("trailing_type"),
        )
    validate_session_combo(
        session=spec.session,
        order_type=spec.order_type,
        duration=spec.duration,
    )
    return spec, build_body(spec)


class _PlacementGateway(BaseService):
    """Authed-client plumbing + the headless pipeline run."""

    def run(self, *, body, spec, account, profile_name, limits_mod,
            order_context_cls):
        from schwab_cli.api.client import ApiError

        self.last_ctx = None
        with self._authed_client() as client:
            try:
                acct = client.resolve_account(account)
            except ApiError as e:
                # Not-found / ambiguous-suffix — messages are last-4
                # masked at source, safe and useful for the caller.
                raise _AccountResolution(str(e)) from e
            limits = limits_mod.evaluate(
                body, account_number=acct.account_number,
            )
            ctx = order_context_cls(
                spec=spec,
                body=body,
                account=acct,
                client=client,
                sub="place",
                dry_run=False,
                yes=True,          # REST is non-interactive by definition
                overriding=False,  # no override ceremony over the wire
                profile_name=profile_name,
                override_reason=None,
                as_json=True,
                limits=limits,
            )
            self.last_ctx = ctx
            run_pipeline(_headless_rules(), ctx)
            return ctx


class _Force:
    """Run a pipeline rule unconditionally.

    Several CLI rules skip themselves under ``--yes`` (scripted mode
    trusts the human at the terminal): the account/quote/chain fetches
    and SchwabPreviewRule. Over REST nobody is watching — the account
    fetch drives the open/close leg rewrite and the preview drives the
    PreviewRejectGate, so they must always run.
    """

    def __init__(self, rule) -> None:
        self._rule = rule
        self.name = getattr(rule, "name", type(rule).__name__)

    def applies(self, ctx) -> bool:
        return True

    def execute(self, ctx):
        return self._rule.execute(ctx)


def _headless_rules() -> tuple:
    """The CLI pipeline minus its interactive / rendering rules.

    Dropped: RenderPanel/EmitPanel (terminal output), NoProfileNotice +
    DryRunComplete (preview-mode UX), OverrideCeremony (no override over
    REST), ConfirmRule (non-interactive). Everything protective stays,
    with the fetch + preview rules FORCED past their --yes skip.
    """
    from schwab_cli.order_pipeline.rules import (
        ComputeAnalyticsRule,
        DetectOpenCloseRule,
        FetchAccountBalancesRule,
        FetchChainRule,
        FetchUnderlyingQuoteRule,
        LoadProfileRule,
        ParallelFetchRule,
        PolicyDenyGateRule,
        PolicyEvaluateRule,
        PreviewRejectGateRule,
        SchwabPreviewRule,
        PlaceOrderRule,
    )

    return (
        LoadProfileRule(),
        ParallelFetchRule(
            _Force(FetchAccountBalancesRule()),
            _Force(FetchUnderlyingQuoteRule()),
            _Force(FetchChainRule()),
        ),
        DetectOpenCloseRule(),
        _Force(SchwabPreviewRule()),
        ComputeAnalyticsRule(),
        PolicyEvaluateRule(),
        PolicyDenyGateRule(),
        PreviewRejectGateRule(),
        PlaceOrderRule(),
    )


def _pipeline_exit_response(code: int, ctx, *, policy_code, reject_code,
                            usage_code):
    if code == policy_code:
        decision = getattr(ctx, "policy_decision", None)
        return _json({
            "error": "rejected by policy",
            "profile": getattr(ctx, "profile_name", None),
            "rule": getattr(decision, "rule_name", None),
            "reason": getattr(decision, "reason", None),
        }, status=403)
    if code == reject_code:
        # Schwab preview rejects go to the authenticated order-writer
        # verbatim — they routinely reference the caller's own buying
        # power / positions, which the caller is entitled to see.
        summary = getattr(ctx, "preview_summary", None)
        rejects = list(getattr(summary, "rejects", []) or [])
        return _json({
            "error": "rejected by Schwab preview",
            "rejects": rejects,
        }, status=422)
    if code == usage_code:
        if getattr(ctx, "profile_load_error", None):
            # TOCTOU: the profile vanished between the pre-check and
            # LoadProfileRule. Same contract as the pre-check: 403.
            return _json({
                "error": "profile unavailable",
                "detail": getattr(ctx, "profile_load_error", None),
            }, status=403)
        return _json({"error": "invalid order parameters"}, status=400)
    return _json({"error": "order placement failed"}, status=502)


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


async def _cancel(request):
    denial = _order_domain_denial(request)
    if denial is not None:
        return denial
    account = request.path_params["account"]
    order_id = request.path_params["order_id"]
    if not _ACCOUNT_RE.fullmatch(account):
        return _json({"error": "account must be digits"}, status=400)
    if not _ORDER_ID_RE.fullmatch(order_id):
        return _json({"error": "order_id must be digits"}, status=400)
    return await asyncio.to_thread(_cancel_sync, account, order_id)


def _cancel_sync(account: str, order_id: str):
    from schwab_cli.commands.order import _audit
    from schwab_cli.service.auth import (
        ApiError,
        NotAuthenticated,
        NotConfigured,
        SessionExpired,
    )

    _audit("cancel", "invoked", source="rest",
           account=account, order_id=order_id)
    gateway = _CancelGateway()
    try:
        gateway.cancel(account, order_id)
    except (NotConfigured, NotAuthenticated) as e:
        _audit("cancel", "failed", source="rest",
               account=account, order_id=order_id, error=type(e).__name__)
        return _json({"error": type(e).__name__}, status=503)
    except (ApiError, SessionExpired) as e:
        _audit("cancel", "failed", source="rest",
               account=account, order_id=order_id, error=_err(e))
        return _json({"error": "upstream API error"}, status=502)
    _audit("cancel", "cancelled", source="rest",
           account=account, order_id=order_id)
    return _json({"cancelled": order_id})


class _CancelGateway(BaseService):
    def cancel(self, account: str, order_id: str) -> None:
        from schwab_cli.api import orders as api_orders

        with self._authed_client() as client:
            acct = client.resolve_account(account)
            api_orders.cancel_order(client, acct.hash_value, order_id)


# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------


def _err(e: Exception) -> str:
    detail = str(e)
    return f"{type(e).__name__}: {detail}" if detail else type(e).__name__


def _json(data: Any, *, status: int = 200):
    from starlette.responses import Response

    return Response(
        json.dumps(data, default=str),
        status_code=status,
        media_type="application/json",
    )
