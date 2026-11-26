"""One address per page (Search Console, 26.11): the homepage names its canonical URL, www redirects
to the bare domain, /favicon.ico and the API base URL without its slash are no longer 404s."""
from pathlib import Path

SITE = Path(__file__).resolve().parents[1] / "examples" / "canton_venues"


def test_homepage_names_its_canonical():
    html = (SITE / "site" / "index.html").read_text()
    assert '<link rel="canonical" href="https://cantonvenues.com/">' in html


def test_vhost_redirects_www_and_covers_the_old_404s():
    conf = (SITE / "deploy" / "nginx.conf").read_text()
    main = conf.split("server {")[1]  # the cantonvenues.com block, before the nip.io one
    assert "if ($host = www.cantonvenues.com) { return 301 https://cantonvenues.com$request_uri; }" in main
    assert "location = /favicon.ico { try_files /icon.png =404; }" in main
    assert "return 301 https://cantonvenues.com/api/v1/;" in main
