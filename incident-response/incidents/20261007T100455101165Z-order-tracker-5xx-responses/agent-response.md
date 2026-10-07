Opening order `express-1002` works again: after the fix it returns 200 instead of a 500.

**What users saw:** the order page for `express-1002` failed every time (3 server errors in the evidence, all with `ValueError: day is out of range for month`). Any express order placed on one of the last two days of a month would have failed the same way.

**Root cause:** `order_detail` in `app/main.py:74` worked out the express delivery estimate by adding 2 to the day of the month (`placed_at.replace(day=placed_at.day + 2)`). For `express-1002`, placed on 2026-09-30, that asks for 32 September, which doesn't exist, so the lookup crashed.

**Change:**
- `app/main.py`: the estimate is now `placed_at + timedelta(days=2)`, which moves correctly into the next month. That one line is the whole fix.
- `tests/test_api.py`: two regression tests. One checks the seeded month-end order `express-1002` returns 200 with an `estimated_delivery`. The other checks an order placed 2026-09-30 gets an estimate of 2026-10-02.

**Verification:**
- `uv run --frozen pytest -q`: 9 passed.
- `docker compose up --build -d --wait app`: the app container rebuilt and reported healthy.
- `curl -si http://localhost:8000/api/orders/express-1002`: `HTTP/1.1 200 OK`, with `"estimated_delivery":"2026-10-02"`.

Nothing is committed. `incident-response/responder.py` already had uncommitted changes before I started; I didn't touch it.

RESOLVED: Express delivery estimates added 2 to the day of the month, which crashed for orders placed at month end, so I replaced it with `+ timedelta(days=2)` and added regression tests.
