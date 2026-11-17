"""Install the YouTube PO token provider (bgutil-ytdlp-pot-provider) for yt-dlp.

YouTube now refuses media downloads (HTTP 403) from clients that don't send a
"proof of origin" token. bgutil generates it with Deno (installed with musloop).
Two parts:
  * the yt-dlp plugin  -> pip package bgutil-ytdlp-pot-provider (requirements.txt)
  * the generator      -> <repo>/.potoken/server (this script: download the
                          release source, `deno install` its npm dependencies)
musloop passes the generator's path to yt-dlp (backend/main.py).

    python tools/setup_potoken.py          # install / update (safe to re-run)

The version follows the installed plugin, so plugin and generator always match.
"""
import io
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEST = os.path.join(BASE, ".potoken")


def plugin_version() -> str | None:
    try:
        import importlib.metadata as md
        return md.version("bgutil-ytdlp-pot-provider")
    except Exception:  # noqa: BLE001
        return None


def deno_bin() -> str | None:
    try:
        import deno
        p = deno.find_deno_bin()
        if p and os.path.exists(p):
            return p
    except Exception:  # noqa: BLE001
        pass
    return shutil.which("deno")


def main() -> int:
    ver = plugin_version()
    if not ver:
        print("PO token provider: the yt-dlp plugin is not installed (pip install bgutil-ytdlp-pot-provider)")
        return 1
    dn = deno_bin()
    if not dn:
        print("PO token provider: Deno not found (pip install deno) — skipped")
        return 1
    server = os.path.join(DEST, "server")
    stamp = os.path.join(DEST, "version")
    if os.path.exists(stamp) and open(stamp).read().strip() == ver and \
            os.path.isdir(os.path.join(server, "node_modules")):
        print(f"PO token provider {ver}: ready")
        return 0
    print(f"PO token provider {ver}: downloading…", flush=True)
    url = f"https://github.com/Brainicism/bgutil-ytdlp-pot-provider/archive/refs/tags/{ver}.tar.gz"
    try:
        with urllib.request.urlopen(url, timeout=120) as r:
            data = r.read()
    except Exception as e:  # noqa: BLE001
        print(f"PO token provider: download failed ({e}). YouTube may answer 403 until it is installed.")
        return 1
    tmp = DEST + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        root = os.path.realpath(tmp)
        for m in tf.getmembers():   # no paths outside the target folder
            if not os.path.realpath(os.path.join(tmp, m.name)).startswith(root + os.sep):
                print("PO token provider: unexpected path in the archive, aborted")
                return 1
        tf.extractall(tmp, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
    top = next(d for d in os.listdir(tmp) if os.path.isdir(os.path.join(tmp, d)))
    src_server = os.path.join(tmp, top, "server")
    print("PO token provider: installing its dependencies (deno install, ~1 min)…", flush=True)
    env = dict(os.environ, DENO_NO_PROMPT="1", DENO_NO_UPDATE_CHECK="1")
    r = subprocess.run([dn, "install", "--allow-scripts=npm:canvas", "--frozen"], cwd=src_server, env=env,
                       capture_output=True, text=True)
    if r.returncode != 0:
        print("PO token provider: deno install failed:\n" + (r.stderr or r.stdout)[-800:])
        shutil.rmtree(tmp, ignore_errors=True)
        return 1
    shutil.rmtree(server, ignore_errors=True)
    os.makedirs(DEST, exist_ok=True)
    shutil.move(src_server, server)
    shutil.rmtree(tmp, ignore_errors=True)
    with open(stamp, "w") as f:
        f.write(ver)
    print(f"PO token provider {ver}: installed in {server}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
