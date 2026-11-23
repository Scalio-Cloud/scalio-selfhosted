import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_cryptography_is_at_least_50():
    # Dependabot-Alerts 5-7 (ADR 0131): cryptography < 50.0.0 hat offene High-Schwachstellen.
    text = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    m = re.search(r"^cryptography(?:==|>=)(\d+)\.", text, re.MULTILINE)
    assert m, "cryptography fehlt in requirements-dev.txt"
    assert int(m.group(1)) >= 50
