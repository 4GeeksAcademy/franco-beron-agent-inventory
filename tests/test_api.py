import csv

import pytest
from fastapi.testclient import TestClient

from api import app as inventory_api


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(inventory_api, "PRODUCTS_FILE", tmp_path / "products.csv")
    with TestClient(inventory_api.app) as test_client:
        yield test_client


def test_create_list_and_persist_across_restarts(client):
    assert client.get("/inventory").json() == []
    response = client.post(
        "/inventory", json={"name": "oat milk", "quantity": 5, "unit": "units"}
    )
    assert response.status_code == 201
    product = response.json()
    assert product == {"id": 1, "name": "oat milk", "quantity": 5, "unit": "units"}
    assert client.get("/inventory").json() == [product]
    with inventory_api.PRODUCTS_FILE.open(newline="", encoding="utf-8") as stream:
        assert list(csv.DictReader(stream))[0]["quantity"] == "5"
    with TestClient(inventory_api.app) as restarted:
        assert restarted.get("/inventory").json() == [product]
        assert restarted.post(
            "/inventory", json={"name": "arabica", "quantity": 20, "unit": "bags"}
        ).json()["id"] == 2


def test_signed_updates_and_alert_threshold(client):
    client.post("/inventory", json={"name": "oat milk", "quantity": 5, "unit": "units"})
    client.post("/inventory", json={"name": "arabica", "quantity": 20, "unit": "bags"})
    client.post("/inventory", json={"name": "tea", "quantity": 10, "unit": "bags"})
    assert client.patch("/inventory/1", json={"delta": 30}).json()["quantity"] == 35
    assert client.patch("/inventory/2", json={"delta": -12}).json()["quantity"] == 8
    assert [product["name"] for product in client.get("/inventory/alerts").json()] == [
        "arabica"
    ]
    assert len(client.get("/inventory/alerts?threshold=11").json()) == 2
    assert client.get("/inventory/alerts?threshold=0").json() == []
    with TestClient(inventory_api.app) as restarted:
        assert restarted.get("/inventory").json()[1]["quantity"] == 8


def test_errors_do_not_change_stock(client):
    client.post("/inventory", json={"name": "oat milk", "quantity": 5, "unit": "units"})
    for endpoint, payload, expected_status in [
        ("/inventory/999", {"delta": 1}, 404),
        ("/inventory/1", {"delta": -6}, 400),
        ("/inventory/1", {"delta": "2"}, 422),
        ("/inventory/1", {"delta": 1.5}, 422),
        ("/inventory/1", {}, 422),
    ]:
        response = client.patch(endpoint, json=payload)
        assert response.status_code == expected_status
        assert response.json()["detail"]
    assert client.get("/inventory").json()[0]["quantity"] == 5
    assert client.get("/inventory/alerts?threshold=-1").status_code == 422


@pytest.mark.parametrize("payload", [
    {"name": "milk", "unit": "units"},
    {"name": "milk", "quantity": -1, "unit": "units"},
    {"name": " ", "quantity": 1, "unit": "units"},
    {"name": "milk", "quantity": 1, "unit": ""},
    {"name": "milk", "quantity": True, "unit": "units"},
])
def test_required_product_fields(client, payload):
    assert client.post("/inventory", json=payload).status_code == 422
    assert client.get("/inventory").json() == []