import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app())


def test_healthz_returns_ok(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_security_headers_present(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert "'unsafe-inline'" not in response.headers["content-security-policy"]


def test_csp_allows_the_bare_openstreetmap_tile_host(client: TestClient) -> None:
    """A wildcard does not match the bare host; the tile URL uses the bare host.

    Getting this wrong blocks every map tile while the page looks fine.
    """
    csp = client.get("/healthz").headers["content-security-policy"]
    assert "https://tile.openstreetmap.org" in csp
    img_src = [part for part in csp.split(";") if part.strip().startswith("img-src")][0]
    assert "https://tile.openstreetmap.org" in img_src.split()
