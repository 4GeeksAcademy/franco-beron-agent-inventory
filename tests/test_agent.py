import copy
import csv
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
from openai.types.chat import ChatCompletionMessage, ChatCompletionMessageFunctionToolCall

import agent as agent_module
from agent import InventoryAgent, LOG_FIELDS, TOOLS


def tool_call(call_id, name, arguments):
    return ChatCompletionMessageFunctionToolCall(
        id=call_id, type="function", function={"name": name, "arguments": json.dumps(arguments)}
    )


def completion(content=None, calls=None):
    return SimpleNamespace(choices=[SimpleNamespace(message=ChatCompletionMessage(
        role="assistant", content=content, tool_calls=calls,
    ))])


def make_agent(tmp_path, completions):
    client = Mock()
    captured = []
    pending = iter(completions)

    def create(**kwargs):
        captured.append(copy.deepcopy(kwargs))
        return next(pending)

    client.chat.completions.create.side_effect = create
    return InventoryAgent(client, "test-model", "http://inventory", tmp_path / "log.csv"), captured


def test_manual_loop_multiple_calls_history_and_append_only_log(tmp_path, monkeypatch):
    agent, captured = make_agent(tmp_path, [
        completion(calls=[tool_call("lookup", "list_inventory", {})]),
        completion(calls=[
            tool_call("receipt", "update_stock", {"product_id": 1, "delta": 30}),
            tool_call("sale", "update_stock", {"product_id": 2, "delta": -12}),
        ]),
        completion("Stock actualizado."),
        completion(calls=[tool_call("alerts", "inventory_alerts", {})]),
        completion("Arabica: 8 bags."),
    ])
    get_mock = Mock(side_effect=[
        Mock(ok=True, status_code=200, json=Mock(return_value=[
            {"id": 1, "name": "oat milk", "quantity": 5, "unit": "units"},
            {"id": 2, "name": "arabica", "quantity": 20, "unit": "bags"},
        ])),
        Mock(ok=True, status_code=200, json=Mock(return_value=[
            {"id": 2, "name": "arabica", "quantity": 8, "unit": "bags"},
        ])),
    ])
    patch_mock = Mock(side_effect=[
        Mock(ok=True, status_code=200, json=Mock(return_value={"id": 1, "quantity": 35})),
        Mock(ok=True, status_code=200, json=Mock(return_value={"id": 2, "quantity": 8})),
    ])
    monkeypatch.setattr("agent.requests.get", get_mock)
    monkeypatch.setattr("agent.requests.patch", patch_mock)
    assert agent.respond("we received 30 units of oat milk and sold 12 bags of arabica") == "Stock actualizado."
    assert agent.respond("what products are running low?") == "Arabica: 8 bags."
    patch_mock.assert_any_call("http://inventory/inventory/1", json={"delta": 30}, timeout=15)
    patch_mock.assert_any_call("http://inventory/inventory/2", json={"delta": -12}, timeout=15)
    get_mock.assert_any_call("http://inventory/inventory/alerts", params={"threshold": 10}, timeout=15)
    assert captured[0]["tools"] == TOOLS
    assert captured[1]["messages"][-1]["tool_call_id"] == "lookup"
    assert [message["tool_call_id"] for message in captured[2]["messages"][-2:]] == ["receipt", "sale"]
    assert json.loads(captured[2]["messages"][-1]["content"])["data"]["quantity"] == 8
    assert any(message.get("content") == "Stock actualizado." for message in captured[3]["messages"])
    log_path = tmp_path / "log.csv"
    original = log_path.read_bytes()
    second_agent, _ = make_agent(tmp_path, [completion("Nueva sesion.")])
    second_agent.respond("hola")
    assert log_path.read_bytes().startswith(original)
    with log_path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        assert reader.fieldnames == LOG_FIELDS
        rows = list(reader)
    assert len(rows) == 14
    assert {row["actor"] for row in rows} == {"user", "agent", "tool"}
    assert len([row for row in rows if row["actor"] == "tool"]) == 4
    for row in rows:
        assert datetime.fromisoformat(row["timestamp"]).tzinfo is not None
        if row["actor"] == "tool":
            assert json.loads(row["tool_call"])["id"]
        if row["actor"] == "user":
            assert row["tool_call"] == ""


def test_all_tool_schemas_are_typed():
    schemas = {tool["function"]["name"]: tool["function"]["parameters"] for tool in TOOLS}
    assert set(schemas) == {"list_inventory", "create_product", "update_stock", "inventory_alerts"}
    assert schemas["create_product"]["required"] == ["name", "quantity"]
    assert schemas["create_product"]["properties"]["unit"]["default"] == "unidades"
    assert schemas["update_stock"]["properties"]["delta"]["type"] == "integer"
    assert schemas["inventory_alerts"]["properties"]["threshold"]["default"] == 10
    assert all(schema["additionalProperties"] is False for schema in schemas.values())


def test_creation_and_http_errors(tmp_path, monkeypatch):
    agent, _ = make_agent(tmp_path, [])
    post_mock = Mock(return_value=Mock(ok=True, status_code=201, json=Mock(return_value={"id": 1})))
    monkeypatch.setattr("agent.requests.post", post_mock)
    assert agent.execute_tool("create_product", '{"name":"tea","quantity":2,"unit":"bags"}')["status"] == 201
    post_mock.assert_called_once_with(
        "http://inventory/inventory", json={"name": "tea", "quantity": 2, "unit": "bags"}, timeout=15
    )
    monkeypatch.setattr("agent.requests.patch", Mock(return_value=Mock(
        ok=False, status_code=404, json=Mock(return_value={"detail": "Product 99 not found."}),
    )))
    error = agent.execute_tool("update_stock", '{"product_id":99,"delta":1}')
    assert error["status"] == 404
    assert "not found" in error["detail"]["detail"]


def test_invalid_calls_and_connection_failure_are_returned_to_model(tmp_path, monkeypatch):
    agent, captured = make_agent(tmp_path, [
        completion(calls=[tool_call("failed", "list_inventory", {})]),
        completion("No se pudo conectar con el inventario."),
    ])
    assert "error" in agent.execute_tool("unknown", "{}")
    assert "error" in agent.execute_tool("update_stock", "not json")
    assert "error" in agent.execute_tool("update_stock", '{"product_id":1,"delta":"30"}')
    assert "error" in agent.execute_tool("inventory_alerts", '{"threshold":-1}')
    monkeypatch.setattr("agent.requests.get", Mock(side_effect=requests.ConnectionError("offline")))
    assert agent.respond("list inventory") == "No se pudo conectar con el inventario."
    result = json.loads(captured[1]["messages"][-1]["content"])
    assert "error" in result
    assert result["detail"] == "offline"


def test_batch_creation_defaults_bare_counts_to_units(tmp_path, monkeypatch):
    products = [
        {"name": "techos", "quantity": 2},
        {"name": "garrafas", "quantity": 10},
        {"name": "caramelos de dulce de leche", "quantity": 15},
    ]
    calls = [
        tool_call(f"create-{index}", "create_product", product)
        for index, product in enumerate(products, start=1)
    ]
    agent, captured = make_agent(tmp_path, [
        completion(calls=calls), completion("Creados los tres productos en unidades."),
    ])
    post_mock = Mock(side_effect=[
        Mock(ok=True, status_code=201, json=Mock(return_value={
            "id": index, **product, "unit": "unidades",
        }))
        for index, product in enumerate(products, start=1)
    ])
    monkeypatch.setattr("agent.requests.post", post_mock)
    assert agent.respond(
        "podrias agregar porfa techos 2 cantidades, garrafas 10, y 15 caramelos de dulce de leche"
    ) == "Creados los tres productos en unidades."
    assert post_mock.call_count == 3
    for product in products:
        post_mock.assert_any_call(
            "http://inventory/inventory", json={**product, "unit": "unidades"}, timeout=15
        )
    assert "Do not ask for units for bare counts" in captured[0]["messages"][0]["content"]
    assert len([message for message in captured[1]["messages"] if message["role"] == "tool"]) == 3


@pytest.mark.parametrize("base_url,model", [
    ("https://api.openai.com/v1", "test-openai-model"),
    ("https://api.groq.com/openai/v1", "test-groq-model"),
])
def test_startup_uses_configured_provider_and_model(monkeypatch, base_url, model):
    monkeypatch.setattr(agent_module, "load_dotenv", Mock())
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-a-real-secret")
    monkeypatch.setenv("OPENAI_BASE_URL", base_url)
    monkeypatch.setenv("OPENAI_MODEL", model)
    monkeypatch.setenv("INVENTORY_API_URL", "http://inventory")
    agent_factory = Mock()
    monkeypatch.setattr(agent_module, "InventoryAgent", agent_factory)
    monkeypatch.setattr("builtins.input", lambda prompt: "salir")
    agent_module.main()
    configuration = agent_factory.call_args.kwargs
    assert str(configuration["client"].base_url) == f"{base_url}/"
    assert configuration["model"] == model
    assert configuration["api_url"] == "http://inventory"
    configuration["client"].close()