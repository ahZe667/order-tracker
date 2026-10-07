"""Incident responder: receives Grafana alerts on POST /alerts and starts a headless agent.

Run from the repository root:  uv run --frozen python incident-response/responder.py
"""

import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import evidence

REPO = Path(__file__).resolve().parent.parent
INCIDENTS = Path(__file__).resolve().parent / "incidents"
APP_URL = "http://localhost:8000"
AGENT_TIMEOUT_SECONDS = 20 * 60
OUTCOMES = ("RESOLVED:", "ESCALATE:", "NO ACTION:")

# Tools the agent may use. Anything else is denied without asking (dontAsk),
# so the agent can edit code and restart the app but not commit, push, or
# reach the network beyond the local app.
AGENT_TOOLS = "Read,Grep,Glob,Edit,Write,Bash"
AGENT_ALLOWED = [
    "Read",
    "Grep",
    "Glob",
    "Edit",
    "Write",
    "Bash(uv run --frozen pytest)",
    "Bash(uv run --frozen pytest *)",
    "Bash(docker compose up --build -d --wait app)",
    "Bash(docker compose ps *)",
    "Bash(docker compose logs *)",
    "Bash(curl -s http://localhost:8000/*)",
    "Bash(curl -si http://localhost:8000/*)",
    "Bash(git diff)",
    "Bash(git diff *)",
    "Bash(git status)",
    "Bash(git log *)",
]

PROMPT = """You are the on-call engineer for Order Tracker, the app in this repository.
A Grafana alert reached the incident responder. Everything collected for you is in
{incident}/: alert.json (the alert as Grafana sent it) and evidence.md / evidence.json
(request counts, error logs and error traces from the last 15 minutes).
Your shell starts in the repository root. Run every command from there and never use `cd`;
commands that change directory are denied.

1. Read the alert and the evidence first.
2. If the alert is a test (label test="true") or the evidence shows no failing requests,
   change nothing and reply in one or two sentences.
3. Otherwise find the root cause in the code, make the smallest fix, and add a regression
   test under tests/. Run `uv run --frozen pytest -q`.
4. Restart the app with `docker compose up --build -d --wait app`, then repeat the failing
   request(s) with `curl -si http://localhost:8000/...` and confirm they no longer return 5xx.
5. Do not commit or push, and do not change the telemetry, alerting or incident-response code.
   If you cannot fix it safely, stop and say what a developer should look at.

Write a short report: what users saw, the root cause, the change, and how you verified it.
The last line of your answer must start with exactly one of:
RESOLVED: <root cause and fix in one sentence>
ESCALATE: <why a developer is needed>
NO ACTION: <why nothing needed fixing>
"""

log = logging.getLogger("responder")
agent_lock = threading.Lock()
active_lock = threading.Lock()
active_fingerprints: set[str] = set()


def fingerprint(alert):
    if alert.get("fingerprint"):
        return alert["fingerprint"]
    labels = json.dumps(alert.get("labels", {}), sort_keys=True)
    return hashlib.sha256(labels.encode()).hexdigest()[:16]


def incident_id(alert):
    name = alert.get("labels", {}).get("alertname", "alert")
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return f"{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}-{slug}"


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")


def outcome_line(answer):
    lines = [line.strip() for line in answer.strip().splitlines() if line.strip()]
    return lines[-1] if lines else ""


def run_agent(incident):
    prompt = PROMPT.format(incident=incident.relative_to(REPO))
    (incident / "prompt.md").write_text(prompt)
    command = [
        "claude",
        "-p",
        "--output-format",
        "json",
        "--restricted",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--permission-mode",
        "dontAsk",
        "--tools",
        AGENT_TOOLS,
        "--allowedTools",
        *AGENT_ALLOWED,
    ]
    log.info("%s: starting headless agent", incident.name)
    try:
        completed = subprocess.run(
            command,
            input=prompt,
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=AGENT_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"error": f"agent timed out after {AGENT_TIMEOUT_SECONDS}s", "result": ""}
    (incident / "agent-stderr.log").write_text(completed.stderr)
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {"error": f"agent exited {completed.returncode} without JSON", "result": completed.stdout}
    if completed.returncode != 0 or result.get("is_error"):
        result["error"] = f"agent exited {completed.returncode}"
    return result


def verify(paths):
    """Repeat the requests that failed and run the tests; this, not the agent, decides."""
    requests = []
    requests_ok = True
    for path in paths:
        status, problem = None, None
        try:
            with urllib.request.urlopen(APP_URL + path, timeout=10) as response:
                status = response.status
        except urllib.error.HTTPError as error:
            status = error.code
        except OSError as error:
            problem = str(error)
        requests.append({"path": path, "status": status, "error": problem})
        requests_ok = requests_ok and status is not None and status < 500
    tests = subprocess.run(
        ["uv", "run", "--frozen", "pytest", "-q"], cwd=REPO, capture_output=True, text=True, timeout=300, check=False
    )
    return {
        "ok": requests_ok and tests.returncode == 0,
        "requests": requests,
        "tests_passed": tests.returncode == 0,
        "tests_output": tests.stdout[-2000:],
    }


def decide(outcome, verification):
    if verification is not None and not verification["ok"]:
        return "escalated"
    if outcome.startswith("RESOLVED:") and verification is not None:
        return "resolved"
    if outcome.startswith("NO ACTION:"):
        return "no_action"
    return "escalated"


def handle_incident(incident, alert):
    try:
        collected = evidence.collect(alert)
        write_json(incident / "evidence.json", collected)
        (incident / "evidence.md").write_text(evidence.to_markdown(alert, collected))
        with agent_lock:
            started = datetime.now(UTC)
            agent = run_agent(incident)
            write_json(incident / "agent.json", agent)
            answer = agent.get("result", "")
            (incident / "agent-response.md").write_text(answer + "\n")
            outcome = "" if agent.get("error") else outcome_line(answer)
            verification = verify(collected["failing_paths"]) if collected["failing_paths"] else None
        status = decide(outcome, verification)
        summary = {
            "incident": incident.name,
            "alert": alert.get("labels", {}),
            "agent_started": started.isoformat(),
            "agent_finished": datetime.now(UTC).isoformat(),
            "agent_error": agent.get("error"),
            "agent_cost_usd": agent.get("total_cost_usd"),
            "agent_outcome": outcome,
            "verification": verification,
            "status": status,
        }
        write_json(incident / "summary.json", summary)
        if status == "escalated":
            (incident / "ESCALATION.md").write_text(
                f"# Escalation: {incident.name}\n\nThe responder could not confirm a fix.\n\n"
                f"- Agent outcome: {outcome or agent.get('error')}\n"
                f"- Verification: {json.dumps(verification)}\n\nSee evidence.md and agent-response.md.\n"
            )
        log.info("%s: %s (%s)", incident.name, status, outcome)
    finally:
        with active_lock:
            active_fingerprints.discard(fingerprint(alert))


def accept(payload):
    """Open one incident per new firing alert; return what was started or skipped."""
    started, skipped = [], []
    context = {key: value for key, value in payload.items() if key != "alerts"}
    for alert in payload.get("alerts") or []:
        key = fingerprint(alert)
        if alert.get("status") != "firing":
            skipped.append({"fingerprint": key, "reason": alert.get("status", "no status")})
            continue
        with active_lock:
            if key in active_fingerprints:
                skipped.append({"fingerprint": key, "reason": "already being handled"})
                continue
            active_fingerprints.add(key)
        incident = INCIDENTS / incident_id(alert)
        incident.mkdir(parents=True)
        write_json(incident / "alert.json", {"notification": context, "alert": alert})
        threading.Thread(target=handle_incident, args=(incident, alert), daemon=True).start()
        started.append(incident.name)
        log.info("%s: opened for %s", incident.name, alert.get("labels"))
    return {"incidents": started, "skipped": skipped}


class AlertHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/alerts":
            return self.reply(404, {"error": "not found"})
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return self.reply(400, {"error": "invalid JSON"})
        if not isinstance(payload, dict):
            return self.reply(400, {"error": "expected a JSON object"})
        self.reply(202, accept(payload))

    def reply(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        log.info("%s %s", self.address_string(), format % args)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    host = os.getenv("RESPONDER_HOST", "127.0.0.1")
    port = int(os.getenv("RESPONDER_PORT", "8001"))
    server = ThreadingHTTPServer((host, port), AlertHandler)
    log.info("listening on http://%s:%s/alerts", host, port)
    server.serve_forever()


if __name__ == "__main__":
    main()
