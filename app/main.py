import logging
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from opentelemetry import metrics, trace
from opentelemetry.trace import SpanKind, StatusCode
from pydantic import BaseModel, Field

from app.telemetry import LOGGER_NAME, setup_telemetry


DB_PATH = Path(os.getenv("ORDER_DB_PATH", "data/orders.db"))
STATUSES = {"received", "preparing", "shipped", "delivered"}
LOOKUP_ROUTE = "/api/orders/{order_id}"

setup_telemetry()
logger = logging.getLogger(LOGGER_NAME)
tracer = trace.get_tracer(LOGGER_NAME)
request_duration = metrics.get_meter(LOGGER_NAME).create_histogram(
    "http.server.request.duration",
    unit="s",
    description="Duration of order lookup requests",
)


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    with connect() as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                customer TEXT NOT NULL,
                item TEXT NOT NULL,
                priority TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        if db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0:
            now = datetime.now(timezone.utc)
            previous_month_end = now.replace(day=1) - timedelta(days=1)
            for order in (
                ("standard-1001", "Avery", "Notebook", "standard", "received", now),
                ("express-1002", "Sam", "Headphones", "express", "preparing", previous_month_end),
                ("standard-1003", "Riley", "Water bottle", "standard", "shipped", now),
            ):
                db.execute(
                    "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)",
                    (*order[:5], order[5].isoformat()),
                )


def as_dict(row):
    return dict(row) if row else None


def order_detail(row):
    order = as_dict(row)
    if order["priority"] == "express":
        placed_at = datetime.fromisoformat(order["created_at"])
        estimated_at = placed_at + timedelta(days=2)
        order["estimated_delivery"] = estimated_at.date().isoformat()
    return order


class NewOrder(BaseModel):
    customer: str = Field(min_length=1, max_length=80)
    item: str = Field(min_length=1, max_length=120)
    priority: str = "standard"


class StatusUpdate(BaseModel):
    status: str


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Order Tracker", lifespan=lifespan)


@app.middleware("http")
async def observe_order_lookups(request: Request, call_next):
    if request.method != "GET" or not request.url.path.startswith("/api/orders/"):
        return await call_next(request)

    order_id = request.url.path.removeprefix("/api/orders/")
    attributes = {"http.request.method": "GET", "http.route": LOOKUP_ROUTE}
    started = time.perf_counter()
    status = 500
    with tracer.start_as_current_span(f"GET {LOOKUP_ROUTE}", kind=SpanKind.SERVER) as span:
        span.set_attributes({**attributes, "url.path": request.url.path, "order.id": order_id})
        try:
            response = await call_next(request)
            status = response.status_code
        except Exception:
            logger.exception(
                "Order lookup failed",
                extra={**attributes, "http.response.status_code": status, "order.id": order_id},
            )
            raise
        else:
            logger.log(
                logging.ERROR if status >= 500 else logging.INFO,
                "Order lookup returned %s",
                status,
                extra={**attributes, "http.response.status_code": status, "order.id": order_id},
            )
            return response
        finally:
            span.set_attribute("http.response.status_code", status)
            if status >= 500:
                span.set_status(StatusCode.ERROR)
            request_duration.record(
                time.perf_counter() - started,
                {**attributes, "http.response.status_code": status},
            )


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent.parent / "static" / "index.html")


@app.get("/healthz")
def health():
    with connect() as db:
        db.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/api/orders")
def list_orders():
    with connect() as db:
        rows = db.execute("SELECT * FROM orders ORDER BY created_at DESC").fetchall()
    return [as_dict(row) for row in rows]


@app.get("/api/orders/{order_id}")
def get_order(order_id: str):
    with connect() as db:
        row = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Order not found")
    return order_detail(row)


@app.post("/api/orders", status_code=201)
def create_order(order: NewOrder):
    if order.priority not in {"standard", "express"}:
        raise HTTPException(422, "Priority must be standard or express")
    order_id = str(uuid4())
    with connect() as db:
        db.execute(
            "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)",
            (order_id, order.customer, order.item, order.priority, "received",
             datetime.now(timezone.utc).isoformat()),
        )
    return get_order(order_id)


@app.patch("/api/orders/{order_id}")
def update_status(order_id: str, update: StatusUpdate):
    if update.status not in STATUSES:
        raise HTTPException(422, "Invalid status")
    with connect() as db:
        cursor = db.execute(
            "UPDATE orders SET status = ? WHERE id = ?",
            (update.status, order_id),
        )
    if cursor.rowcount == 0:
        raise HTTPException(404, "Order not found")
    return get_order(order_id)
