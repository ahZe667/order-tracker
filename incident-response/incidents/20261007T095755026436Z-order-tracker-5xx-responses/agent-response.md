I've made a fix and added a regression test, but I couldn't run either. The test command and the app restart were blocked: the session's permission mode denies shell commands that change directory. So nothing below has been verified.

**What users saw:** `GET /api/orders/express-1002` returned HTTP 500 (once at 09:57 UTC). Any express order placed within two days of the end of a month would fail to open the same way.

**Root cause:** `order_detail` in `app/main.py:74` worked out the express delivery estimate as `placed_at.replace(day=placed_at.day + 2)`. That only changes the day number, so it can't roll into the next month. An order placed on the 30th asks for day 32, and Python raises `ValueError: day is out of range for month`. The seeded order `express-1002` is dated the last day of the previous month, so it always hits this.

**Change (not committed):**
- `app/main.py:74`: the line is now `estimated_at = placed_at + timedelta(days=2)`. `timedelta` was already imported. No telemetry, alerting or incident-response code was touched.
- `tests/test_api.py`: two new tests.
  - `test_express_delivery_estimate_crosses_month_end` checks that an order placed 2026-09-30 gets an estimate of 2026-10-02.
  - `test_seeded_express_order_lookup` checks that `GET /api/orders/express-1002` returns 200 with an `estimated_delivery` field.

**Verification:** none. Running `uv run --frozen pytest -q` from the incident folder collected no tests, and the same command from the repo root was denied. The `docker compose` restart and the `curl` re-check have not been run. To finish, run these from the repo root:
1. `uv run --frozen pytest -q`
2. `docker compose up --build -d --wait app`
3. `curl -si http://localhost:8000/api/orders/express-1002` (should return 200)

ESCALATE: The root cause is found and a fix is in place (date arithmetic in `order_detail` replaced with `timedelta(days=2)`), but someone with shell access needs to run the tests, restart the app and re-check `/api/orders/express-1002`, because those commands were blocked here.
