"""get.scaliocloud.com is GitHub Pages from the root of main: index.html must be the current get.sh."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_site_serves_the_current_get_sh():
    assert (ROOT / "index.html").read_bytes() == (ROOT / "get.sh").read_bytes(), "cp get.sh index.html"
    assert (ROOT / "CNAME").read_text().strip() == "get.scaliocloud.com"
    assert (ROOT / ".nojekyll").is_file()
