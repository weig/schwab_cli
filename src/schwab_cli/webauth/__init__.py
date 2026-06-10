"""webauth — the REST API's OAuth2 resource-server layer.

Users configure one or more external authorization servers (Auth0,
Google, any OIDC/OAuth2 provider) under ``~/.config/schwab_cli/webauth/``
(one JSON file per provider). A request bearing a JWT signed by any
configured provider — and whose subject passes that provider's
allowlist — gets a :class:`~schwab_cli.webauth.verify.Principal` with
the union of the token's scopes and any statically granted ones.

The daemon itself never talks to the authorization servers except to
fetch their public JWKS for local signature verification.
"""

from schwab_cli.webauth.config import (
    LoadedProviders,
    ProviderConfig,
    ProviderError,
    load_providers,
)
from schwab_cli.webauth.scopes import scope_satisfied
from schwab_cli.webauth.verify import (
    InvalidToken,
    JwksKeyResolver,
    Principal,
    SubjectNotAllowed,
    TokenVerifier,
    UnknownIssuer,
    WebAuthError,
)

__all__ = [
    "LoadedProviders",
    "ProviderConfig",
    "ProviderError",
    "load_providers",
    "scope_satisfied",
    "WebAuthError",
    "InvalidToken",
    "UnknownIssuer",
    "SubjectNotAllowed",
    "Principal",
    "TokenVerifier",
    "JwksKeyResolver",
]
