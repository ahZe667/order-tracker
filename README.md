# Order Tracker

A small order tracking app for the AI Dev Tools Zoomcamp observability homework. It includes a web page, API, tests, and a Docker Compose setup. You add telemetry, alerts, and an incident responder in Homework 4.

The main user flow is creating an order and checking its status. Three sample orders are created on first startup.

## Run it

You need Docker with Compose. To run the tests, you also need Python 3.11+ and `uv`.

```bash
docker compose up --build -d --wait
```

Open <http://127.0.0.1:8000>. The API is at `/api/orders`, and the health check is at `/healthz`. Data is stored in a Docker volume and survives container recreation.

If port 8000 is occupied, set `ORDER_TRACKER_PORT`, for example:

```bash
ORDER_TRACKER_PORT=18080 docker compose up --build -d --wait
```

Run tests with `uv run --frozen pytest -q`. Stop the app with `docker compose down`. Add `-v` only if you also want to delete the order data.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | Web page |
| GET | `/healthz` | Database health check |
| GET | `/api/orders` | List orders |
| POST | `/api/orders` | Create an order |
| GET | `/api/orders/{id}` | Check an order |
| PATCH | `/api/orders/{id}` | Change an order status |

The app uses SQLite to keep setup small. Run one app container at a time. The course exercise is about detecting and handling an incident, not scaling the database.

## Observability

`docker compose up --build -d --wait` also starts the telemetry stack. The app sends OpenTelemetry metrics, logs, and traces for order lookups (`GET /api/orders/{id}`) over OTLP to the Collector, which forwards them to Prometheus, Loki, and Tempo. Configuration lives in `observability/`.

| Service | URL |
| --- | --- |
| Grafana (anonymous viewer) | <http://localhost:3000/d/order-tracker> |
| Prometheus | <http://localhost:9090> |
| Loki | <http://localhost:3100> |
| Tempo | <http://localhost:3200> |

The request metric is `http_server_request_duration_seconds` with `http_route` and `http_response_status_code` labels. Logs carry `trace_id`, so Grafana links each log line to its trace. Set `ORDER_TRACKER_TELEMETRY=console` instead of `otlp` to print the signals to `docker compose logs app`.
