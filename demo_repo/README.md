# Shop Demo Repository

Disposable sample service used to demonstrate RepoRescue's autonomous multi-agent bug-fixing workflow.

## Seeded Bug Scenario
Calling `calculate_order_total(items, coupon_code="EXPIRED")` raises an unhandled `ValueError("HTTP 500: Server error unhandled coupon state EXPIRED")`.

## Fix Path
Update `src/shop/checkout.py` to handle expired/unknown coupons gracefully by ignoring them without crashing, and verify via `tests/test_reproduce_bug.py`.
