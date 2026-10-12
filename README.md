# BAPS CLT Signage

Upload an MP4 through `/admin`; show only the looping video at `/view`.
The Raspberry Pi downloads and verifies each published video, then plays a local
copy in Chromium kiosk mode. Cached playback continues when the internet is down.

## What is included

- `server/`: FastAPI server, password login, protected upload, public display,
  token-protected Pi API and persistent video storage.
- `pi/`: download agent, local player, systemd unit and Chromium kiosk launcher.
- `docker/`: container image and Compose deployment.
- `.env.example` and `pi/pi.env.example`: configuration examples with no secrets.
- `tests/` and `.github/workflows/tests.yml`: automated checks.

This is a new implementation based on the earlier setup. It has not been installed
on your existing TrueNAS or Pi. Back up your current files, service definitions,
and media before switching. Existing server tokens, metadata formats, and cached
state may differ: deploy this server and this Pi agent together. Old media is not
imported automatically; upload your current MP4 after starting the new server.

## Create the GitHub repository

Create an empty GitHub repository named `bapsclt-signage` (a private repository is
recommended). In GitHub Desktop choose **Add Local Repository**, select this
folder, and use **create a repository here** if prompted. Review the files before
the first commit, then publish to your new repository. Do not initialize the
remote with a separate README if you are publishing this local folder.

Keep real passwords and tokens in the ignored `.env` / `pi.env` files. Videos and
runtime data are ignored too. The example files are safe to commit. No Git
repository or remote has been created by this setup.

## Start the server on TrueNAS or a Docker host

Clone the repository to the host, then run these commands from the repository root:

```sh
cp .env.example .env
```

Edit `.env` and fill in `ADMIN_PASSWORD`, `SESSION_SECRET`, and `SIGNAGE_API_TOKEN`.
Use a long unique password, and generate a separate random value for each secret:

```sh
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

`ADMIN_USERNAME` defaults to `admin`. `MAX_UPLOAD_MB` defaults to 2048 MiB (2 GiB).
Keep `COOKIE_SECURE=true` when using HTTPS. For testing at localhost over HTTP,
set it to `false`, then restore `true` before public use.

```sh
docker compose --env-file .env -f docker/compose.yaml up -d --build
```

Compose exposes port 8080 on the host's loopback interface by default. Connect your
existing HTTPS reverse proxy or tunnel to `http://127.0.0.1:8080`. If the proxy runs
on a different machine or in an isolated container, set `BIND_ADDRESS` to a host
LAN address reachable by that proxy. Keep the port on your trusted network.
Configure the proxy's upload size and timeout to accommodate your videos.

The public hostname is configured in your reverse proxy / tunnel, not in Python:

- `https://bapsclt.datavault.digital/admin`: administrator sign-in and upload.
- `https://bapsclt.datavault.digital/upload`: publisher sign-in and upload.
- `https://bapsclt.datavault.digital/view`: media-only viewer.
- `/`: redirects to `/view`.
- `/health`: container health check.

The media shown by `/view` is public to anyone who knows the URL. Uploads require
login. The separate Pi API requires the bearer token from your `.env`.

Upload an MP4 containing H.264 video with yuv420p pixel format. The server validates it using `ffprobe` and
publishes it after validation. No transcoding is performed. Browser playback is
muted for reliable autoplay; the video fits the display without stretching.
One upload replaces the currently published video; there is no playlist editor.

Docker stores media in a named volume that survives container rebuilds. Back up
that volume. Do not use `docker compose down -v` unless you intend to erase it.
Keep one server process/replica, as configured, to serialize publication.

## Larger videos through Cloudflare Free

The upload portal sends one selected video in 8 MiB chunks, then asks the server
to validate and publish the assembled file. Each request stays below Cloudflare
Free's 100 MB request limit. You do not need to split the video yourself. Both the
administrator and shared publisher use this flow.

The default total limit is **2048 MiB (2 GiB / 2147483648 bytes)**. For the existing
TrueNAS Apps deployment, set `MAX_UPLOAD_MB: '2048'` under the service environment.
On the Pi, set `SIGNAGE_MAX_VIDEO_BYTES=2147483648` in
`/etc/bapsclt-signage/pi.env`, then restart the agent. Existing environment values
override new code defaults, so both settings must be updated during deployment.

Progress first reports transfer, then video validation/publication. The portal
retries temporary connection failures while the page remains open; confirmed
chunks are retained rather than uploaded again. Final validation runs in the
background and is checked through short status requests, so a large video does
not require one long request through Cloudflare. Keep the page open until it
reports publication complete. Cancelling during transfer removes the temporary
upload; the currently published video stays available until a replacement passes
validation and is published.

Unfinished upload sessions expire after 24 hours and temporary data is cleaned up.
The server needs enough free storage for the assembled video alongside retained
videos. The legacy single-request upload API remains available for compatibility,
but large files through Cloudflare should use the portal's chunked flow.

Cloudflare's plan limits are documented in
[Error 413](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/4xx-client-error/error-413/).

## Admin management

The administrator's `/admin` page includes a video library, device status, and
separate controls to reboot a Pi or restart the signage server. Publisher accounts
only see upload/publish controls on `/upload`.

The library lists retained videos with previews, sizes, and publication status.
Deleting a video requires confirmation. The currently published video and videos
cached by online devices are protected from deletion. Older files are retained
until the administrator deletes them; uploads do not automatically remove them.

Devices report their cached video and last check-in. A recent check-in means the
agent is online; it does not prove the TV is rendering frames. Devices without the
reboot helper installed are shown without an available reboot action. Requests
expire after five minutes and are acknowledged before the Pi triggers a reboot.

To allow **Restart signage server**, set `SIGNAGE_ALLOW_RESTART=true` in the server
environment and retain `restart: unless-stopped` (or `always`) in Compose. For the
TrueNAS Apps installation, add `SIGNAGE_ALLOW_RESTART: 'true'` under the existing
service `environment` section and save the updated application. Keep this option
false for local development without a restart supervisor.

This action gracefully ends the signage process so Docker starts it again using
the already deployed image. It affects only the signage container. Updating code
still requires pulling GitHub changes, rebuilding the image, and redeploying the
application. Cached Pi playback continues while the server restarts.

### Enable the Pi reboot control

Update the Pi checkout and restart its agent to enable heartbeat/status reporting.
The existing API token is used; no new token is needed. `SIGNAGE_DEVICE_ID` is an
optional stable device name; when omitted or empty, the Pi's hostname is used.

Install the narrow systemd reboot helper from the Pi checkout. The default paths
assume `/home/admin/bapsclt-signage-data` is already created. If you use a different
`SIGNAGE_DATA_DIR`, update the path in both helper units to match before installing.

```sh
sudo install -m 644 pi/systemd/bapsclt-signage-reboot.service /etc/systemd/system/
sudo install -m 644 pi/systemd/bapsclt-signage-reboot.path /etc/systemd/system/
sudo rm -f /home/admin/bapsclt-signage-data/reboot.request
sudo systemctl daemon-reload
sudo systemctl enable --now bapsclt-signage-reboot.path
sudo systemctl restart bapsclt-signage-agent.service
```

The removal command clears only a stale reboot-request marker before activation.
The agent remains unprivileged and writes a marker only for an acknowledged,
unexpired reboot command. The root helper removes that marker before invoking the
fixed system reboot command. It provides no general remote shell execution.

Test these controls during a suitable maintenance window: a Pi reboot briefly
interrupts that display, and a server restart briefly interrupts the upload portal.

## Shared publisher account

Enable an optional publisher account with `PUBLISHER_USERNAME` and
`PUBLISHER_PASSWORD` in the server environment. Set both values or leave both
blank to disable the account. Its username must differ from `ADMIN_USERNAME`.
Keep the password outside the repository.

For the shared account, set the username to `bapsclt` and choose its password in
your private server configuration. Share the `/upload` URL with publishers. This
account can upload and publish videos through `/upload`, but cannot access the
`/admin` portal or its protected endpoints. The administrator retains access to
both portals. The `/view` page remains public.

When using the TrueNAS Apps YAML installation, add these keys under the existing
service `environment` section after deploying an image built from the updated
repository:

```yaml
PUBLISHER_USERNAME: 'bapsclt'
PUBLISHER_PASSWORD: '<set privately in TrueNAS>'
```

Rebuild the server image and restart the app to apply code or credential changes.
After updating to role-based sessions, sign in again. Disabling the publisher
account or changing its username invalidates its existing access. Sign-in sessions
otherwise expire after eight hours.

## Raspberry Pi installation

These examples assume Raspberry Pi OS Desktop with the existing user `admin` and
a Labwc desktop session. If your user differs, adjust the service's `User`,
`Group`, paths, and launcher instructions before installing. Desktop auto-login
must be enabled for Chromium to launch after reboot.

Clone your repository to `/home/admin/bapsclt-signage`. Python 3 is required;
the Pi agent uses the standard library. Run the following commands from the
repository root. Install Chromium if it is not present:

```sh
sudo apt update
sudo apt install -y chromium
sudo install -d -m 755 /etc/bapsclt-signage
sudo install -m 600 pi/pi.env.example /etc/bapsclt-signage/pi.env
sudo nano /etc/bapsclt-signage/pi.env
```

Set `SIGNAGE_URL=https://bapsclt.datavault.digital` and paste the same
`SIGNAGE_API_TOKEN` used on the server. The default cache is `/home/admin/bapsclt-signage-data`, outside the Git checkout
and separate from the older installation. Create it before starting systemd:

```sh
sudo install -d -o admin -g admin -m 700 /home/admin/bapsclt-signage-data
```

If you change `SIGNAGE_DATA_DIR`, also update `ReadWritePaths` in the service unit
and create the selected directory with the Pi user as owner. Install and start
the service:

```sh
sudo cp pi/systemd/bapsclt-signage-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bapsclt-signage-agent.service
journalctl -u bapsclt-signage-agent.service -f
```

The agent also serves the cached player at `http://127.0.0.1:8090/index.html`.
From a Pi desktop terminal, test the launcher:

```sh
sh /home/admin/bapsclt-signage/pi/kiosk.sh
```

For automatic startup, add this line to `~/.config/labwc/autostart`:

```sh
sh /home/admin/bapsclt-signage/pi/kiosk.sh &
```

Preserve other autostart entries. Replace any older Chromium watchdog or signage
launch entry so only one signage browser runs. Log out and back in or reboot to
apply the change. Kiosk mode removes the browser controls; the page fills the
screen without resizing a browser window. Other desktop environments need their
own login-session autostart mechanism.

If replacing the older setup, back up and stop its signage agent/local HTTP
service first; this new agent owns port 8090. Check that its data path does not
point at old state before you start it. These files do not automatically change
or stop your existing services.

## Offline Wi-Fi setup hotspot (optional)

The Pi can offer a password-protected **BAPS-Signage-Setup** network when neither
Ethernet nor saved Wi-Fi connects for 120 seconds. Connect a phone to that network,
then open **http://10.42.0.1:8091/** in its browser. Choose your network from the
nearby Wi-Fi list and enter its password, or choose **Retry saved Wi-Fi networks**
if an existing network is back. The list shows signal strength and whether a
password is needed. Networks with unsupported security, including organization sign-in, are
unavailable for selection. For a hidden network, choose **Enter a Wi-Fi name manually**.

The initial list uses networks detected before the hotspot started. Choose
**Refresh nearby networks** to look again. Some Wi-Fi adapters temporarily pause
the setup hotspot while scanning; if your phone disconnects, reconnect to
**BAPS-Signage-Setup** and reload the page to see the new list.
There is no automatic captive-portal popup; open the address yourself. Your phone
may warn that this setup network has no internet. Stay connected to complete setup.

The setup network disappears when the Pi tries Wi-Fi. Wait up to two minutes; if
it returns, reconnect to it and reopen the page to retry. A failed attempt restores
the setup network. Saved networks are retried only when you request it on the page,
so the setup connection stays available while you enter details. Connecting
Ethernet stops the setup hotspot automatically. An internet outage on a working
local network does not trigger fallback. Cached signage playback continues; new
video downloads and remote management return when internet access returns.

This feature uses NetworkManager and the Pi's Wi-Fi adapter. It supports ordinary
WPA personal networks and, when explicitly selected, networks without a password.
Enterprise Wi-Fi and networks requiring a browser sign-in are not supported by the
setup page. It does not change the Pi's Wi-Fi country setting. Use the existing
Raspberry Pi OS configuration tools to set the correct country before deployment.

### Install on the Pi

This is opt-in and needs no TrueNAS changes. First confirm that NetworkManager
manages the Wi-Fi adapter on your Raspberry Pi OS installation:

```sh
systemctl is-active NetworkManager
nmcli --version
busctl --version
nmcli -f GENERAL.DEVICE,GENERAL.TYPE,GENERAL.STATE device show wlan0
```

Continue only when NetworkManager is active and `wlan0` is a managed Wi-Fi device.
Nearby-network refresh also needs `busctl`, provided by systemd on Raspberry Pi OS,
to confirm scan freshness. If scan status is unavailable, the page still supports
manual entry of the Wi-Fi name and password.
If your adapter has another name, set `WIFI_INTERFACE` accordingly in the private
configuration below. If another network manager owns the adapter, configure that
separately before enabling this helper. NetworkManager's shared IPv4 mode needs
`dnsmasq-base`; install the binary package without adding a second DHCP daemon:

```sh
sudo apt update
sudo apt install -y network-manager dnsmasq-base
```

From `/home/admin/bapsclt-signage`, install root-owned copies. The privileged helper
runs these copies rather than writable files from the Git checkout:

```sh
sudo install -d -o root -g root -m 755 /usr/local/lib/bapsclt-signage
sudo install -o root -g root -m 755 pi/wifi-setup.py /usr/local/lib/bapsclt-signage/
sudo install -o root -g root -m 644 pi/wifi-setup.html /usr/local/lib/bapsclt-signage/
sudo install -d -o root -g root -m 755 /etc/bapsclt-signage
if ! sudo test -e /etc/bapsclt-signage/wifi.env; then
  sudo install -o root -g root -m 600 pi/wifi.env.example /etc/bapsclt-signage/wifi.env
fi
sudo nano /etc/bapsclt-signage/wifi.env
```

Set `WIFI_HOTSPOT_PASSWORD` to a unique password of 12–63 ASCII characters.
Leave `WIFI_INTERFACE=wlan0` and `WIFI_FALLBACK_SECONDS=120` unless your installation
needs different values. Keep this password in the private root-owned file; do not
commit it or reuse the admin, publisher, or device API credentials. NetworkManager
stores Wi-Fi credentials in its root-owned connection profiles; the setup page
does not save passwords in browser storage. Anyone with the setup hotspot password
can change the Pi's Wi-Fi settings while fallback is active.

```sh
sudo install -o root -g root -m 644 pi/systemd/bapsclt-signage-wifi.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bapsclt-signage-wifi.service
sudo systemctl status bapsclt-signage-wifi.service --no-pager
```

Test locally with the TV and a phone available. Disconnect Ethernet and make the
saved Wi-Fi unavailable; after the delay, join the setup hotspot and enter working
Wi-Fi details. Confirm the hotspot disappears, the Pi reconnects, and video keeps
playing. Test an incorrect password too: the setup hotspot should return. Keep a
keyboard available for recovery during this first hardware test. These adapter,
phone, and physical playback checks require the real Pi.

For updates, run these commands from `/home/admin/bapsclt-signage`. Preserve
`/etc/bapsclt-signage/wifi.env`; do not replace it with the sample configuration:

```sh
git pull --ff-only
sudo install -o root -g root -m 755 pi/wifi-setup.py /usr/local/lib/bapsclt-signage/
sudo install -o root -g root -m 644 pi/wifi-setup.html /usr/local/lib/bapsclt-signage/
sudo install -o root -g root -m 644 pi/systemd/bapsclt-signage-wifi.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart bapsclt-signage-wifi.service
```

The normal signage agent and server need no restart for Wi-Fi helper updates.

### Disable or remove the fallback

Disable the helper before changing its reserved NetworkManager connection:

```sh
sudo systemctl disable --now bapsclt-signage-wifi.service
sudo nmcli -f connection.id,connection.uuid,802-11-wireless.mode,802-11-wireless.ssid connection show uuid 72c25775-673a-4b48-bf74-511894293f48
```

Confirm that the displayed profile is `bapsclt-signage-setup`, with mode `ap` and
SSID `BAPS-Signage-Setup`. Then remove that fixed profile by UUID. This avoids
removing an unrelated profile with the same name and preserves saved Wi-Fi networks:

```sh
sudo nmcli connection down uuid 72c25775-673a-4b48-bf74-511894293f48
sudo nmcli connection delete uuid 72c25775-673a-4b48-bf74-511894293f48
```

The `down` command may report that the profile is not active. If the helper never
created the profile, `delete` may report that it does not exist. The cached player
continues running independently. Connect Ethernet or use the Pi desktop to select
a saved Wi-Fi network after disabling fallback.

See [NetworkManager shared-network examples](https://networkmanager.pages.freedesktop.org/NetworkManager/NetworkManager/nmcli-examples.html)
for the hotspot's shared IPv4 network behavior.

## Daily use

1. Open `/admin` and sign in.
2. Upload an MP4 and wait for publication to complete.
3. The public page and Pi detect the new version automatically.

The Pi downloads updates in the background. Failed downloads do not replace the
cached video. At first boot with no cached media and no connection, the display
waits for a successful download. Playback continues during later outages.

This release targets one full-screen player. Two-TV spanning or separate left /
right display crops need a follow-up configuration and are not included.

## Update from GitHub

Edit and commit locally, then push to GitHub. On the server:

```sh
git pull --ff-only
docker compose --env-file .env -f docker/compose.yaml up -d --build
```

On the Pi:

```sh
cd /home/admin/bapsclt-signage
git pull --ff-only
sudo systemctl restart bapsclt-signage-agent.service
```

After changing the kiosk launcher, restart the desktop session too. Copy updated
systemd units to `/etc/systemd/system/` and run `daemon-reload` before restarting.
GitHub Actions runs tests; it does not deploy to TrueNAS or the Pi.

## Local development and tests

Use Python 3.12. Install FFmpeg (`ffprobe` is included) on the development host.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

For local server testing, populate `.env`, set `COOKIE_SECURE=false`, and run:

```sh
uvicorn main:app --app-dir server --env-file .env --host 127.0.0.1 --port 8080
```

For a deployment smoke test, upload a real H.264 MP4, verify it starts on `/view`,
then verify it plays on the Pi. Disconnect the Pi from the network and confirm
cached playback continues. Restore connectivity, publish another MP4, and confirm
it replaces the video without touching the display. Physical TV/kiosk behavior
must be checked on your hardware.

## API contract

The Pi sends `Authorization: Bearer <SIGNAGE_API_TOKEN>` to `/api/current` and
`/api/video?version=<sha256>`. Metadata contains `version`, `sha256`, `size` and
`video_url`. `version` is the content hash; before the first upload it is `null`.
Immutable version URLs prevent a download from mixing two publications. Public
playback uses `/public/current` and `/public/video?version=<sha256>`.

Older immutable video files are retained to keep ongoing playback/downloads
stable. Monitor disk usage and delete unused videos through the administrator
library after backing up; this release does not automatically delete published media.

References: [Docker Compose environment files](https://docs.docker.com/compose/how-tos/environment-variables/set-environment-variables/)
and [build paths](https://docs.docker.com/reference/compose-file/build/).
