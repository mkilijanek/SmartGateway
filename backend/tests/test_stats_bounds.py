import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.mark.parametrize("minutes,expected", [(0, 1), (-5, 1), (60, 60), (10 ** 9, 7 * 1440)])
def test_stats_minutes_is_bounded(client, minutes, expected):
    r = client.get("/api/stats", params={"minutes": minutes})
    assert r.status_code == 200, r.text   # minutes=0 used to raise ZeroDivisionError
    assert r.json()["minutes"] == expected
