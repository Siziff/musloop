# musloop remote — Android app

Controls musloop's **Live mode** from an Android phone over Wi-Fi. The music plays on the computer;
the phone is the remote: parts, scenes, Next / Cancel / Fade, transition mode, track switching, volume.

The same remote also works without installing anything (open the QR link in the phone's browser).
The app adds:

- **finds musloop on the Wi-Fi** by itself (UDP broadcast) and remembers your computers;
- **QR scanner** built in (Google's scanner from Play services — no camera permission);
- **screen stays on** and **haptic feedback** through Android itself;
- the remote UI is inside the APK, so it starts instantly.

Requirements: Android 7.0+, musloop on a computer in the same Wi-Fi.

## Install

Download `musloop-remote-X.Y.Z.apk` from the repository's **Releases** on the phone, open it and allow
"Install unknown apps" for your browser or file manager when Android asks.

## Pair

1. On the computer: musloop → Live mode (`L`) → **📱 Phone**.
2. In the app: tap the computer in **Found on this Wi-Fi** or **Scan QR code**. The first time you
   need the QR code (it carries the key); after that the app reconnects by itself.

If the computer is not found: same Wi-Fi network (not a guest network)? Is musloop running? Allow
musloop / Python in the computer's firewall for private networks (Windows asks on first start).

## Build

The web part is shared with the browser remote: `frontend/remote.src.js` (compiled to
`frontend/remote.js` by `python3 tools/build.py`) + `frontend/remote.html`.
`mobile/scripts/build-www.mjs` copies them into `mobile/www` and adds the app's connect screen
(`mobile/src/app.js`). Native parts: `android/app/src/main/java/io/github/siziff/musloop/`
(`DiscoveryPlugin` — UDP discovery + native HTTP ping, `QrScanPlugin` — Google code scanner).

Needs JDK 21, Node 20+, Android SDK (platform 36, build-tools 36).

```bash
python3 tools/build.py          # in the repo root: frontend/remote.js
cd mobile
npm ci
npm run apk:debug               # -> android/app/build/outputs/apk/debug/app-debug.apk
```

### Signed release (GitHub Actions)

Pushing a tag `app-vX.Y.Z` builds a signed APK and attaches it to a GitHub release. The signing key
must stay the same for every release, otherwise Android refuses to update the installed app.
Create it once and keep a backup:

```bash
keytool -genkeypair -v -keystore musloop.jks -alias musloop -keyalg RSA -keysize 4096 -validity 10000
base64 -w0 musloop.jks          # value for the MUSLOOP_KEYSTORE_B64 secret
```

Repository → Settings → Secrets and variables → Actions:
`MUSLOOP_KEYSTORE_B64`, `MUSLOOP_KEYSTORE_PASSWORD`, `MUSLOOP_KEY_ALIAS` (`musloop`), `MUSLOOP_KEY_PASSWORD`.

## Tests

- `tests/app_web_test.py LAN_IP PORT` — the app's web part in a Pixel 7-sized browser with the
  Capacitor plugins stubbed, against a running musloop (connect screen, discovery, QR, wrong key,
  unreachable PC, remote driving the PC, haptics / keep-awake calls, restart, "Another PC").
- `tests/emulator_test.py` — CI: the APK on a real Android emulator connects to a musloop server and
  plays music on it (`.github/workflows/android.yml`).
