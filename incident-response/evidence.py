"""Read-only evidence collection: GET requests to the local telemetry backends only."""

import json
import time
import urllib.parse
import urllib.request

PROMETHEUS = "http://localhost:9090"
LOKI = "http://localhost:3100"
TEMPO = "http://localhost:3200"
SERVICE = "order-tracker"
WINDOW_SECONDS = 15 * 60
MAX_LOGS = 20
MAX_TRACES = 5


def get_json(base, path, params=None):
    url = f"{base}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.load(response)


def request_counts():
    query = (
        "sum by (http_route, http_response_status_code) "
        f'(http_server_request_duration_seconds_count{{service_name="{SERVICE}"}})'
    )
    data = get_json(PROMETHEUS, "/api/v1/query", {"query": query})
    return [
        {**row["metric"], "requests": float(row["value"][1])}
        for row in data["data"]["result"]
    ]


def error_logs(route):
    query = f'{{service_name="{SERVICE}"}} | severity_text="ERROR"'
    if route:
        query += f" | http_route={json.dumps(route)}"
    now = time.time_ns()
    data = get_json(
        LOKI,
        "/loki/api/v1/query_range",
        {
            "query": query,
            "start": now - WINDOW_SECONDS * 1_000_000_000,
            "end": now,
            "limit": MAX_LOGS,
            "direction": "backward",
        },
    )
    logs = []
    for stream in data["data"]["result"]:
        for timestamp, line, *meta in stream["values"]:
            metadata = meta[0].get("structuredMetadata", {}) if meta else {}
            logs.append({"time_ns": timestamp, "message": line, **stream["stream"], **metadata})
    return sorted(logs, key=lambda log: log["time_ns"], reverse=True)[:MAX_LOGS]


def _value(any_value):
    return next(iter(any_value.values()), None)


def _attributes(items):
    return {item["key"]: _value(item["value"]) for item in items or []}


def _spans(trace):
    for batch in trace.get("batches", []):
        for scope in batch.get("scopeSpans", []):
            for span in scope.get("spans", []):
                yield {
                    "name": span["name"],
                    "status": span.get("status", {}),
                    "attributes": _attributes(span.get("attributes")),
                    "events": [
                        {"name": event["name"], **_attributes(event.get("attributes"))}
                        for event in span.get("events", [])
                    ],
                }


def error_traces(route):
    query = f'resource.service.name="{SERVICE}" && status=error'
    if route:
        query += f" && span.http.route={json.dumps(route)}"
    now = int(time.time())
    found = get_json(
        TEMPO,
        "/api/search",
        {"q": f"{{{query}}}", "limit": MAX_TRACES, "start": now - WINDOW_SECONDS, "end": now},
    )
    return [
        {"trace_id": hit["traceID"], "spans": list(_spans(get_json(TEMPO, f"/api/traces/{hit['traceID']}")))}
        for hit in found.get("traces", [])
    ]


def failing_paths(traces):
    """URL paths that answered 5xx, so the fix can be checked with the same requests."""
    paths = {
        span["attributes"]["url.path"]
        for trace in traces
        for span in trace["spans"]
        if "url.path" in span["attributes"]
        and int(span["attributes"].get("http.response.status_code", 0)) >= 500
    }
    return sorted(paths)


def collect(alert):
    """Gather metrics, logs and traces for the alerted route.

    A backend that cannot be reached is recorded under "unavailable" rather
    than aborting the incident, so the agent and a human both see the gap.
    """
    route = alert.get("labels", {}).get("http_route")
    evidence = {"route": route, "window_seconds": WINDOW_SECONDS, "unavailable": {}}
    sources = {
        "request_counts": request_counts,
        "error_logs": lambda: error_logs(route),
        "error_traces": lambda: error_traces(route),
    }
    for name, source in sources.items():
        try:
            evidence[name] = source()
        except (OSError, ValueError, KeyError) as error:
            evidence[name] = []
            evidence["unavailable"][name] = f"{type(error).__name__}: {error}"
    evidence["failing_paths"] = failing_paths(evidence["error_traces"])
    return evidence


def to_markdown(alert, evidence):
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    lines = [
        f"# {labels.get('alertname', 'alert')}",
        "",
        f"- Status: {alert.get('status')}",
        f"- Summary: {annotations.get('summary', '-')}",
        f"- Endpoint: {evidence['route'] or '-'}",
        f"- Started: {alert.get('startsAt', '-')}",
        f"- Dashboard: {alert.get('dashboardURL') or annotations.get('dashboard_url', '-')}",
        f"- Failing paths: {', '.join(evidence['failing_paths']) or '-'}",
        "",
        "## Request counts since app start",
        "",
        "| route | status | requests |",
        "| --- | --- | --- |",
    ]
    lines += [
        f"| {row.get('http_route')} | {row.get('http_response_status_code')} | {row['requests']:g} |"
        for row in evidence["request_counts"]
    ]
    lines += ["", f"## Error logs (last {WINDOW_SECONDS // 60} min, newest first)", ""]
    for log in evidence["error_logs"]:
        lines.append(
            f"- {log['message']} order_id={log.get('order_id')} trace_id={log.get('trace_id')} "
            f"{log.get('exception_type', '')}: {log.get('exception_message', '')}"
        )
    stacktrace = next(
        (log["exception_stacktrace"] for log in evidence["error_logs"] if "exception_stacktrace" in log),
        None,
    )
    if stacktrace:
        lines += ["", "Latest stack trace:", "", "```", stacktrace.rstrip(), "```"]
    lines += ["", "## Error traces", ""]
    for trace in evidence["error_traces"]:
        for span in trace["spans"]:
            exceptions = [event for event in span["events"] if event["name"] == "exception"]
            detail = "; ".join(
                f"{event.get('exception.type')}: {event.get('exception.message')}" for event in exceptions
            )
            lines.append(f"- {trace['trace_id']} {span['name']} {span['attributes'].get('url.path')} {detail}")
    if evidence["unavailable"]:
        lines += ["", "## Unavailable sources", ""]
        lines += [f"- {name}: {error}" for name, error in evidence["unavailable"].items()]
    return "\n".join(lines) + "\n"
