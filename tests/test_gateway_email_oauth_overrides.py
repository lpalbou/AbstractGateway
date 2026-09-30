"""OAuth endpoint and scope overrides (framework backlog 0992, gate review G1).

Endpoints and scopes come from the provider preset. Only an administrator, and only for
provider `custom` (which brings its own client), may give them; otherwise any user could make
the gateway POST the admin's OAuth client secret to a host of their choosing. A gateway-held
client (the admin's or the built-in one) is only ever used against the preset's endpoints.

`OAuthTokenClient` is replaced by a recorder, so a refused request is proven to reach no
endpoint at all (without the guard the recorder sees the attacker's URL and the secret).
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from email_fixtures import *  # noqa: F401,F403 - fixtures
from email_fixtures import ADMIN, ALICE

pytestmark = pytest.mark.integration

ADMIN_SECRET = "admin-client-secret-G1"
EVIL = "https://10.0.0.5/x"


class _Device:
    def public(self) -> Dict[str, Any]:
        return {"user_code": "CODE-1234", "verification_uri": "https://microsoft.com/devicelogin", "expires_in": 900, "interval": 5}


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    calls: List[Dict[str, Any]] = []

    class _Recorder:
        def __init__(self, oauth: Any, *, client_secret: str = "", verify: Any = None) -> None:
            self.oauth = oauth
            self.secret = client_secret

        def start_device_authorization(self) -> _Device:
            calls.append({"url": self.oauth.device_authorization_endpoint, "token_url": self.oauth.token_endpoint, "secret": self.secret})
            return _Device()

    import abstractgateway.mail.accounts as accounts

    monkeypatch.setattr(accounts, "OAuthTokenClient", _Recorder)
    return calls


@pytest.fixture
def admin_ms_client(gateway) -> None:
    r = gateway["client"].put(
        "/api/gateway/admin/email/oauth-clients/microsoft",
        headers=ADMIN,
        json={"client_id": "admin-cid", "client_secret": ADMIN_SECRET, "tenant": "common"},
    )
    assert r.status_code == 200, r.text


def _start(gateway, headers, **extra) -> Any:
    body = {"address": ALICE, "provider": "microsoft", "flow": "device", **extra}
    return gateway["client"].post("/api/gateway/me/email/oauth/start", headers=headers, json=body)


def _refused(r, *fields: str) -> None:
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert detail["reason_code"] == "email_oauth_override_refused"
    assert detail["cause"] and detail["fix"]
    for f in fields:
        assert f in detail["cause"]


def test_user_cannot_point_microsoft_device_flow_at_an_internal_host(gateway, admin_ms_client, recorder) -> None:
    # The exact gate scenario: a plain user, provider microsoft, the admin's client, a device
    # endpoint on an internal address. Refused before any request; the secret goes nowhere.
    r = _start(gateway, gateway["alice"], device_authorization_endpoint=EVIL)
    _refused(r, "device_authorization_endpoint")
    assert recorder == []
    assert ADMIN_SECRET not in r.text


@pytest.mark.parametrize(
    "field,value",
    [
        ("token_endpoint", EVIL),
        ("authorization_endpoint", EVIL),
        ("device_authorization_endpoint", EVIL),
        ("scopes", ["https://graph.microsoft.com/.default"]),
    ],
)
def test_every_override_is_refused_for_a_user(gateway, admin_ms_client, recorder, field, value) -> None:
    _refused(_start(gateway, gateway["alice"], **{field: value}), field)
    assert recorder == []


@pytest.mark.parametrize("field", ["token_endpoint", "device_authorization_endpoint", "scopes"])
def test_admin_cannot_override_a_known_providers_endpoints_either(gateway, admin_ms_client, recorder, field) -> None:
    value: Any = ["x"] if field == "scopes" else EVIL
    _refused(_start(gateway, ADMIN, **{field: value}), field)
    assert recorder == []


def test_tenant_cannot_reshape_the_preset_endpoint(gateway, admin_ms_client, recorder) -> None:
    _refused(_start(gateway, gateway["alice"], tenant="common/../../x?"))
    assert recorder == []


def test_custom_provider_is_refused_for_a_user_with_the_typed_code(gateway, recorder) -> None:
    r = _start(gateway, gateway["alice"], provider="custom", client_id="c", token_endpoint=EVIL, scopes=["mail"])
    _refused(r)
    assert recorder == []


def test_without_overrides_the_admin_client_goes_to_the_preset_only(gateway, admin_ms_client, recorder) -> None:
    r = _start(gateway, gateway["alice"])
    assert r.status_code == 200, r.text
    assert ADMIN_SECRET not in r.text
    assert recorder == [
        {
            "url": "https://login.microsoftonline.com/common/oauth2/v2.0/devicecode",
            "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
            "secret": ADMIN_SECRET,
        }
    ]


def test_gateway_held_secret_never_meets_a_foreign_endpoint() -> None:
    # The second line of defence, on its own: a gateway-held client with endpoints that are
    # not the preset's is refused; the caller's own client is the caller's business.
    from abstractgateway.mail.accounts import _gateway_secret_stays_with_preset
    from abstractgateway.mail.core_mail import EmailError, OAuthSettings, provider_preset

    preset = provider_preset("microsoft", tenant="common")
    foreign = OAuthSettings.build("microsoft", "cid", device_authorization_endpoint=EVIL, tenant="common")
    own_preset = OAuthSettings.build("microsoft", "cid", tenant="common")
    with pytest.raises(EmailError) as exc:
        _gateway_secret_stays_with_preset({"held": "gateway"}, foreign, preset)
    assert exc.value.code == "email_oauth_override_refused"
    _gateway_secret_stays_with_preset({"held": "gateway"}, own_preset, preset)
    _gateway_secret_stays_with_preset({"held": "caller"}, foreign, preset)
