#!/usr/bin/env bash
# Deploy Canton Venues from a git ref, never from the working tree.
#   examples/canton_venues/deploy/deploy.sh [ref]     (default: HEAD)
# No secrets: every source the collector reads is public.
set -euo pipefail

SERVER="${VENUES_SERVER:-root@46.225.216.13}"
HOST="cantonvenues.com"
OLD_HOST="canton.46-225-216-13.nip.io"
REPO="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
SHA="$(git -C "$REPO" rev-parse --short "${1:-HEAD}")"

echo "▸ release $SHA"
git -C "$REPO" archive --format=tar "$SHA" | ssh "$SERVER" "
  set -e
  mkdir -p /opt/canton-venues/releases/$SHA /var/www/canton-venues/api/v1
  tar xf - -C /opt/canton-venues/releases/$SHA"

ssh "$SERVER" "
  set -e
  cd /opt/canton-venues
  test -d .venv || python3 -m venv .venv
  .venv/bin/pip install -q --upgrade \"./releases/$SHA[venues]\"
  ln -sfn releases/$SHA current
  install -m 644 current/examples/canton_venues/site/index.html current/examples/canton_venues/site/*.png \
    current/examples/canton_venues/site/llms.txt current/examples/canton_venues/site/robots.txt /var/www/canton-venues/
  install -m 644 current/examples/canton_venues/site/api-index.json /var/www/canton-venues/api/v1/index.json
  install -m 644 current/examples/canton_venues/deploy/canton-venues.service current/examples/canton_venues/deploy/canton-venues-mcp.service current/examples/canton_venues/deploy/canton-venues-contact.service \
    current/examples/canton_venues/deploy/canton-venues-study.service current/examples/canton_venues/deploy/canton-venues-study.timer \
    current/examples/canton_venues/deploy/canton-venues-crosscheck.service current/examples/canton_venues/deploy/canton-venues-crosscheck.timer \
    current/examples/canton_venues/deploy/canton-venues-weekly.service current/examples/canton_venues/deploy/canton-venues-weekly.timer \
    current/examples/canton_venues/deploy/canton-venues-watchdog.service current/examples/canton_venues/deploy/canton-venues-watchdog.timer /etc/systemd/system/
  # (re)install the vhost only when the repo's conf-version changes; certbot then adds TLS
  if ! grep -q \"\$(head -1 current/examples/canton_venues/deploy/nginx.conf)\" /etc/nginx/sites-available/canton-venues 2>/dev/null; then
    install -m 644 current/examples/canton_venues/deploy/nginx.conf /etc/nginx/sites-available/canton-venues
    ln -sfn /etc/nginx/sites-available/canton-venues /etc/nginx/sites-enabled/canton-venues
    nginx -t && systemctl reload nginx
    certbot --nginx -d $HOST -d www.$HOST --keep-until-expiring --non-interactive --agree-tos --register-unsafely-without-email --redirect
    certbot --nginx -d $OLD_HOST --non-interactive --agree-tos --register-unsafely-without-email --reinstall --redirect
  fi
  systemctl daemon-reload
  systemctl enable canton-venues canton-venues-mcp canton-venues-contact >/dev/null 2>&1
  # the study timer is enabled once and never restarted here: it only writes spreads.json
  systemctl is-enabled canton-venues-study.timer >/dev/null 2>&1 || systemctl enable --now canton-venues-study.timer
  # the daily cross-check timer, enabled once like the study's (OnCalendar, not Persistent: enabling never fires it)
  systemctl is-enabled canton-venues-crosscheck.timer >/dev/null 2>&1 || systemctl enable --now canton-venues-crosscheck.timer
  # the weekly card's timer, the same way (OnCalendar, not Persistent). Its first card appears on its
  # first run; deploy does not run it
  systemctl is-enabled canton-venues-weekly.timer >/dev/null 2>&1 || systemctl enable --now canton-venues-weekly.timer
  # the watchdog's timer, enabled once: every ten minutes it reads the live site and messages the owner
  # on Telegram only when the snapshot is over 30 min old, and once when it is fresh again
  systemctl is-enabled canton-venues-watchdog.timer >/dev/null 2>&1 || systemctl enable --now canton-venues-watchdog.timer
  systemctl restart canton-venues canton-venues-mcp canton-venues-contact
  sleep 3
  systemctl is-active canton-venues canton-venues-mcp canton-venues-contact
  ls -1t releases | tail -n +6 | sed 's|^|releases/|' | xargs -r rm -r --"

echo "✓ https://$HOST"
