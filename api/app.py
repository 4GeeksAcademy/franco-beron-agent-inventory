import csv
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from threading import RLock
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StringConstraints


PRODUCTS_FILE = Path(__file__).resolve().parents[1] / "products.csv"
CSV_FIELDS = ["id", "name", "quantity", "unit"]
storage_lock = RLock()
ProductText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ProductCreate(BaseModel):
    name: ProductText
    quantity: int = Field(ge=0, strict=True)
    unit: ProductText


class Product(ProductCreate):
    id: int


class StockUpdate(BaseModel):
    delta: int = Field(strict=True)


def write_products(products: list[Product]) -> None:
    PRODUCTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=PRODUCTS_FILE.parent,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            writer = csv.DictWriter(temporary_file, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(product.model_dump() for product in products)
        os.replace(temporary_path, PRODUCTS_FILE)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def read_products() -> list[Product]:
    if not PRODUCTS_FILE.exists():
        write_products([])
    with PRODUCTS_FILE.open(encoding="utf-8", newline="") as products_file:
        reader = csv.DictReader(products_file)
        if reader.fieldnames != CSV_FIELDS:
            raise HTTPException(status_code=500, detail="Invalid products CSV header.")
        try:
            return [
                Product(
                    id=int(row["id"]), name=row["name"],
                    quantity=int(row["quantity"]), unit=row["unit"],
                )
                for row in reader
            ]
        except (ValueError, TypeError, KeyError) as error:
            raise HTTPException(
                status_code=500, detail="Invalid product record in products CSV."
            ) from error


@asynccontextmanager
async def lifespan(app: FastAPI):
    with storage_lock:
        read_products()
    yield


app = FastAPI(title="Inventory API", lifespan=lifespan)


@app.get("/inventory", response_model=list[Product])
def inventory():
    with storage_lock:
        return read_products()


@app.post("/inventory", response_model=Product, status_code=201)
def create_product(product: ProductCreate):
    with storage_lock:
        products = read_products()
        product_id = max((record.id for record in products), default=0) + 1
        created = Product(id=product_id, **product.model_dump())
        products.append(created)
        write_products(products)
        return created


@app.get("/inventory/alerts", response_model=list[Product])
def inventory_alerts(threshold: int = Query(default=10, ge=0)):
    with storage_lock:
        return [product for product in read_products() if product.quantity < threshold]


@app.patch("/inventory/{product_id}", response_model=Product)
def update_stock(product_id: int, update: StockUpdate):
    with storage_lock:
        products = read_products()
        for product in products:
            if product.id == product_id:
                quantity = product.quantity + update.delta
                if quantity < 0:
                    raise HTTPException(
                        status_code=400, detail="Stock update would make quantity negative."
                    )
                product.quantity = quantity
                write_products(products)
                return product
        raise HTTPException(status_code=404, detail=f"Product {product_id} not found.")