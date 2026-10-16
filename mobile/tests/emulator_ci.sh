#!/usr/bin/env bash
# CI (android.yml, inside android-emulator-runner): open the PC host tab,
# read the current remote key, run the emulator test against the server.
set -u
python mobile/tests/pc_host.py http://127.0.0.1:8801 "Village Consort" > host.log 2>&1 &
for i in $(seq 1 90); do grep -q "PC host ready" host.log && break; sleep 2; done
cat host.log
# read the key right before the test (the web test above rotated it with "New code")
KEY=$(curl -fs localhost:8801/api/remote/info | python -c "import sys,json;print(json.load(sys.stdin)['key'])")
echo "key length: ${#KEY}; ping with it:"
curl -fs -H "X-Musloop-Key: $KEY" localhost:8801/api/remote/ping; echo
python mobile/tests/emulator_test.py http://10.0.2.2:8801 "$KEY" \
  mobile/android/app/build/outputs/apk/debug/app-debug.apk > emu.log 2>&1
RC=$?
cat emu.log
if [ $RC -ne 0 ]; then
  echo "::error title=emulator test::$(tail -25 emu.log | tr '\n' '|')"
  echo "::error title=host log::$(tail -8 host.log | tr '\n' '|')"
  echo "::error title=logcat::$(adb logcat -d -s chromium:E Capacitor:E AndroidRuntime:E '*:F' | tail -15 | tr '\n' '|')"
fi
exit $RC
