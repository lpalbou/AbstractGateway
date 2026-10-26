"""`abstractgateway push-relay` (framework backlog 1049) against a fake APNs (httpx.MockTransport):
strict request schema, the fixed APNs message and headers, the ES256 provider token and its reuse,
the status mapping, the per-token and per-IP limits, hashed tokens in the logs, /v1/info."""

from __future__ import annotations

import base64
import json
import logging
from typing import Any, Dict, List, Tuple

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from fastapi.testclient import TestClient

from abstractgateway.push.relay import (
    RelayConfig,
    SlidingLimiter,
    apns_message,
    build_relay_app,
    load_config,
    parse_push_request,
    RelayRequestError,
    token_tag,
)

TOPIC = "ai.abstractframework.assistant.watchkitapp"
TOKEN = "c" * 64
T0 = 1_800_000_000.0


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> float:
        return self.t


class FakeApns:
    def __init__(self) -> None:
        self.requests: List[httpx.Request] = []
        self.replies: List[Tuple[int, Dict[str, Any]]] = []
        self.raise_network = False

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if self.raise_network:
            raise httpx.ConnectTimeout("timed out", request=request)
        self.requests.append(request)
        code, payload = self.replies.pop(0) if self.replies else (200, {})
        return httpx.Response(code, json=payload) if payload else httpx.Response(code)


@pytest.fixture()
def key():
    return ec.generate_private_key(ec.SECP256R1())


@pytest.fixture()
def relay(key):
    clock, apns = Clock(), FakeApns()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    app = build_relay_app(RelayConfig(key_pem=pem, key_id="KEYID12345", team_id="TEAMID1234", topics=(TOPIC,), version="9.9"),
                          apns_client=httpx.AsyncClient(transport=httpx.MockTransport(apns.handler)), clock=clock)
    with TestClient(app) as client:
        yield client, apns, clock, app


def _req(**over) -> Dict[str, Any]:
    out = {"topic": TOPIC, "env": "production", "token": TOKEN, "kind": "approval", "collapse": "ab" * 16, "seq": 7, "reg": "0123456789abcdef"}
    out.update(over)
    return out


def _post(client: TestClient, body: Any, **kw) -> httpx.Response:
    raw = body if isinstance(body, (bytes, str)) else json.dumps(body)
    return client.post("/v1/push", content=raw, headers={"content-type": "application/json"}, **kw)


def test_schema_rejections(relay) -> None:
    client, apns, _clock, _app = relay
    bad = [
        {**_req(), "title": "Your account is locked"},
        {k: v for k, v in _req().items() if k != "reg"},
        _req(topic="com.example.other"),
        _req(env="dev"),
        _req(token="C" * 64),
        _req(token="c" * 63),
        _req(kind="custom"),
        _req(collapse="zz"),
        _req(seq=True),
        _req(seq=-1),
        _req(seq="7"),
        _req(reg="0123"),
        [1, 2],
    ]
    for body in bad:
        r = _post(client, body)
        assert r.status_code == 400 and r.json()["status"] == "invalid", (body, r.text)
    assert _post(client, '{"topic": "a", "topic": "b"}').status_code == 400
    assert _post(client, '{"seq": NaN}').status_code == 400
    assert _post(client, b"\xff\xfe").status_code == 400
    big = json.dumps({**_req(), "pad": "x" * 1200})
    assert _post(client, big).status_code == 413
    assert apns.requests == []
    with pytest.raises(RelayRequestError):
        parse_push_request(json.dumps(_req()).encode(), topics=())


def test_template_headers_and_body(relay, key) -> None:
    client, apns, clock, _app = relay
    assert _post(client, _req()).json() == {"status": "sent"}
    assert _post(client, _req(kind="result", env="sandbox", seq=8)).json() == {"status": "sent"}
    first, second = apns.requests
    assert str(first.url) == f"https://api.push.apple.com/3/device/{TOKEN}"
    assert str(second.url) == f"https://api.sandbox.push.apple.com/3/device/{TOKEN}"
    h = first.headers
    assert (h["apns-push-type"], h["apns-topic"], h["apns-priority"], h["apns-collapse-id"], h["apns-expiration"]) == (
        "alert", TOPIC, "10", "ab" * 16, str(int(T0) + 86400))
    assert second.headers["apns-priority"] == "5"
    assert json.loads(first.content) == {
        "aps": {"alert": {"title-loc-key": "push.approval.title", "loc-key": "push.approval.body"}, "sound": "default",
                "mutable-content": 1, "interruption-level": "time-sensitive", "thread-id": "approval"},
        "s": 7, "r": "0123456789abcdef",
    }
    assert json.loads(second.content)["aps"]["interruption-level"] == "active"
    for kind in ("question", "notify", "failure"):
        _h, body = apns_message(_req(kind=kind), now=T0)
        assert body["aps"]["alert"] == {"title-loc-key": f"push.{kind}.title", "loc-key": f"push.{kind}.body"}
    # The provider token: ES256 over header.claims, kid + iss + iat, verifiable with the key.
    jwt = h["authorization"].removeprefix("bearer ")
    head, claims, sig = jwt.split(".")
    pad = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))  # noqa: E731
    assert json.loads(pad(head)) == {"alg": "ES256", "kid": "KEYID12345"}
    assert json.loads(pad(claims)) == {"iss": "TEAMID1234", "iat": int(T0)}
    raw = pad(sig)
    key.public_key().verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
                            f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))


def test_jwt_is_reused_for_at_most_forty_minutes(relay) -> None:
    client, apns, clock, app = relay
    for i in range(5):
        _post(client, _req(seq=i))
        clock.t += 7 * 60  # 35 minutes in total
    assert app.state.provider_token.signed == 1
    assert len({r.headers["authorization"] for r in apns.requests}) == 1
    clock.t = T0 + 40 * 60
    _post(client, _req(seq=99))
    assert app.state.provider_token.signed == 2
    # An expired provider token is re-signed and the request retried once.
    apns.replies = [(403, {"reason": "ExpiredProviderToken"}), (200, {})]
    assert _post(client, _req(seq=100)).json() == {"status": "sent"}
    assert app.state.provider_token.signed == 3


def test_status_mapping(relay) -> None:
    client, apns, clock, _app = relay
    cases = [
        ((410, {"reason": "Unregistered", "timestamp": 1_799_000_000_000}), 200, {"status": "gone", "since_ms": 1_799_000_000_000}),
        ((400, {"reason": "BadDeviceToken"}), 200, {"status": "gone", "since_ms": int(T0 * 1000)}),
        ((400, {"reason": "DeviceTokenNotForTopic"}), 200, {"status": "gone", "since_ms": int(T0 * 1000)}),
        ((400, {"reason": "PayloadTooLarge"}), 200, {"status": "rejected", "reason": "PayloadTooLarge"}),
        ((413, {}), 200, {"status": "rejected", "reason": "APNs 413"}),
        ((429, {"reason": "TooManyRequests"}), 429, {"status": "retry", "after_s": 60}),
        ((500, {"reason": "InternalServerError"}), 503, {"status": "retry", "after_s": 30}),
        ((503, {"reason": "ServiceUnavailable"}), 503, {"status": "retry", "after_s": 30}),
    ]
    for i, (reply, code, payload) in enumerate(cases):
        apns.replies = [reply]
        r = _post(client, _req(seq=i))
        assert (r.status_code, r.json()) == (code, payload), reply
    apns.raise_network = True
    r = _post(client, _req(seq=50))
    assert (r.status_code, r.json()) == (503, {"status": "retry", "after_s": 30})


def test_per_token_limits_and_hashed_logs(relay, caplog: pytest.LogCaptureFixture) -> None:
    client, apns, clock, _app = relay
    with caplog.at_level(logging.INFO, logger="abstractgateway.push.relay"):
        for i in range(30):
            assert _post(client, _req(seq=i)).json() == {"status": "sent"}
        limited = _post(client, _req(seq=31))
        assert limited.status_code == 429 and limited.json()["status"] == "retry" and limited.json()["after_s"] >= 1
        # Another token is not affected; the hour passing frees this one.
        assert _post(client, _req(token="d" * 64)).json() == {"status": "sent"}
        clock.t += 3601
        assert _post(client, _req(seq=32)).json() == {"status": "sent"}
    assert len(apns.requests) == 32
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert TOKEN not in text and token_tag(TOKEN) in text


def test_day_and_ip_limits() -> None:
    clock = Clock()
    per_token = SlidingLimiter(((3600.0, 30), (86400.0, 300)), clock=clock)
    allowed = 0
    for _ in range(24 * 40):
        if per_token.hit("t") is None:
            allowed += 1
        clock.t += 90  # 40 tries an hour, over a day
    assert allowed <= 300 + 30
    per_ip = SlidingLimiter(((3600.0, 600),), clock=clock)
    assert all(per_ip.hit("1.2.3.4") is None for _ in range(600))
    assert per_ip.hit("1.2.3.4") is not None and per_ip.hit("5.6.7.8") is None


def test_info_and_config(relay, tmp_path) -> None:
    client, _apns, _clock, _app = relay
    assert client.get("/v1/info").json() == {"topics": [TOPIC], "version": "9.9"}
    import argparse

    key = tmp_path / "AuthKey.p8"
    key.write_bytes(ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    ns = argparse.Namespace(apns_key_file=str(key), key_id="KEYID12345", team_id="TEAMID1234", topic=[TOPIC, TOPIC])
    assert load_config(ns).topics == (TOPIC,)
    with pytest.raises(ValueError):
        load_config(argparse.Namespace(apns_key_file=str(key), key_id="short", team_id="TEAMID1234", topic=[TOPIC]))


def test_the_cli_parses_and_refuses_without_http2(monkeypatch, tmp_path) -> None:
    from abstractgateway.cli import main
    from abstractgateway.push import relay as relay_mod

    def _no_h2():
        raise RuntimeError("the push relay talks to Apple over HTTP/2, which needs the `h2` package")

    monkeypatch.setattr(relay_mod, "build_apns_client", _no_h2)
    key = tmp_path / "AuthKey.p8"
    key.write_bytes(ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    with pytest.raises(SystemExit) as exc:
        main(["push-relay", "--apns-key-file", str(key), "--key-id", "KEYID12345", "--team-id", "TEAMID1234", "--topic", TOPIC])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        main(["serve", "--push-relay", "ftp://x", "--help"])
    assert exc.value.code == 2
