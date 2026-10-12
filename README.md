# BAPS CLT Signage

A video upload portal for BAPS Charlotte, with full-screen playback on a Raspberry
Pi. The Pi downloads and verifies each published MP4, then plays its local copy.
Saved video continues playing without internet. A protected setup hotspot lets a
phone configure new Wi-Fi when Ethernet and saved Wi-Fi are unavailable.

The current installation runs on **DataVault (TrueNAS)** and
**signage-pi-01 (Raspberry Pi 5)**. Uploading the representative 353 MB video,
full-screen TV playback, offline playback, remote Pi reboot, server restart, and
phone setup using the hotspot's nearby Wi-Fi list have been tested on this setup.
This README covers operation and manual updates first, then installation reference.

## Contents

- [Daily operation](#daily-operation)
- [Connect the Pi to new Wi-Fi](#connect-the-pi-to-new-wi-fi)
- [Installed paths and services](#installed-paths-and-services)
- [Update from GitHub](#update-from-github)
- [Troubleshooting](#troubleshooting)
- [Backups and rollback](#backups-and-rollback)
- [Installation reference](#installation-reference)
- [Local development and API](#local-development-and-api)

## Daily operation

| Page | Who uses it | What it does |
| --- | --- | --- |
| [Admin](https://bapsclt.datavault.digital/admin) | Administrator (`admin`) | Upload, video library, device status, Pi reboot, server restart |
| [Upload](https://bapsclt.datavault.digital/upload) | Shared publisher (`bapsclt`) or administrator | Upload and publish a video |
| [Display](https://bapsclt.datavault.digital/view) | Anyone with the URL | Show the current video only |

Passwords are held in private server configuration and shared separately. The
publisher cannot access admin controls. `/view` and its media are public; do not
publish private video. The homepage redirects to `/view`.

1. Open the upload or admin page and sign in.
2. Select an MP4, then choose **Publish video**.
3. Keep the page open until publication completes.
4. Check the TV to confirm the new video is playing.

A successful upload becomes the current video. There is no playlist editor.
Displays check for updates approximately every 30 seconds; download time also
depends on the file size and connection. Until a new download is verified, the
Pi keeps its previous video. At first boot without a cached video, it waits for a
successful download.

### Video format and upload size

Use an MP4 containing **H.264 video with yuv420p pixel format**. The server checks
it with `ffprobe`; it does not convert unsupported video. Playback is muted for
reliable autoplay. The video fits the screen without stretching. The pointer
hides after three seconds without activity and returns when moved.

The total upload limit is **2048 MiB (2 GiB / 2147483648 bytes)**. The portal sends
**8 MiB chunks**, then the server assembles, validates, and publishes the video.
Each request stays below Cloudflare Free's **100 MB request limit**; you do not
need to split files yourself. Transfer progress is followed by validation and
publication. Temporary failures are retried while the page remains open.
Refreshing or closing the page does not provide automatic upload resumption.

Cancelling during transfer removes that temporary upload. Failed validation does
not replace the current video. Unfinished upload sessions expire after 24 hours.
Keep enough free server storage for the new file alongside retained videos. The
legacy single-request API remains available, but larger Cloudflare uploads should
use the portal's chunked flow.

See [Cloudflare Error 413 and upload limits](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/4xx-client-error/error-413/).

### Admin controls

**Video library:** preview retained files and remove unused videos. The current
video and videos cached by online devices are protected from deletion. Old files
are retained until an administrator deletes them.

**Signage devices:** a recent check-in means the update agent is online. The
cached version shows what it downloaded; neither confirms that the TV is rendering
frames. Verify physical playback when needed.

**Reboot Pi:** temporarily interrupts that display. It is available when the Pi's
fixed reboot helper is active. Requests expire after five minutes and are
acknowledged before reboot.

**Restart signage server:** briefly interrupts the portal and online viewing.
Pi devices continue their cached playback. This gracefully ends the server
process and Docker restarts it. It does not install a newly built image.

## Connect the Pi to new Wi-Fi

When no Ethernet or saved Wi-Fi connection is available for **120 seconds**, the
Pi starts its setup network. A working local connection is sufficient: an internet
outage alone does not cause it to take over the Wi-Fi adapter.

Keep the setup hotspot password and the following URL in a private offline note
so they are available at a new location. Do not put the password in GitHub.

1. On your phone, join **BAPS-Signage-Setup** using its private setup password.
2. If the phone says **No internet**, stay connected to that network.
3. Open **http://10.42.0.1:8091/** in the phone's browser. There is no automatic
   captive-portal popup; enter the address yourself.
4. Choose your Wi-Fi from **Nearby Wi-Fi networks**, then enter its password.
5. Select **Connect display**. Your phone may disconnect as the Pi switches.
6. Wait up to two minutes. If the setup network returns, reconnect to it, reopen
   the page, and retry. A failed connection restores the setup hotspot.

The list shows signal strength and whether a password is needed. Its initial
results were detected before the hotspot started. **Refresh nearby networks**
requests a new scan; some adapters temporarily pause the hotspot while scanning.
If the phone disconnects, reconnect to **BAPS-Signage-Setup** and reload the page.
If scanning is unavailable, manual entry still works.

For a known network missing from the list, choose **Enter a Wi-Fi name manually**
and enter the exact name, including any spaces. Networks that do not broadcast
their name may require separate NetworkManager configuration. For an open network, explicitly select the option
that it has no password. Ordinary WPA personal and open networks are supported;
enterprise login, WEP, WPA3-only networks, and browser sign-in networks need
separate configuration. Unsupported networks in the list cannot be selected.

**Retry saved Wi-Fi networks** explicitly tries saved profiles. It does not
periodically disconnect your phone while you enter details. Connecting Ethernet
automatically stops the setup hotspot. Cached video continues throughout network
changes; new downloads and remote management resume when internet access returns.

Anyone with the setup hotspot password can change the Pi's Wi-Fi settings while
fallback is active. Use a separate setup password, not the admin, publisher, or
device API secret. A USB keyboard and the Pi's TV remain a local recovery option.

## Installed paths and services

| System | Current configuration |
| --- | --- |
| TrueNAS SSH | `mpate100@DataVault` |
| TrueNAS checkout | `/mnt/fastpool/bapsclt-signage/repo` |
| TrueNAS app/container | `bapsclt-signage` |
| Docker image | `bapsclt-signage:local` |
| Ports | Host `8088` → container `8000` |
| Persistent server data | `/mnt/fastpool/bapsclt-signage/data-v2` → `/data`, owner `10001:10001` |
| Public domain | `https://bapsclt.datavault.digital`, through Cloudflare |
| Pi SSH | `admin@signage-pi-01` |
| Pi hardware/desktop | Raspberry Pi 5, Raspberry Pi OS Trixie, Labwc |
| Pi checkout | `/home/admin/bapsclt-signage` |
| Pi video cache | `/home/admin/bapsclt-signage-data` |
| Pi private agent settings | `/etc/bapsclt-signage/pi.env` |
| Pi private hotspot settings | `/etc/bapsclt-signage/wifi.env` |
| Installed Wi-Fi helper | `/usr/local/lib/bapsclt-signage/wifi-setup.py` and `wifi-setup.html` |
| Local player on the Pi | `http://127.0.0.1:8090/index.html` |
| Phone setup on the hotspot | `http://10.42.0.1:8091/` |

The old TrueNAS `app`, `media`, and `data` directories are preserved. The current
image uses `data-v2`. Do not remove those older directories without checking their
contents and backups.

The Pi runs three separate service roles:

- `bapsclt-signage-agent.service` runs as `admin`, downloads video, reports device
  status, and serves the local player. It does not run arbitrary privileged commands.
- `bapsclt-signage-reboot.path` watches the fixed reboot marker. Its root helper,
  `bapsclt-signage-reboot.service`, removes that marker and reboots the Pi.
- `bapsclt-signage-wifi.service` manages NetworkManager as root using installed,
  root-owned copies. The Git checkout is not executed by this privileged helper.

The older `bapsclt-signage-web.service` is disabled; the agent now serves the
player itself. The desktop's `~/.config/labwc/autostart` launches Chromium in kiosk
mode. Keep one signage browser launcher, preserving other desktop entries.

## Update from GitHub

The repository is [mpate1001/bapsclt-signage](https://github.com/mpate1001/bapsclt-signage).
On the Mac, edit and review changes, commit, then push using GitHub Desktop or Git.
Do not commit passwords, `.env`, `pi.env`, `wifi.env`, video, or runtime data.
GitHub Actions runs tests; it does not deploy the server or Pi automatically.

### Update the TrueNAS server

In the TrueNAS shell:

```sh
sudo git -C /mnt/fastpool/bapsclt-signage/repo pull --ff-only &&
sudo docker build \
  -t bapsclt-signage:local \
  -f /mnt/fastpool/bapsclt-signage/repo/docker/Dockerfile \
  /mnt/fastpool/bapsclt-signage/repo
```

Then open **TrueNAS → Apps → bapsclt-signage → Edit**. Preserve private credentials,
ports, and the data mount. **Save a configuration change to recreate the app with
the rebuilt image.** A plain container restart continues using its previous image.

If the app configuration needs no other change, an optional inert environment
value such as `DEPLOY_REVISION` can be set to the new commit from this command:

```sh
sudo git -C /mnt/fastpool/bapsclt-signage/repo rev-parse --short HEAD
```

Changing `DEPLOY_REVISION` under the app's existing `environment` section and
saving forces a configuration recreation; the signage code does not use this
value. Keep `pull_policy: never` for the local image.

Confirm the app is running, then check health and the image IDs:

```sh
curl -sS http://127.0.0.1:8088/health
sudo docker inspect bapsclt-signage --format '{{.Image}}'
sudo docker image inspect bapsclt-signage:local --format '{{.Id}}'
```

Health should return `{"status":"ok"}`. The two image IDs must match. Open the
portal and check the expected behavior. If the IDs differ, the existing container
has not been recreated from the new image.

This installation is managed by **TrueNAS Apps custom YAML**. Edit it through
Apps, not the generated `/mnt/.ix-apps/.../rendered/docker-compose.yaml` file.
Generated files can be replaced by TrueNAS.

### Update the Pi agent

On the Pi:

```sh
git -C /home/admin/bapsclt-signage pull --ff-only &&
sudo systemctl restart bapsclt-signage-agent.service
```

Keep `/etc/bapsclt-signage/pi.env`. An existing value overrides the code's defaults.
For the current upload limit it must contain:

```text
SIGNAGE_MAX_VIDEO_BYTES=2147483648
```

After changing service units, reinstall the relevant units, run
`sudo systemctl daemon-reload`, and restart that service. A normal agent code
update does not require reinstalling the reboot helper.

### Update the Wi-Fi setup helper

On the Pi, update the checkout, then copy only the helper, page, and unit. Preserve
`/etc/bapsclt-signage/wifi.env`; never replace it with the sample on an update.

```sh
cd /home/admin/bapsclt-signage
git pull --ff-only
sudo install -o root -g root -m 755 pi/wifi-setup.py /usr/local/lib/bapsclt-signage/
sudo install -o root -g root -m 644 pi/wifi-setup.html /usr/local/lib/bapsclt-signage/
sudo install -o root -g root -m 644 pi/systemd/bapsclt-signage-wifi.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart bapsclt-signage-wifi.service
```

The signage agent and TrueNAS server need no restart for a Wi-Fi helper update.
A helper restart may interrupt an active phone setup connection briefly.

### Update the desktop launcher

Changes to kiosk arguments take effect when Chromium is relaunched or the desktop
session restarts. Preserve the existing launcher in Labwc autostart and avoid
adding another watchdog. Changing `pi/kiosk.sh` only affects an installation that
actually launches that script; the current Pi has an inline Chromium launcher in
autostart. A desktop restart or Pi reboot briefly interrupts TV playback.

## Troubleshooting

### Server or login problems

On TrueNAS:

```sh
curl -sS http://127.0.0.1:8088/health
sudo docker logs --tail 50 bapsclt-signage
```

For incorrect credentials, check the private values in **Apps → Edit**, confirm
the correct portal, save changes, and sign in again. Both `PUBLISHER_USERNAME` and
`PUBLISHER_PASSWORD` must be set to enable the shared publisher; its username must
differ from `ADMIN_USERNAME`. Publisher access is only through `/upload`. Sessions
expire after eight hours. Changing a publisher password alone does not end its
existing sessions; disabling the publisher or changing its username invalidates
its access. Rotating `SESSION_SECRET` invalidates all sign-in sessions. Never
paste real credentials into logs, Git, or support screenshots.

If the portal still shows 1024 MB, verify `MAX_UPLOAD_MB: '2048'` in the app and
confirm the container uses the rebuilt image. Do not disable Cloudflare protection
to accommodate larger uploads; use the chunked portal flow.

### Pi offline, not updating, or blank

On the Pi:

```sh
systemctl status bapsclt-signage-agent.service --no-pager -l
sudo journalctl -u bapsclt-signage-agent.service -n 30 --no-pager -l
curl -sS http://127.0.0.1:8090/state.json
```

A version in local `state.json` confirms a cached video exists. It does not prove
Chromium or the TV is playing. Check the physical TV, kiosk launcher, and HDMI
connection too. A cache version of `null` means no verified video is available.

For API **401**, privately compare the Pi's `SIGNAGE_API_TOKEN` with the server's
value; restart the agent after editing its env file. Do not expose the values to
compare them. Earlier Cloudflare **403** responses to Python's default user-agent
were addressed by the agent's `BAPSCLT-Signage/1.0` user-agent. Pull the current
agent and check Cloudflare events if a 403 returns; do not turn off security rules
without identifying the actual block.

### Phone setup network missing or scan unavailable

```sh
sudo systemctl status bapsclt-signage-wifi.service --no-pager -l
sudo journalctl -u bapsclt-signage-wifi.service -n 30 --no-pager -l
systemctl is-active NetworkManager
nmcli device status
busctl --version
```

The hotspot appears only after the fallback delay with no other connected Wi-Fi
or Ethernet network. A working network with no internet will not trigger it.
Check the configured `WIFI_INTERFACE`, private hotspot password, and Wi-Fi country
setting. The helper requires NetworkManager to manage the adapter and
`dnsmasq-base` for the shared network. Scan freshness uses systemd's `busctl`.
If a scan is unavailable, enter the Wi-Fi name manually. If refresh disconnects
the phone, rejoin the setup hotspot and reload the page.

Use a keyboard and the Pi desktop's network settings if needed. Do not reset all
NetworkManager profiles or delete saved networks as a troubleshooting shortcut.

### Keyring prompt, visible pointer, or jitter

The current inline Chromium launcher includes `--password-store=basic` to avoid
the desktop keyring prompt. If replacing that launcher, retain the flag. The
repository's generic `pi/kiosk.sh` currently does not include it.

Idle pointer hiding is implemented by the display pages. Pull updated files and
reload the page if needed; movement makes the pointer visible again.

Judge video smoothness on the physical HDMI TV. Remote desktop can look jittery
even when physical playback is acceptable. For a physical playback issue, inspect
the source codec, dimensions, frame rate, and bitrate before changing the player.
The server image includes `ffprobe`; do not assume changing upload size improves
playback performance.

## Backups and rollback

Back up the complete TrueNAS data directory
`/mnt/fastpool/bapsclt-signage/data-v2` and the private Apps configuration containing
credentials. The directory holds videos, current publication metadata, device
records, and upload state. Use a consistent dataset snapshot or stop the app while
copying the complete directory so SQLite and publication files agree. Protect
backups containing secrets.

On the Pi, preserve both private env files, `/etc/NetworkManager/system-connections`
(root-owned Wi-Fi credentials), the cached data directory, service definitions,
and `~/.config/labwc/autostart`. Record the last working Git commit before updates.

For a code rollback, select a known working Git ref, rebuild/recreate the server
image or reinstall the Pi helper copies as appropriate, and preserve data and
credentials. Do not wipe `data-v2`, delete Docker volumes, or replace private env
files with examples. Older code may not understand newer state; review the change
before rolling back a database-related update.

### Disable the Wi-Fi fallback

Disable the helper first, then inspect its reserved profile:

```sh
sudo systemctl disable --now bapsclt-signage-wifi.service
sudo nmcli -f connection.id,connection.uuid,802-11-wireless.mode,802-11-wireless.ssid connection show uuid 72c25775-673a-4b48-bf74-511894293f48
```

Confirm the profile is `bapsclt-signage-setup`, mode `ap`, SSID
`BAPS-Signage-Setup`. Then remove only that fixed UUID:

```sh
sudo nmcli connection down uuid 72c25775-673a-4b48-bf74-511894293f48
sudo nmcli connection delete uuid 72c25775-673a-4b48-bf74-511894293f48
```

`down` may report that the profile is not active; `delete` may report it does not
exist if it was never created. Preserve other saved profiles. Connect Ethernet or
select saved Wi-Fi locally after disabling fallback. The cached player remains
independent of this helper.

## Installation reference

These sections are for a fresh device or rebuilding the setup. The current server
and Pi are already installed. Preserve existing content before changing paths or
services; do not overwrite private env files with placeholders.

### TrueNAS Apps custom YAML

Clone the repository into `/mnt/fastpool/bapsclt-signage/repo` if that path does not
already exist. Create `/mnt/fastpool/bapsclt-signage/data-v2` only if it is new;
it must be writable by UID/GID `10001:10001` (the container user). The current
installation uses directory mode `700`. Build the image with the update commands
above, then use a custom YAML app named `bapsclt-signage`.

The following is a **template**. Replace every private placeholder in TrueNAS,
not in the repository; preserve current values on an existing installation.
`SESSION_SECRET` and `SIGNAGE_API_TOKEN` must each have at least 32 characters.
Generate independent values privately, for example with
`python3 -c 'import secrets; print(secrets.token_urlsafe(48))'`.

```yaml
services:
  bapsclt-signage:
    image: bapsclt-signage:local
    pull_policy: never
    container_name: bapsclt-signage
    restart: unless-stopped
    environment:
      ADMIN_USERNAME: 'admin'
      ADMIN_PASSWORD: '<private admin password>'
      SESSION_SECRET: '<private random session secret>'
      SIGNAGE_API_TOKEN: '<private random device token>'
      PUBLISHER_USERNAME: 'bapsclt'
      PUBLISHER_PASSWORD: '<private publisher password>'
      COOKIE_SECURE: 'true'
      MAX_UPLOAD_MB: '2048'
      SIGNAGE_ALLOW_RESTART: 'true'
      DATA_DIR: '/data'
    ports:
      - '8088:8000'
    volumes:
      - /mnt/fastpool/bapsclt-signage/data-v2:/data
```

Keep the same device token in the Pi's private agent configuration. Blank both
publisher fields to disable that account. Use one server process/replica, as the
Dockerfile configures. Restart controls require the configured Docker restart
policy. `COOKIE_SECURE=true` is required for the public HTTPS portal; use `false`
only for a private localhost HTTP test.

Configure the existing Cloudflare tunnel/reverse proxy to reach the host's port
8088. The public hostname is set in that proxy, not in Python. Keep any origin
access on a trusted network.

### Optional generic Docker Compose deployment

`docker/compose.yaml` is a separate reference for another Docker host. It uses
host loopback **8080** and a named data volume; those differ from the current
TrueNAS Apps deployment. Do not use these commands to replace the installed Apps
configuration inadvertently.

From the repository root, create `.env` from `.env.example` only if `.env` does not
already exist, then privately fill its credentials:

```sh
if [ ! -e .env ]; then cp .env.example .env; chmod 600 .env; fi
docker compose --env-file .env -f docker/compose.yaml up -d --build
```

Set `BIND_ADDRESS` and `PORT` as required by that host's reverse proxy. Its named
volume survives image rebuilds. Do not run `docker compose down -v` unless you
intend to erase its stored data.

### Pi agent and kiosk

These paths assume the user `admin`, Raspberry Pi OS Desktop with Labwc, and a
checkout at `/home/admin/bapsclt-signage`. Clone into a new path only; inspect an
existing checkout before replacing it. Python 3 and Chromium are required.
Enable desktop auto-login for automatic kiosk startup.

From the Pi repository root:

```sh
sudo apt update
sudo apt install -y chromium
sudo install -d -o root -g root -m 755 /etc/bapsclt-signage
if ! sudo test -e /etc/bapsclt-signage/pi.env; then
  sudo install -o root -g root -m 600 pi/pi.env.example /etc/bapsclt-signage/pi.env
fi
sudo nano /etc/bapsclt-signage/pi.env
```

Set the public `SIGNAGE_URL`, matching private `SIGNAGE_API_TOKEN`, data path, and
`SIGNAGE_MAX_VIDEO_BYTES=2147483648`. For a new cache directory:

```sh
sudo install -d -o admin -g admin -m 700 /home/admin/bapsclt-signage-data
sudo install -o root -g root -m 644 pi/systemd/bapsclt-signage-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bapsclt-signage-agent.service
```

If changing the data path or username, update the service's `User`, working path,
and `ReadWritePaths` too. Do not point a new installation at unverified old state.
Stop any older local web service before starting this agent; port 8090 must be
available.

For a **fresh** desktop installation, add a single launcher to
`~/.config/labwc/autostart` while preserving other entries:

```sh
sh /home/admin/bapsclt-signage/pi/kiosk.sh &
```

Run the script from the graphical desktop to test it. The existing deployment
already has an inline launcher; replace its signage block if migrating to the
script rather than adding a second one. Add `--password-store=basic` to the chosen
Chromium command if the keyring dialog appears. Restart the desktop session to
apply launcher changes. Other desktops need their own session autostart setup.

### Pi remote reboot helper

The default paths assume `/home/admin/bapsclt-signage-data`. For another data path,
update both helper units before installing them. From the Pi checkout:

```sh
sudo install -o root -g root -m 644 pi/systemd/bapsclt-signage-reboot.service /etc/systemd/system/
sudo install -o root -g root -m 644 pi/systemd/bapsclt-signage-reboot.path /etc/systemd/system/
sudo rm -f /home/admin/bapsclt-signage-data/reboot.request
sudo systemctl daemon-reload
sudo systemctl enable --now bapsclt-signage-reboot.path
sudo systemctl restart bapsclt-signage-agent.service
```

The removal clears only a stale reboot request before activation. The agent
writes a marker for an acknowledged, unexpired request; the root helper performs
the fixed reboot operation. It is not a remote shell.

### Pi Wi-Fi fallback helper

First verify prerequisites on the Pi:

```sh
systemctl is-active NetworkManager
nmcli --version
busctl --version
nmcli -f GENERAL.DEVICE,GENERAL.TYPE,GENERAL.STATE device show wlan0
```

Continue when NetworkManager is active and manages the Wi-Fi adapter. `busctl`
comes with systemd on Raspberry Pi OS and confirms scan freshness. Set the correct
Wi-Fi country using Raspberry Pi OS tools; this helper preserves it. If another
manager owns the adapter, configure that separately before enabling fallback.

Install `dnsmasq-base` for NetworkManager's shared IPv4 network, rather than adding
a competing DHCP daemon:

```sh
sudo apt update
sudo apt install -y network-manager dnsmasq-base
```

From `/home/admin/bapsclt-signage`, install root-owned helper copies and create the
private settings only if they do not exist:

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

Use `WIFI_INTERFACE=wlan0` unless the adapter has another name,
`WIFI_FALLBACK_SECONDS=120`, and a unique `WIFI_HOTSPOT_PASSWORD` of **12–63 printable
ASCII characters**. Replace the sample password. Keep the file root-owned with
mode `600`; do not commit it. NetworkManager stores saved Wi-Fi credentials in
root-owned connection profiles. The page does not store passwords in the browser.

```sh
sudo install -o root -g root -m 644 pi/systemd/bapsclt-signage-wifi.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bapsclt-signage-wifi.service
```

This feature requires no TrueNAS change. See
[NetworkManager shared-network examples](https://networkmanager.pages.freedesktop.org/NetworkManager/NetworkManager/nmcli-examples.html).

### Hardware checks after a fresh installation

Have the TV, phone, and a keyboard available. Confirm normal playback and uploads
first. Disconnect Ethernet and make the saved Wi-Fi unavailable. For an intentional
local fallback test, `sudo nmcli device disconnect wlan0` disconnects Wi-Fi;
**expect SSH over Wi-Fi to drop**. Leave the Wi-Fi radio enabled so it can offer
the hotspot; do not use `nmcli radio wifi off`. Do not run that test without a
local recovery path. Wait for the fallback delay, join the hotspot, select working Wi-Fi, and
verify reconnection. Try an incorrect password and confirm the hotspot returns.

Confirm cached video continues during a network outage. Test remote Pi reboot
and server restart when a brief interruption is acceptable; check the actual TV
after recovery. These tests require hardware and are separate from automated
software tests.

## Local development and API

### Repository layout

- `server/`: FastAPI server, templates, credentials/roles, chunked publication,
  device management, and persistent data.
- `pi/`: offline player/agent, kiosk launcher, Wi-Fi setup page/helper, and units.
- `docker/`: nonroot image with FFmpeg and optional generic Compose reference.
- `.env.example`, `pi/pi.env.example`, `pi/wifi.env.example`: examples without
  deployment secrets.
- `tests/`, `.github/workflows/tests.yml`: automated checks.

Use Python 3.12 and FFmpeg (`ffprobe` included) for development:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

For a private local server test, populate `.env`, set `COOKIE_SECURE=false`, then:

```sh
uvicorn main:app --app-dir server --env-file .env --host 127.0.0.1 --port 8080
```

Restore secure cookies for public HTTPS use. Automated tests do not substitute
for physical TV, Wi-Fi adapter, and phone checks.

### API summary

The Pi sends `Authorization: Bearer <SIGNAGE_API_TOKEN>` to `/api/current` and
`/api/video?version=<sha256>`. Metadata includes `version`, `sha256`, `size`, and
`video_url`; `version` is the content hash and is `null` before the first upload.
Immutable version URLs keep a download consistent during publication. Public
playback uses `/public/current` and `/public/video?version=<sha256>`.

The local player reads `/state.json` and the verified cached video on loopback
port 8090. Device heartbeat and fixed reboot commands use the token-protected
server API. The separate Wi-Fi setup page uses a local CSRF token and is served
only on the setup interface, not through the public admin portal.

References: [Docker Compose environment files](https://docs.docker.com/compose/how-tos/environment-variables/set-environment-variables/)
and [Docker build paths](https://docs.docker.com/reference/compose-file/build/).
