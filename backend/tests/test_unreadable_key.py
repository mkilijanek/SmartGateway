"""A stored upstream key that cannot be decrypted must not turn into "no key": the endpoint is never called without its credentials."""
import asyncio
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import Session, select

from app import crypto, db, discovery
from app.main import app
from app.models import Endpoint, GatewayModel, Member, Model
from app import proxy


@pytest.fixture(scope="module")
def client():
    db.init_db()
    return TestClient(app)


def broken_endpoint(name):
    with Session(db.engine) as s:
        ep = Endpoint(name=name, base_url="http://127.0.0.1:1/v1", status="online", api_key="sk-real")
        s.add(ep); s.commit(); s.refresh(ep)
        s.exec(text("UPDATE endpoint SET api_key = 'enc:not-a-valid-token' WHERE id = :i").bindparams(i=ep.id)); s.commit()
        return ep.id


def test_undecryptable_value_is_marked_not_dropped(client):
    ep_id = broken_endpoint("broken-1")
    with Session(db.engine) as s:
        ep = s.get(Endpoint, ep_id)
        assert crypto.is_unreadable(ep.api_key) and ep.api_key            # truthy marker, not None
    row = next(e for e in client.get("/api/endpoints").json() if e["id"] == ep_id)
    assert row["key_unreadable"] is True and row["api_key"] is True


def test_no_request_is_built_without_the_credentials():
    with pytest.raises(crypto.SecretUnreadable):
        discovery.headers_for(crypto.UNREADABLE)
    ok, err = asyncio.run(discovery.ping("http://127.0.0.1:1/v1", crypto.UNREADABLE))
    assert ok is False and "cannot be decrypted" in err
    assert discovery.headers_for(None) == {"Content-Type": "application/json"}   # a genuinely keyless endpoint still works


def test_unreadable_endpoint_is_taken_out_of_rotation(client):
    ep_id = broken_endpoint("broken-2")
    with Session(db.engine) as s:
        m = Model(endpoint_id=ep_id, upstream_id="m", capabilities=["chat"], max_slots=2); s.add(m); s.commit(); s.refresh(m)
        gm = GatewayModel(gateway_id=0, public_name=f"x{time.time_ns()}"); s.add(gm); s.commit(); s.refresh(gm)
        s.add(Member(gateway_model_id=gm.id, model_id=m.id)); s.commit()
        gm_id = gm.id
    assert proxy.load_members(gm_id) == []


def test_rescan_of_an_unreadable_endpoint_is_a_clear_409(client):
    ep_id = broken_endpoint("broken-3")
    r = client.post(f"/api/endpoints/{ep_id}/rescan")
    assert r.status_code == 409 and "cannot be decrypted" in r.json()["detail"]


def test_mcp_endpoint_with_unreadable_headers_is_never_called(client):
    from app.mcp import registry
    from app.mcp.client import McpError
    from app.models import McpEndpoint
    with Session(db.engine) as s:
        ep = McpEndpoint(name="mcp-broken", url="http://127.0.0.1:1/mcp", headers_enc='{"X-Api-Key": "k"}')
        s.add(ep); s.commit(); s.refresh(ep)
        s.exec(text("UPDATE mcpendpoint SET headers_enc = 'enc:not-a-valid-token' WHERE id = :i").bindparams(i=ep.id)); s.commit()
        s.refresh(ep)
        assert crypto.is_unreadable(ep.headers_enc)
        with pytest.raises(McpError, match="cannot be decrypted"):
            asyncio.run(registry.session_for(ep).request("tools/list"))
