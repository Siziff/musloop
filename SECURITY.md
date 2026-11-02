# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for a security problem.

Report it privately on GitHub: **Security** tab → **Report a vulnerability**
(<https://github.com/Siziff/musloop/security/advisories/new>). Only the maintainer sees the report.

Please include:

- what an attacker can do and from where (the same computer, the same Wi-Fi, the internet);
- steps or a small script to reproduce it;
- the version: commit (`git rev-parse --short HEAD`), Docker image tag or Android app version;
- how musloop runs: `run.sh` / `run.bat`, Docker (light or AI image), and any `MUSLOOP_*`
  settings you changed.

musloop is maintained by one person in their free time. You can expect a first answer within
7 days. Once a fix is released, the advisory is published with credit to you, unless you
prefer to stay anonymous.

## Supported versions

| Component | Supported |
|---|---|
| musloop (web app, Docker images built from this repository) | the latest commit on `main` |
| musloop remote (Android app) | the latest `app-v*` release |

Fixes are not backported to older versions.

## How musloop is meant to be used

musloop is a single-user desktop app. It runs a local web server and is used in a browser on
the same computer. Knowing what it trusts helps decide whether something is a vulnerability.

- **Requests from this computer** (loopback) get full access: library, folders, uploads,
  exports, settings.
- **Requests from the network** reach only the phone remote: the `/remote` page, its WebSocket
  and a ping endpoint. They need the remote key from the QR code shown in Live mode. The key is
  random, changes on every start and on **New code**. A phone with the key can control
  playback, but it can't read or change files.
- **Docker:** the app port (8801) is published on `127.0.0.1` only. The remote has its own port
  (8802), which never gets full access whatever the source address.
- **Discovery:** the server answers a UDP broadcast on port 8803 with its port and computer
  name. The answer has no key; pairing still needs the QR code.
- **Websites in the user's browser** are not trusted, even though they run on this computer.
  Requests and WebSockets from another site's page (`Origin`, `Sec-Fetch-Site`) get only what a
  phone without the key gets. musloop answers only to `localhost`, IP addresses, this computer's
  name, `MUSLOOP_PUBLIC_URL` and `MUSLOOP_ALLOWED_HOSTS`, which blocks DNS rebinding.
- **`MUSLOOP_TRUST_NETWORK`** gives full access to the networks you list. That's by design:
  everyone on those networks is treated like this computer.

## In scope

- Getting more than the phone remote from the network: any other page or API, without the key
  or with it.
- Bypassing or guessing the remote key, or a phone keeping access after **New code**.
- A website opened in the user's browser reading or changing musloop data, or controlling
  playback.
- Reading or writing files outside the music folders you added and musloop's own data folder
  (path traversal through folders, uploads, imports, exports or markup files).
- Running commands through imported files, URLs or markup files (`*.musloop.json`).
- The Docker images and compose files exposing more than described above.
- The Android app: leaking the saved key, accepting a foreign QR code as a musloop one, or
  connecting to something other than what the user picked.

## Out of scope

- Anyone who already has full access by design: a user of this computer, or a network listed in
  `MUSLOOP_TRUST_NETWORK`.
- Someone who has the remote key controlling playback. That is what the key is for. Use
  **New code** to cut off old phones.
- Publishing port 8801 to the network or the internet yourself (port forwarding, reverse proxy,
  `0.0.0.0:8801:8801` in Docker).
- Denial of service from the same Wi-Fi, such as flooding the remote port or the discovery port.
- Vulnerabilities in third-party tools (yt-dlp, ffmpeg, PyTorch, the AI models, Capacitor).
  Report them upstream. If musloop uses them in an unsafe way, that is in scope.
