import json

import evidence
import pytest
import responder


class InlineThread:
    def __init__(self, target, args, daemon):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


@pytest.fixture
def incidents(tmp_path, monkeypatch):
    monkeypatch.setattr(responder, "INCIDENTS", tmp_path)
    monkeypatch.setattr(responder, "active_fingerprints", set())
    monkeypatch.setattr(responder.threading, "Thread", InlineThread)
    handled = []
    monkeypatch.setattr(responder, "handle_incident", lambda incident, alert: handled.append(alert))
    return tmp_path, handled


def test_decide_trusts_verification_over_the_agent():
    assert responder.decide("RESOLVED: fixed", {"ok": True}) == "resolved"
    assert responder.decide("RESOLVED: fixed", {"ok": False}) == "escalated"
    assert responder.decide("RESOLVED: fixed", None) == "escalated"
    assert responder.decide("NO ACTION: test alert", None) == "no_action"
    assert responder.decide("ESCALATE: unclear", {"ok": True}) == "escalated"
    assert responder.decide("", None) == "escalated"


def test_outcome_line_is_last_non_empty_line():
    assert responder.outcome_line("Report\n\nRESOLVED: fixed\n\n") == "RESOLVED: fixed"


def test_accept_opens_one_incident_per_new_firing_alert(incidents):
    directory, handled = incidents
    alert = {"status": "firing", "labels": {"alertname": "ResponderTest", "test": "true"}}

    first = responder.accept({"receiver": "responder", "alerts": [alert, {"status": "resolved"}]})
    second = responder.accept({"alerts": [alert]})

    assert len(first["incidents"]) == 1
    assert first["skipped"][0]["reason"] == "resolved"
    assert second["incidents"] == []
    assert second["skipped"][0]["reason"] == "already being handled"
    assert handled == [alert]
    saved = json.loads((directory / first["incidents"][0] / "alert.json").read_text())
    assert saved == {"notification": {"receiver": "responder"}, "alert": alert}


def test_failing_paths_keeps_only_5xx_requests():
    traces = [
        {
            "trace_id": "t1",
            "spans": [
                {"attributes": {"url.path": "/api/orders/express-1002", "http.response.status_code": "500"}},
                {"attributes": {"url.path": "/api/orders/missing", "http.response.status_code": "404"}},
            ],
        }
    ]
    assert evidence.failing_paths(traces) == ["/api/orders/express-1002"]
