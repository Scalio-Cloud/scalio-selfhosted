"""get.scaliocloud.com is GitHub Pages from docs/: both copies of get.sh must be the current get.sh."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_site_serves_the_current_get_sh():
    src = (ROOT / "get.sh").read_bytes()
    for name in ("index.html", "get.sh"):
        assert (ROOT / "docs" / name).read_bytes() == src, f"docs/{name} differs: cp get.sh docs/{name}"
    assert (ROOT / "docs" / "CNAME").read_text().strip() == "get.scaliocloud.com"
    assert (ROOT / "docs" / ".nojekyll").is_file()
