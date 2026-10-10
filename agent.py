import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import requests
from dotenv import load_dotenv
from openai import OpenAI, OpenAIError
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError


ROOT = Path(__file__).resolve().parent
LOG_FIELDS = ["actor", "message", "tool_call", "timestamp"]
SYSTEM_PROMPT = (
    "You manage inventory exclusively through the provided tools. Reply in the user's "
    "language. Retrieve inventory to resolve product names to real IDs before updates; "
    "never invent IDs, quantities, or successful actions. For stock updates, ask for "
    "clarification if the product is missing or ambiguous. For requested product "
    "creation, a name and numeric count are sufficient: use unit 'unidades' when "
    "no measurement unit is specified. Do not ask for units for bare counts, even "
    "when the user lists several products informally. Preserve explicit units "
    "such as kg or litros; never infer weight, volume, or package capacity. Ask "
    "only when the product name, quantity, or intended action is unclear. Create "
    "every requested product in a batch and report the actual results, including "
    "units. Do not create products unless the user requests it. Receipts use "
    "positive deltas; sales use "
    "negative deltas. Use alerts for low-stock questions (default threshold 10). "
    "Execute all actions needed, inspect tool results, and report failures honestly."
)


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ListInventoryInput(ToolInput):
    pass


class CreateProductInput(ToolInput):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    quantity: int = Field(ge=0, strict=True)
    unit: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)] = Field(
        default="unidades", description="Use the explicit unit, or unidades for a bare count."
    )


class UpdateStockInput(ToolInput):
    product_id: int = Field(ge=1, strict=True)
    delta: int = Field(strict=True)


class InventoryAlertsInput(ToolInput):
    threshold: int = Field(default=10, ge=0, strict=True)


TOOL_INPUTS = {
    "list_inventory": ListInventoryInput,
    "create_product": CreateProductInput,
    "update_stock": UpdateStockInput,
    "inventory_alerts": InventoryAlertsInput,
}
TOOL_DESCRIPTIONS = {
    "list_inventory": "GET /inventory: list all products with IDs, quantities and units.",
    "create_product": "POST /inventory: create a product with name, quantity and unit.",
    "update_stock": "PATCH /inventory/{product_id}: apply a signed integer stock delta.",
    "inventory_alerts": "GET /inventory/alerts: list products strictly below threshold (10 by default).",
}
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": name,
            "description": TOOL_DESCRIPTIONS[name],
            "parameters": input_model.model_json_schema(),
        },
    }
    for name, input_model in TOOL_INPUTS.items()
]


class ConversationLog:
    def __init__(self, path: Path):
        self.path = path

    def append(self, actor: str, message: str, tool_call: str = "") -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        needs_header = not self.path.exists() or self.path.stat().st_size == 0
        with self.path.open("a", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=LOG_FIELDS)
            if needs_header:
                writer.writeheader()
            writer.writerow({
                "actor": actor,
                "message": message,
                "tool_call": tool_call,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })


class InventoryAgent:
    def __init__(
        self, client: OpenAI, model: str, api_url: str,
        log_path: Path = ROOT / "conversation_log.csv",
    ):
        self.client = client
        self.model = model
        self.api_url = api_url.rstrip("/")
        self.log = ConversationLog(log_path)
        self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    def execute_tool(self, name: str, raw_arguments: str) -> dict:
        if name not in TOOL_INPUTS:
            return {"error": f"Unknown tool: {name}"}
        try:
            arguments = TOOL_INPUTS[name].model_validate_json(raw_arguments).model_dump()
        except ValidationError as error:
            return {"error": "Invalid tool arguments", "detail": str(error)}
        try:
            if name == "list_inventory":
                response = requests.get(f"{self.api_url}/inventory", timeout=15)
            elif name == "create_product":
                response = requests.post(
                    f"{self.api_url}/inventory", json=arguments, timeout=15
                )
            elif name == "update_stock":
                response = requests.patch(
                    f"{self.api_url}/inventory/{arguments['product_id']}",
                    json={"delta": arguments["delta"]}, timeout=15,
                )
            else:
                response = requests.get(
                    f"{self.api_url}/inventory/alerts", params=arguments, timeout=15
                )
        except requests.RequestException as error:
            return {
                "error": "Inventory API request failed; outcome may be unknown. "
                "Check inventory before retrying a stock mutation.",
                "detail": str(error),
            }
        try:
            body = response.json()
        except ValueError:
            body = {"detail": response.text}
        if not response.ok:
            return {"error": "Inventory API rejected the request", "status": response.status_code, "detail": body}
        return {"status": response.status_code, "data": body}

    def record_response(self, answer: str) -> str:
        self.messages.append({"role": "assistant", "content": answer})
        self.log.append("agent", answer)
        return answer

    def respond(self, user_message: str) -> str:
        self.messages.append({"role": "user", "content": user_message})
        self.log.append("user", user_message)
        while True:
            completion = self.client.chat.completions.create(
                model=self.model, messages=self.messages, tools=TOOLS, tool_choice="auto",
            )
            message = completion.choices[0].message
            if not message.tool_calls:
                return self.record_response(message.content or "El modelo no devolvio una respuesta.")
            self.messages.append({
                "role": "assistant",
                "content": message.content,
                "tool_calls": [tool_call.model_dump() for tool_call in message.tool_calls],
            })
            for tool_call in message.tool_calls:
                call_json = json.dumps(tool_call.model_dump(), ensure_ascii=False)
                self.log.append("agent", message.content or "", call_json)
                result = self.execute_tool(tool_call.function.name, tool_call.function.arguments)
                result_json = json.dumps(result, ensure_ascii=False)
                self.messages.append({
                    "role": "tool", "tool_call_id": tool_call.id, "content": result_json,
                })
                self.log.append("tool", result_json, call_json)


def main() -> None:
    load_dotenv(ROOT / ".env")
    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("Falta OPENAI_API_KEY. Configurala en .env antes de iniciar el agente.")
    agent = InventoryAgent(
        client=OpenAI(), model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        api_url=os.getenv("INVENTORY_API_URL", "http://127.0.0.1:8000"),
    )
    print("Agente de inventario. Escribe salir o exit para terminar.")
    while True:
        try:
            user_message = input("Tu: ").strip()
            if user_message.lower() in {"salir", "exit", "quit"}:
                break
            if not user_message:
                continue
            try:
                answer = agent.respond(user_message)
            except OpenAIError as error:
                answer = agent.record_response(f"Error del servicio LLM: {error}")
            print(f"Agente: {answer}")
        except (EOFError, KeyboardInterrupt):
            print("\nSesion finalizada.")
            break


if __name__ == "__main__":
    main()