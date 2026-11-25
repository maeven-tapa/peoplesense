# PeopleSense device portal guide

A Django portal for **one local USB camera**, with a router-style login and first-use setup. OpenCV and lightweight person detection count confirmed crossings of a line drawn in the browser.

## Install on Raspberry Pi OS

Use a **Raspberry Pi 4 with Raspberry Pi OS Trixie 64-bit** (the current OS,
Python 3.13). Bookworm 64-bit / Python 3.11 is also supported. Copy
`setup_peoplesense.py` to the Pi, open a terminal in that folder, and run:

```bash
python3 setup_peoplesense.py
```

This one file contains the application. It installs Linux and Python packages,
downloads both detection models, initializes the database, and checks the web,
GPIO and hotspot services before leaving the app stopped. Keep internet connected
until it finishes. It requests
sudo and asks for the Wi-Fi country if unset. Use a local terminal or Ethernet:
the hotspot replaces the Pi's wlan0 Wi-Fi connection. Re-run the same command to
resume an interrupted installation.

## Run on the Pi

After setup, start PeopleSense with:

```bash
cd /opt/peoplesense
python3 main.py
```

Keep the terminal open. **Ctrl+C stops the app.** The app waits for `main.py`
after each reboot; the setup script is only for installing, updating or repairing.
You can also run the project's `main.py` from another folder; it controls the
installation in `/opt/peoplesense` and requests sudo automatically.

While the app runs, join **PeopleSense-AP**, open **http://10.0.0.1/** and log in
with **admin / 12345678**. Complete browser setup and change the password. Select
the USB camera and press **Start camera**; Tripwire needs a saved counting line.

Stop `main.py` before maintenance. To repair the current installation, run
`python3 setup_peoplesense.py --repair`. This restores its files, dependencies and
services while preserving data, passwords and settings.

For an existing installer-managed device, copy the new file to the Pi and run
`python3 setup_peoplesense.py`. It detects a changed bundle, backs up the installation and preserves
your password, camera configuration and recorded data. See the
[Pi installation guide](../deploy/rpi/README.md) for wiring and service details.

All PeopleSense logs are together in `/opt/peoplesense/logs/`: `setup.log`,
`web.log`, `hardware.log`, `hotspot.log`, `pigpio.log`, `camera-<id>.log`, and
the Nginx access/error logs. To follow the web log from the application folder:

```bash
sudo tail -f logs/web.log
```

Diagnostic logs are preserved during repairs, updates and factory reset.
If setup stops before it can prepare that folder, its error is saved in
`setup-startup.log` beside `setup_peoplesense.py` instead. The installer prints
the actual log location at startup and on failure.

## Start on Windows

For Raspberry Pi 4 deployment, wiring, servo calibration, DHT11 readings, and systemd services, see [the Pi installation guide](../deploy/rpi/README.md). The pin map is horizontal servo GPIO18 / pin 12, vertical servo GPIO13 / pin 33, and DHT11 GPIO4 / pin 7. My camera includes a pan/tilt D-pad above Device information; GPIO controls remain unavailable on Windows.

After the first-time commands below, run `./start.ps1` and open **http://127.0.0.1:8000/**.

- Username: **admin**
- Initial password: **12345678**

First login opens quick setup. Choose your settings or select **Skip for now** to open the dashboard for the current login session. Initialization never resets an existing password or completed setup.

For a fresh checkout:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py bootstrap_appliance
.\.venv\Scripts\python.exe manage.py download_models
.\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8000
```

## Dependencies

`requirements.txt` is the only dependency list. It selects LiteRT and headless
OpenCV on Linux, MediaPipe and OpenCV contrib on Windows/macOS, and GPIO packages
only on ARM64 Linux. NumPy and SciPy are needed by person tracking. The Pi
installer also installs OS packages and creates `/opt/peoplesense/.venv` using
the OS Python; never copy a Windows virtual environment to Linux. Installing
Python requirements alone does not configure GPIO, the hotspot or boot services.

Camera selection lists only connected USB cameras, using their actual capture indices (including indices above 5). Windows checks USB device paths to exclude virtual cameras. Linux checks the USB bus and each V4L2 node's capture/streaming capabilities, excludes CSI, codec, loopback and metadata devices, and lists each physical camera once. Discovery reads metadata without starting capture, so running cameras stay listed. Reload after connecting or disconnecting a webcam; the Linux service account needs access to `/dev/video*` through the `video` group.

## Quick setup

1. **Administrator password:** replace the initial password. Django hashes and validates it. Later changes require the current password. The session stays signed in after a change.
2. **Wi-Fi AP configuration:** choose a name with 1–32 UTF-8 bytes and an optional password with 8–63 printable ASCII characters. On Raspberry Pi 4, the installed hotspot service broadcasts the saved AP at **10.0.0.1** and applies changes after an eight-second reconnect delay. **This Windows build saves the configuration without broadcasting Wi-Fi.** See the [Pi installation guide](../deploy/rpi/README.md).
3. **USB camera:** select a connected USB webcam from the list. Set its name and doorway location. Selecting the camera stops capture and takes you to mode and line setup.
4. **Camera mode & line:** Tripwire is selected by default. Start the preview, choose Draw new line, click A then B, and check the IN arrow. Drag an endpoint or the middle to adjust it, save, then confirm and finish. Alternatively choose Surveillance, set its capture interval, save the mode, and confirm; no counting line is required.

Setup resumes where you left off. **Skip for now** preserves that progress and keeps Quick setup available in the sidebar. The skip lasts until logout; the next login offers unfinished setup again. A new camera selection requires another line confirmation. Settings has separate cards for Wi-Fi AP configuration, the administrator password, and the time zone, plus a link to camera controls.

Only the selected camera appears in previews, events, reports, occupancy, and alerts. Multi-camera registration, external event ingestion, device keys, network-camera connection options, and the Django admin URL have been removed from the portal. Earlier cameras and events stay preserved in the database as archived history. Their workers are revoked by initialization. Initialization selects an existing local USB camera or creates the one device configuration.

## Camera modes

There are exactly two modes. Select them under **My camera → Camera mode & controls**, then press **Save camera mode**. The camera remains stopped until you start it.

- **Tripwire (default):** records Entry and Exit crossings. The line is neutral at rest, flashes blue for a confirmed IN crossing and red for OUT for approximately three seconds.
- **Surveillance:** saves an annotated JPEG as soon as a person is detected, then at most once per configured interval while people remain visible. Set seconds (30 = 30 seconds, 60 = one minute; supported range 1–86,400). Empty frames do not create captures. The interval is measured from the last saved photo and survives worker restarts. Presence events record the number visible in each photo without changing entries, exits, or occupancy.

Tripwire preview cards on Overview, My camera, and the line setup step include a **DEBUG → Draw movement line** toggle, off by default. It draws yellow paths from each confirmed track's observed foot points; predicted positions never extend a path. Paths use at most 64 samples and eight seconds per person, with at most 50 tracks. Disappeared people are hidden immediately, long observation gaps break the path, and tracking resets, camera motion, configuration changes, and restarts cannot join unrelated track IDs. This visualization does not alter counting or save photos. A second fixed 256 KiB lease-scoped IPC slot holds only the latest paths; the browser requests paths only while the toggle is on and the tab is visible, at up to four updates per second. The camera feed keeps its separate 30/60 FPS target.

**Capture** on the live preview saves the latest camera JPEG immediately in either mode, including empty scenes. Manual photos are labeled in Captures and Detection activity. They do not change entry/exit totals, do not save the browser’s editable line or movement overlay, and do not delay the surveillance interval. The button is unavailable until a live frame is ready; stopped, revoked, or reconfigured camera frames are rejected.

Photos appear under **Captures** and through **View photo** in Detection activity. They are stored locally under `runtime/captures`, outside public static files, and can only be read through administrator-authenticated routes. No face recognition or video recording is performed. Photos persist on disk. Switching modes clears tracking baselines and rejects pending detections from the previous configuration.

## Overview and surveillance stay time

The overview has three equal-width cards on desktop: occupancy history, temperature, and camera preview. The camera selector preserves the reporting period and switches the page to the selected camera’s mode. This single-device portal lists only its configured camera.

Tripwire shows People inside and Total entries. Surveillance shows Current people in area (visible tracks in the running camera) and Average stay in area. Stay time is measured continuously from each anonymous track’s first observation to its last observed frame, independently of the capture interval. Tracks require three observations before contributing to average stay. The average includes ongoing and completed tracked visits last seen in the selected reporting period; no tracking history shows a dash. Short gaps are tolerated by the tracker, and tracking loss or camera restart starts a new visit. This is a camera-visible duration estimate, not an identity-based measurement of a person’s full visit.

Surveillance’s history chart shows the peak observed people per time bucket, sampled every five seconds and whenever the visible count changes. The preview streams continuously at its selected target; metrics refresh every five seconds. Stops and stale leases cannot add stay records or observations. New history is collected from this update onward; earlier snapshots cannot reconstruct stay durations.

## Counting and camera movement

A tracked person crossing toward IN creates one Entry; crossing back creates one Exit. Occupancy is cumulative entries minus exits, clamped to zero. Remaining visible on one side creates no additional count. Entry and exit totals remain cumulative audit records.

Two observed frames on the new side and three track observations are required. Motion prediction, one-to-one assignment, tolerance, and cooldown reduce duplicate counts. Unobserved predictions never create crossings. Newly visible tracks establish a baseline. Worker leases and configuration versions reject duplicate workers and stale crossings after a pause or line change.

Arrow keys move A, Shift+arrow keys move B, and Ctrl+arrow keys (Command on Mac) move the whole line. Normalized coordinates preserve the image location when the browser resizes.

**Follow camera movement** aligns background features against a fixed reference, excluding detected people. It follows small pans, rotations, and zoom changes. Counting pauses during movement and resumes after 0.75 seconds of settling with fresh track baselines. Lost alignment, an obscured doorway, or the line leaving the view pauses counting until you reposition and save it. Check the line after every restart or movement while capture was stopped. Large movements and depth/parallax require manual calibration. Turn the option off for a fixed camera or blank background.

Heavy occlusion, crowding, poor lighting, missed detections, and low FPS can cause missed crossings or track ID changes. Validate accuracy at the doorway. RGB video does not measure body temperature; thermal readings require suitable hardware. Tracking is anonymous and does not identify faces.

## Models and local processing

| Model | Size | Use |
|---|---:|---|
| EfficientDet-Lite0 INT8 | 4.39 MiB | Default person detector |
| YOLOX-Nano ONNX | 3.49 MiB | Low-resource alternative, 416×416 input, OpenCV CPU |

EfficientDet comes from [Google's official model storage](https://developers.google.com/edge/mediapipe/solutions/vision/object_detector); YOLOX-Nano comes from [Megvii's official ONNX release](https://github.com/Megvii-BaseDetection/YOLOX/tree/main/demo/ONNXRuntime). The downloader pins SHA-256 hashes and records provenance. YOLOX-Nano runs directly through OpenCV DNN with one CPU thread, bounded person-only postprocessing, and no PyTorch, Ultralytics, ONNX Runtime, or TensorFlow dependency. EfficientDet uses MediaPipe on Windows and LiteRT on the Pi. Models are ignored by Git and downloaded on each new installation.

Select **YOLOX-Nano · low resource** in My camera → Edit camera. SSD MobileNet is removed; existing SSD selections migrate to YOLOX-Nano. The current EfficientDet selection stays unchanged. Both detectors use the same anonymous tracker, tripwire counting, movement paths, and surveillance capture logic. Nano's small model size does not guarantee a particular Pi inference rate; check measured AI FPS at the doorway. Preview continues independently at its selected target.

Run `python -m vision.smoke_detector` to obtain the public test image, then `python -m vision.benchmark_models` to compare the installed models. Results are generated in `artifacts/model-resource-comparison.json`; they depend on hardware and scene and are not full camera-worker RAM figures.

A separate OpenCV process captures and processes video. Page loads never open the webcam. The latest preview JPEG uses a fixed 256 KiB memory-mapped buffer under `runtime/cameras`; Surveillance additionally stores interval photos under `runtime/captures`. No video archive is recorded. Preview and control routes require administrator login. Stop camera revokes its counting lease. The smoke-pipeline commands below generate isolated verification fixtures and reports when needed.

## Verification and deployment

```powershell
.\.venv\Scripts\python.exe manage.py test
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py makemigrations --check --dry-run
.\.venv\Scripts\python.exe -m vision.smoke_pipeline
.\.venv\Scripts\python.exe -m vision.smoke_pipeline --model yolox_nano
```

Tests cover login/default credentials, password changes and session continuity, ordered setup, AP-name validation, camera isolation, removed endpoints, CSRF, metrics/exports, duplicate suppression, camera alignment, stale worker/configuration races, two-mode validation, surveillance timing and JPEG generation, private capture access, and direction feedback.

For deployment, set `DJANGO_DEBUG=0`, a strong `DJANGO_SECRET_KEY`, allowed hosts, and HTTPS. The development server binds to 127.0.0.1. Administrator login is required for the dashboard, including when quick setup is skipped.

## Smooth preview and memory

Both modes offer 30 FPS (default) and 60 FPS under **Camera mode & controls → Preview speed**. Save to apply. The webcam is requested to provide MJPEG at 640×480 and the selected frame rate; unsupported camera modes fall back to the actual hardware rate. Live frames remain at most 640×480 with aspect ratio preserved. The display shows measured **Preview FPS** separately from **AI FPS**.

Capture and JPEG publishing run independently of inference. AI detection stays at its separately configured target (default 8 FPS), so slow inference does not stall the preview. Track boxes update at the AI rate. The worker uses one OpenCV CPU thread and caps native numerical thread pools unless the operator explicitly configures them, avoiding excess thread-stack memory. A one-frame queue drops stale frames instead of growing; each client streams only the latest JPEG. A fixed 256 KiB IPC slot avoids writing a new JPEG file every frame. Hidden browser tabs disconnect previews, and camera stop/restart closes streams by lease. File playback retains its sampling behavior and is limited by its source/inference rate.

60 FPS requires camera support and sufficient CPU/USB/browser performance; it is a target, not a guaranteed or interpolated rate. The model file size is not its RAM use: MediaPipe, model tensors, OpenCV, tracking, and Django add memory beyond the bounded preview buffer. Use 30 FPS and a modest AI target for lower CPU use; choose the model based on the local benchmark and doorway accuracy.

Run `.\.venv\Scripts\python.exe -m vision.benchmark_preview` to benchmark synthetic 640×480 capture with the real installed models. Results report published FPS, AI FPS, Windows working-set RAM, and buffer size without opening the webcam or creating detection events.

The preview benchmark generates `artifacts/preview-performance.json` from synthetic capture with the installed models. Run `python -m vision.smoke_detector` first if the test image is missing. Measure physical webcam performance on the target device; camera FPS and whole-worker memory depend on hardware and scene.

## Device settings

Settings provides **Device configuration**, **Change administrator password**, and **Time & region** cards, each with its own save button. Choose a supported IANA time zone to update chart labels, portal dates, event/capture timestamps, and CSV headers and timestamps. Events remain stored in UTC. Open dashboards refresh when their effective time zone changes.

**My camera → Select camera** changes the active appliance's USB webcam input from the connected-device list, without adding another portal device. Saving the current USB input keeps the live worker running. A different input stops the feed, preserves all historical records and pictures, and retains the mode/model/capture interval. Start the new preview afterward. In Tripwire mode, counting stays paused until the line is repositioned and saved, including across worker restarts. Surveillance requires no line. **Device information** now appears in Settings with the camera name, IP address, location, processing, thermal sensor status, total entries, and latest heartbeat. Compact sidebar navigation remains scrollable with its scrollbar hidden.

The Wi-Fi card allows an AP name and optional password. With protection selected, a blank password keeps the saved password; unchecking protection removes it. Existing passwords are never filled into browser fields or returned through the dashboard API. Passwords use [Fernet authenticated encryption](https://cryptography.io/en/latest/fernet/). By default a random key is created atomically in `runtime/secrets/wifi.key`, separate from the database; keep that key with device backups. Deployment can instead supply a Fernet key through `PEOPLESENSE_AP_SECRET_KEY`. Changing or losing the key requires re-entering the saved AP password.

The Pi hotspot service uses NetworkManager on wlan0 with a fixed **10.0.0.1/24** address, DHCP/DNS and boot autoconnect. Join **PeopleSense-AP** (or your chosen SSID) and open **http://10.0.0.1/**. Nginx and Django allow that address. Saving the AP queues a delayed update, with status/error feedback. Only a root-owned, restricted helper changes the fixed AP; Django never executes privileged commands in a web request. The helper validates settings, stores NetworkManager's PSK in a root-only keyfile, avoids restarting an unchanged active AP, and restores the previous profile on failure. Installation and actual Wi-Fi commissioning are described in [deploy/rpi/README.md](../deploy/rpi/README.md). Windows does not execute the adapter.

Administrator password changes require the current password, matching new passwords, and Django password validation. Successful changes preserve the current login session and complete the initial password step if it was still pending. The Settings page no longer offers the building configuration form.

**Factory reset** is a separate card at the bottom of Settings. Expand **Reset device…**, enter the current administrator password, and type `RESET`. The authenticated, CSRF-protected POST stops all camera workers and permanently removes all captures (including orphan and unfinished files), preview buffers and old runtime camera logs, detections, counts, stays, observations, alerts, camera/AP/time-zone settings, portal accounts, sessions, and administrator history. It restores `admin / 12345678`, first-time setup, the default USB camera in Tripwire mode, and `PeopleSense-AP` without an AP password. On the Pi the existing hotspot worker applies the defaults after the usual eight-second reconnect delay at **http://10.0.0.1/**, and the hardware worker returns the mount to its center position.

The reset gate pauses other portal requests, waits for camera processes to release their files, rejects deletion paths outside the appliance runtime folder or overlapping models/database/worker locks, and reports cleanup failures instead of claiming success. Reset is an application factory reset; it preserves installed software, detection models, logos, deployment environment and service configuration. Reports or pictures already downloaded to another computer and external backups are not managed by the portal. Reset tests use an isolated database and temporary storage; they do not erase the development appliance's records or pictures.
