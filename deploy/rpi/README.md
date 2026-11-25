# Raspberry Pi 4 installation

Target: **Raspberry Pi 4, Raspberry Pi OS Trixie 64-bit, Python 3.13**, USB webcam. **Bookworm 64-bit / Python 3.11** is also supported. Pi 5 and 32-bit OS images are outside this GPIO profile. The hardware service supports the Pi 4's GPIO layout; a CSI ribbon camera is not yet supported by this portal's USB camera setup. No Pi is connected to this Windows development machine, so physical movement, sensor timing, and Pi FPS must be verified on the appliance.

The current Raspberry Pi OS Desktop, Full and Lite 64-bit images (6 October 2026)
are all based on Trixie ([official OS downloads](https://www.raspberrypi.com/software/operating-systems/)).
Use the OS's built-in `/usr/bin/python3`; no separate Python or camera-driver download
is needed for a standard UVC USB webcam.

## Single-file installer

Copy the generated `setup_peoplesense.py` to the Pi user's home folder and run:

```bash
python3 ~/setup_peoplesense.py
```

The file embeds a clean application snapshot, including logos and deployment
templates, so no separate project-folder transfer is needed. Use Raspberry Pi OS
**Trixie 64-bit**, its default Python 3.13, internet access and a local terminal or
Ethernet. Wire the GPIO with power disconnected before installation; the hardware
service starts the mount at its saved target, initially 90 degrees.

The installer requests sudo, uses the configured Wi-Fi country or asks for its
two-letter code, installs to `/opt/peoplesense`, creates the virtual environment,
initializes the database and downloads both models. Linux package installation is
noninteractive, waits for the package-manager lock and retries downloads; pip
retries slow connections and uses ARM64 wheels for the large native packages.
The installer downloads and builds the pinned GPIO daemon, installs USB camera
utilities, and grants the service user `video`/`gpio` access automatically.

A fresh device selects the first detected USB camera using its actual capture
index, YOLOX-Nano, a 30 FPS preview target and a 4 FPS AI target. Existing camera
selections are preserved on resume. Both downloaded models must load and pass
an inference check, and OpenCV must have its Linux V4L2 backend, before services
start. Camera discovery runs as the service user without opening a video stream;
installation can finish with no webcam plugged in. Plug it in and reload camera
selection when ready. No manual `pip`, model download, service setup or reboot is
needed after a successful installation. It briefly starts the GPIO/web
services and tests the hotspot last, then stops the app. Start `main.py` as
described below, join **PeopleSense-AP**, open **http://10.0.0.1/** and sign in
with the factory credentials to complete browser setup.
Hotspot activation replaces the current wlan0 client connection.

Run `python3 ~/setup_peoplesense.py --check` for platform/bundle checks without
installation, or supply `--wifi-country PH` if the Pi is used in the Philippines.
Failures stop subsequent steps. Re-running the same file resumes an incomplete
installation while preserving the database, pictures and generated secrets; it
refuses to replace an existing installation it did not create. Logs are in
`/opt/peoplesense/logs/setup.log`. GPIO movement, DHT11 readings, client Wi-Fi
access and actual FPS must still be checked on the physical Pi.

## Everyday startup

```bash
cd /opt/peoplesense
python3 main.py
```

`main.py` starts the installed portal, GPIO and hotspot services without installing
packages or running migrations. It requests sudo automatically. Keep its terminal
open; **Ctrl+C stops PeopleSense**. After a reboot, run `main.py` again. The app's
services are not enabled at boot. NetworkManager and Nginx remain OS services;
stopping the app does not disconnect the NetworkManager-owned Wi-Fi AP.

Run `python3 main.py --status` to inspect services. Use a local Pi terminal or
Ethernet; starting the hotspot can disconnect a wlan0 SSH session. After login,
choose **My camera → Start camera** to begin capture.

### Logs in the application folder

Setup and service logs are all in `/opt/peoplesense/logs/`, beside `main.py`:
`setup.log`, `web.log`, `hardware.log`, `hotspot.log`, `pigpio.log`,
`camera-<id>.log`, `nginx-access.log`, and `nginx-error.log`.
Setup creates the directory and permissions automatically before starting services.
Logs append across restarts and are preserved during repairs, updates and factory reset.
An early setup failure (platform check, bundle check, update backup or installer
lock) is recorded in `setup-startup.log` beside the installer, before the
application's log folder exists. The console prints the current log location.

```bash
cd /opt/peoplesense
sudo tail -f logs/web.log logs/hardware.log logs/hotspot.log
```

## Repair or update

Stop `main.py` with Ctrl+C before installation, repair or updates. A shared lock
prevents a launcher and an installer from controlling the same appliance at once.
To restore the current bundle's files, packages, models and service configuration:

```bash
python3 ~/setup_peoplesense.py --repair
```

Repair preserves the database, pictures, Wi-Fi key and existing credentials and
settings. A completed repair leaves the app stopped; start it with `main.py`.

### Update an existing installer-managed device

Copy the newly generated `setup_peoplesense.py` to the Pi, replacing the installer
file in your home folder, then run the same setup command:

```bash
python3 ~/setup_peoplesense.py
```

The installer recognizes an existing PeopleSense bundle and updates it
automatically, including when using `--repair`. The former **"An installation
from another bundle exists"** error no longer blocks the update. The `--update`
option remains accepted for compatibility with older instructions. If an older
installer still shows that error, replace it with this generated file first.

Use a local Pi terminal or Ethernet with internet access. The update stops the
PeopleSense workers, saves a private backup under `/opt/peoplesense-backups/`,
then replaces application files and updates packages, models and the database
schema. The backup includes application source, the database (including committed
WAL data), photos, environment settings and the Wi-Fi encryption key. It excludes
the virtual environment, collected static files and Python caches.

Your existing administrator password, AP name/password, selected USB camera,
model, mode, line and recorded history are preserved. Factory defaults are not
reapplied. After updating, run `python3 /opt/peoplesense/main.py`, reconnect to
your existing AP and use your existing password; press **Start camera** to resume
capture. If the update stops, rerun the
same new installer to resume. The original backup path is retained;
rollback is not automatic. Backup failure stops before application files are
replaced and restarts previously running services. An unmanaged directory or
unrecognized installer marker is still refused.

For a later dependency/model/camera-discovery check, run it with the same account
and inference backend used by the service (no video capture, events or GPIO):

```bash
cd /opt/peoplesense
sudo -u peoplesense env PEOPLESENSE_VISION_BACKEND=litert .venv/bin/python manage.py check_camera_setup
```

After application changes, rebuild the snapshot on the development computer:

```bash
python deploy/rpi/build_installer.py
python -m unittest discover -s deploy/rpi -p 'test_*.py'
```

The bundle excludes development databases, pictures, virtual environments,
downloaded models, Python caches and presentation artifacts. The manual setup
instructions below remain available for installations you manage yourself.

## Wiring

| Device | BCM GPIO | Physical pin |
| --- | --- | --- |
| Horizontal servo signal | GPIO18 | 12 |
| Vertical servo signal | GPIO13 | 33 |
| DHT11 data | GPIO4 | 7 |

Use a separate regulated servo supply matched to your servos; connect its ground to Pi ground (e.g. physical pin 6). Do not power two servos from the Pi's 3.3 V output. Connect DHT11 VCC to 3.3 V (physical pin 1), GND to ground, and its data line to GPIO4. A bare DHT11 needs a 4.7–10 kΩ pull-up from data to 3.3 V; many sensor modules already include it. GPIO signals must remain 3.3 V compatible.

Servo pulse defaults are 1–2 ms with 10–170° travel limits. Test the mount unloaded, then narrow the limits to prevent mechanical collisions. The mount moves to its saved target when the hardware service starts (initially 90°). Center returns both targets to 90°. Mounts with reversed directions can use `PEOPLESENSE_PAN_REVERSED=1` or `PEOPLESENSE_TILT_REVERSED=1`.

## Install the application

Use `/opt/peoplesense` for these service templates, owned by the dedicated `peoplesense` user. Copy this project there without the Windows `.venv` or runtime camera buffers. Keep your database and `runtime/secrets/wifi.key` together if migrating saved AP credentials.

```bash
sudo apt update
sudo apt install python3-venv python3-dev python3-rpi.gpio build-essential ca-certificates libopenblas0 nginx network-manager dnsmasq-base sudo
sudo useradd --system --create-home --groups video,gpio peoplesense
sudo chown -R peoplesense:peoplesense /opt/peoplesense
cd /opt/peoplesense
sudo python3 deploy/rpi/install_pigpio.py
sudo -u peoplesense python3 -m venv --system-site-packages .venv
sudo -u peoplesense .venv/bin/python -m pip install -r requirements.txt
sudo -u peoplesense .venv/bin/python -c 'import django, cv2, numpy, scipy, flatbuffers, ai_edge_litert, gpiozero, pigpio, adafruit_dht, RPi.GPIO, gunicorn'
sudo -u peoplesense .venv/bin/python manage.py migrate
sudo -u peoplesense .venv/bin/python manage.py bootstrap_appliance
sudo -u peoplesense .venv/bin/python manage.py download_models
sudo -u peoplesense .venv/bin/python manage.py collectstatic --noinput
sudo -u peoplesense cp deploy/rpi/.env.example .env.rpi
sudo chmod 600 .env.rpi
```

Edit `.env.rpi`: replace `DJANGO_SECRET_KEY` with a random secret (generate with `.venv/bin/python -c "import secrets; print(secrets.token_urlsafe(48))"`), and add the Pi's actual hostname and LAN IP to `DJANGO_ALLOWED_HOSTS`. The file is a systemd EnvironmentFile; do not prefix lines with `export`.

The single `requirements.txt` automatically selects **LiteRT 2.2.0 ARM64**, OpenCV headless, SciPy, GPIO Zero/pigpio, and Adafruit's DHT driver on the Pi. It does not install full TensorFlow or MediaPipe on Linux, and installs only one OpenCV distribution. EfficientDet uses LiteRT; **YOLOX-Nano · low resource** uses OpenCV DNN directly on one CPU thread, with the official 3.49 MiB ONNX export at 416×416. Choose the model in My camera → Edit camera. Nano needs no additional runtime package and supports both modes through the existing tracker. Start at 30 preview FPS and a modest AI rate (e.g. 4–8 FPS); measured throughput depends on the Pi, power, cooling, webcam, and model. A 60 FPS selection is only a target supported by suitable hardware.

Trixie does not package the pigpio daemon. `install_pigpio.py` downloads the official v79 source, verifies its pinned SHA-256, compiles just the daemon with GCC, and installs `/usr/local/bin/pigpiod`. It does not run the upstream global Python installer. The Python client remains in the virtual environment. The OS supplies the RPi.GPIO extension via `python3-rpi.gpio`; the virtual environment exposes it with `--system-site-packages`. DHT11 uses `use_pulseio=False`, avoiding the obsolete libgpiod v1 PulseIn ABI. The installer checks PeopleSense's imports, both detection models and Django before starting services. It does not run a global `pip check` against unrelated OS packages, which can report missing Flask/type-stub dependencies that PeopleSense does not use.

Compatibility references: [current Raspberry Pi OS](https://www.raspberrypi.com/software/operating-systems/), [Trixie RPi.GPIO for Python 3.13](https://packages.debian.org/trixie/python3-rpi.gpio), [official pigpio v79](https://github.com/joan2937/pigpio/releases/tag/v79). The unified requirements were resolved for ARM64 Python 3.13 (Trixie) and 3.11 (Bookworm), requiring binary wheels for NumPy, SciPy, OpenCV, LiteRT and cryptography. This checks dependency compatibility and wheel availability; actual Pi operation still requires on-device verification.

## Start services

The bundled PWM daemon is restricted to local connections. Disable another pigpiod service first if one already owns port 8888; use only one PWM daemon. Only the hardware worker owns the pins. Django communicates through bounded database mailboxes and never changes GPIO or networking in web requests.

Use a local Pi terminal or Ethernet for initial activation: **wlan0 becomes the hotspot**, replacing its current Wi-Fi client connection. Configure your Wi-Fi country in `sudo raspi-config` first (Localisation Options → WLAN Country). NetworkManager must manage wlan0; do not run hostapd or a second DHCP server on that interface.

```bash
sudo install -d -o root -g root -m 755 /usr/local/libexec
sudo install -o root -g root -m 755 deploy/rpi/peoplesense-hotspot-helper.py /usr/local/libexec/peoplesense-hotspot
sudo install -o root -g root -m 440 deploy/rpi/peoplesense-hotspot.sudoers /etc/sudoers.d/peoplesense-hotspot
sudo visudo -cf /etc/sudoers.d/peoplesense-hotspot
sudo install -d -o peoplesense -g peoplesense -m 750 /opt/peoplesense/logs
sudo cp deploy/rpi/peoplesense-{pigpio,hardware,web,hotspot}.service /etc/systemd/system/
sudo cp deploy/rpi/nginx.conf /etc/nginx/sites-available/peoplesense
sudo ln -s /etc/nginx/sites-available/peoplesense /etc/nginx/sites-enabled/peoplesense
sudo systemctl daemon-reload
sudo systemctl disable peoplesense-pigpio peoplesense-hardware peoplesense-web peoplesense-hotspot
sudo systemctl enable --now NetworkManager nginx
sudo nginx -t
sudo systemctl reload nginx
python3 main.py
```

The supplied `.env.rpi` enables `PEOPLESENSE_HOTSPOT=1` and includes `10.0.0.1` in `DJANGO_ALLOWED_HOSTS`. If upgrading an existing `.env.rpi`, add those values without replacing your secret or other hosts; restart `peoplesense-web` after editing it. The Nginx template includes `10.0.0.1` on port 80. Install the updated template (or add the IP to your customized server block), then reload Nginx.

After hotspot activation, join **PeopleSense-AP** (or your saved AP name) and open **http://10.0.0.1/**. The initial AP is open unless you previously selected AP password protection. Sign in (`admin` / `12345678` on a new installation), then set your administrator password and optional WPA2 AP password in Quick setup. The portal works locally without an internet connection. A phone may label this Wi-Fi as having no internet; keep the Wi-Fi connection to reach the portal.

**My camera** shows the GPIO connection, pan/tilt angles, ambient temperature, humidity, and D-pad. The USB camera remains stopped until you press Start camera. The selector checks the V4L2 USB bus and capture/streaming capabilities, including indices above 5, and lists each physical USB camera once. CSI, Pi codec, virtual/loopback and webcam metadata nodes are excluded. Reload after plugging in or unplugging a camera. Discovery does not start capture, so a running webcam stays listed. If the list is empty, check that the `peoplesense` service user belongs to the `video` group and can read `/dev/video*`.

## Hotspot behavior and verification

NetworkManager creates a persistent, autoconnecting 2.4 GHz AP on **wlan0**, channel 6, at **10.0.0.1/24**. Its shared IPv4 mode provides DHCP/DNS to clients; the portal needs no upstream connection. If an Ethernet uplink is present, NetworkManager may also share its internet connection. The AP does not become the Pi's default route, and existing Ethernet profiles are left untouched. An overlapping `10.0.0.0/24` network on another interface is rejected; change that other network before activating this fixed-address AP.

Saving Wi-Fi settings or Quick setup queues a new apply after **8 seconds**. Reconnect using the new SSID/password and reopen **http://10.0.0.1/**. The worker retries failed updates every 30 seconds and checks the active UUID/address. Identical profiles already active at the correct address do not restart Wi-Fi. An apply failure restores the previous managed AP profile and attempts to restore the previous wlan0 connection. Settings display Pending, Applying, Broadcasting or the failure message; the address stays visible while reconnecting. This is a fixed portal URL, not a captive portal or automatic browser launch.

The service runs as `peoplesense`; only its installed **root-owned** helper can update its fixed AP profile through the restricted sudo rule. The helper cannot accept another interface, command or filesystem path. AP credentials pass over stdin, never command arguments or browser status responses. Django retains encrypted credentials; NetworkManager requires the PSK in its root-only **0600** keyfile at `/etc/NetworkManager/system-connections/peoplesense-ap.nmconnection`. Keep the helper root-owned and do not grant the app unrestricted sudo or NetworkManager permissions.

Check on the wired Pi:

```bash
systemctl status peoplesense-hotspot
sudo tail -f /opt/peoplesense/logs/hotspot.log
nmcli -g GENERAL.CON-UUID,IP4.ADDRESS device show wlan0
curl -I http://10.0.0.1/
```

The active UUID should be `c219e963-2bda-4e69-a191-643d60934007` and the address `10.0.0.1/24`. Check an actual connected client's DHCP lease and login page, then test an SSID/password change and reboot. Wi-Fi transmission and DHCP leases cannot be physically verified on the Windows development machine. Stopping the worker does not tear down the current AP; NetworkManager owns the persistent profile.

DHT11 sampling is every two seconds. Transient checksum errors retry automatically, and readings become unavailable after 15 seconds without a successful sample. These are ambient air readings, separate from thermal readings attached to people detections. Servo commands move 5° per press and wait for the previous movement to settle. Tripwire counting and movement paths pause during rotation. Follow camera movement can realign small rotations; if the line is fixed, counting stays paused until the line is repositioned and saved.

Inspect `sudo tail -f /opt/peoplesense/logs/hardware.log /opt/peoplesense/logs/web.log /opt/peoplesense/logs/hotspot.log` for startup diagnostics. Stop `main.py` before running setup or repair; restart it afterward. This avoids workers using a previous database schema. Setup installs helper updates as root before starting the hotspot worker.

**Factory reset:** use the card at the bottom of Settings, enter the current administrator password, and type `RESET`. All portal data and saved pictures are permanently erased, all sessions expire, and login returns to `admin / 12345678` with first-time setup. The camera stops, the mount returns to its center position, and the running hotspot worker switches to the open `PeopleSense-AP` network after the eight-second apply delay. Reconnect and open **http://10.0.0.1/**. Reset preserves the installed models, `.env.rpi`, system services, GPIO pin configuration, operating system, and non-PeopleSense network profiles. Keep the web/hardware/hotspot services running during reset; the portal manages their command mailboxes without restarting the Pi. If a camera cannot release its files or storage permissions prevent deletion, the form reports the failure and allows a retry.

For production, configure HTTPS in Nginx using your certificate, set `DJANGO_DEBUG=0` and `PEOPLESENSE_PROXY_HTTPS=1`. The included Nginx proxy overwrites `X-Forwarded-Proto` with its own connection scheme; Gunicorn remains bound to localhost. These settings enable the production secure-cookie/redirect behavior behind that trusted proxy. Use an appropriate certificate/hostname for HTTPS; the supplied commissioning profile uses local HTTP at 10.0.0.1. Do not expose the commissioning HTTP server to the internet.

References: [GPIO Zero servo API](https://gpiozero.readthedocs.io/en/stable/api_output.html), [Adafruit DHT examples](https://docs.circuitpython.org/projects/dht/en/latest/examples.html), [LiteRT ARM64 package](https://pypi.org/project/ai-edge-litert/2.2.0/), [NetworkManager shared IPv4](https://networkmanager.dev/docs/api/latest/settings-ipv4.html), [NetworkManager keyfiles](https://networkmanager.dev/docs/api/latest/nm-settings-keyfile.html).
