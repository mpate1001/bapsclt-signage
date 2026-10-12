#!/bin/sh
# Run from the graphical desktop session, e.g. ~/.config/labwc/autostart.
set -eu
if command -v chromium >/dev/null 2>&1; then
    browser=chromium
elif command -v chromium-browser >/dev/null 2>&1; then
    browser=chromium-browser
else
    echo "Install Chromium before launching the signage kiosk" >&2
    exit 1
fi
while :; do
    "$browser" --kiosk --noerrdialogs --disable-session-crashed-bubble \
        --autoplay-policy=no-user-gesture-required \
        --user-data-dir="$HOME/.config/bapsclt-signage-chromium" \
        http://127.0.0.1:8090/index.html || true
    sleep 5
done
