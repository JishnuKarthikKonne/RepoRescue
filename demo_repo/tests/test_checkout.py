from shop.checkout import calculate_order_total


def test_checkout_standard():
    items = [{"price": 50.0, "quantity": 2}]
    assert calculate_order_total(items) == 100.0


def test_checkout_valid_coupon():
    items = [{"price": 100.0, "quantity": 1}]
    assert calculate_order_total(items, "SAVE10") == 90.0
