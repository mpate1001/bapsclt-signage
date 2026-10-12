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

`ADMIN_USERNAME` defaults to `admin`. `MAX_UPLOAD_MB` defaults to 1024 MiB.
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

- `https://bapsclt.datavault.digital/admin`: sign in and upload.
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
stable. Monitor disk usage and plan manual retention maintenance after backing up;
this release does not automatically delete published media.

References: [Docker Compose environment files](https://docs.docker.com/compose/how-tos/environment-variables/set-environment-variables/)
and [build paths](https://docs.docker.com/reference/compose-file/build/).
