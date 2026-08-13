import subprocess
import sys


def _run_helper_check(code: str):
    result = subprocess.run(
        [sys.executable, "-c", code],
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_lazy_module_imports_target_on_first_attribute_access_only():
    _run_helper_check(
        """
import sys

sys.modules.pop("fractions", None)
from core.lazy_imports import LazyModule

fractions = LazyModule("fractions")
assert "fractions" not in sys.modules
assert fractions.Fraction(1, 2) + fractions.Fraction(1, 2) == 1
assert "fractions" in sys.modules
"""
    )


def test_lazy_attribute_imports_target_on_first_call_only():
    _run_helper_check(
        """
import sys

sys.modules.pop("fractions", None)
from core.lazy_imports import LazyAttribute

Fraction = LazyAttribute("fractions", "Fraction")
assert "fractions" not in sys.modules
assert Fraction(3, 6) == Fraction(1, 2)
assert "fractions" in sys.modules
"""
    )
