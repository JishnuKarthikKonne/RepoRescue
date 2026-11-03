from __future__ import annotations


def calculate_order_total(
    items: list[dict[str, float]], coupon_code: str | None = None
) -> float:
    """Calculates order total applying optional coupon code."""
    subtotal = sum(item["price"] * item.get("quantity", 1) for item in items)
    if not coupon_code:
        return round(subtotal, 2)

    valid_coupons = {
        "SAVE10": 0.10,
        "SAVE20": 0.20,
    }

    # Seeded intentional bug for RepoRescue demonstration:
    # If coupon is "EXPIRED", an unhandled ValueError is raised causing HTTP 500
    if coupon_code == "EXPIRED":
        raise ValueError("HTTP 500: Server error unhandled coupon state EXPIRED")

    discount = valid_coupons.get(coupon_code, 0.0)
    return round(subtotal * (1.0 - discount), 2)
