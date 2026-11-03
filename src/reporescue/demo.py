from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

DEMO_REPRO_TEST_CODE = """from shop.checkout import calculate_order_total

def test_reproduce_expired_coupon_500():
    items = [{"price": 100.0, "quantity": 1}]
    # Should not throw HTTP 500 error on expired coupon
    total = calculate_order_total(items, coupon_code="EXPIRED")
    assert total == 100.0
"""

DEMO_FIXED_CHECKOUT_CODE = """from __future__ import annotations


def calculate_order_total(items: list[dict[str, float]], coupon_code: str | None = None) -> float:
    \"\"\"Calculates order total applying optional coupon code.\"\"\"
    subtotal = sum(item["price"] * item.get("quantity", 1) for item in items)
    if not coupon_code:
        return round(subtotal, 2)

    valid_coupons = {
        "SAVE10": 0.10,
        "SAVE20": 0.20,
    }

    # Fix: Expired or invalid coupons are handled gracefully without raising HTTP 500
    discount = valid_coupons.get(coupon_code, 0.0)
    return round(subtotal * (1.0 - discount), 2)
"""


def _handle_remove_readonly(func: Any, path: str, exc_info: Any) -> None:
    """Clear readonly bit and retry file removal on Windows."""
    import stat

    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def init_demo_repository(target_path: Path | str) -> Path:
    """Initializes a standalone Git repository from demo_repo files with an initial commit.

    Args:
        target_path: Directory path where demo repository should be initialized.

    Returns:
        Path to the initialized Git demo repository.
    """
    dest = Path(target_path).resolve()

    # If previous demo repo exists, clean it up to ensure pristine baseline
    if dest.exists():
        try:
            shutil.rmtree(dest, onexc=_handle_remove_readonly)
        except OSError:
            pass

    dest.mkdir(parents=True, exist_ok=True)

    # Locate source demo_repo template
    source_demo = Path(__file__).resolve().parent.parent.parent / "demo_repo"
    if not source_demo.exists():
        raise FileNotFoundError(f"Source demo_repo not found at {source_demo}")

    # Copy template files (including dotfiles like .gitignore)
    for item in source_demo.iterdir():
        target_item = dest / item.name
        if item.is_dir():
            shutil.copytree(item, target_item, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target_item)

    # Initialize Git
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=dest, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "RepoRescue Demo"],
        cwd=dest,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "demo@reporescue.local"],
        cwd=dest,
        check=True,
        capture_output=True,
    )

    # Initial commit
    subprocess.run(["git", "add", "."], cwd=dest, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "commit",
            "-m",
            "Initial commit for shop service with seeded coupon bug",
        ],
        cwd=dest,
        check=True,
        capture_output=True,
    )

    return dest
