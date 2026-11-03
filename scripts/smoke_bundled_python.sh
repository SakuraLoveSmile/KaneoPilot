#!/bin/bash
# Mount a built KaneoPilot DMG read-only and run its bundled Python the way a user's Mac would: an empty
# environment, no Homebrew, no pyenv, nothing but the system directories. It only mounts and reads (no build,
# no signing), so it also works on a package that was downloaded to a Mac.
set -euo pipefail

[ "$#" -eq 1 ] && [ -f "$1" ] || { echo "usage: smoke_bundled_python.sh <KaneoPilot DMG>" >&2; exit 2; }
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="$(mktemp -d)"
MOUNT="$WORK/mount"
mkdir -p "$MOUNT" "$WORK/home"
cleanup() {
    hdiutil detach "$MOUNT" -quiet 2>/dev/null || hdiutil detach "$MOUNT" -force -quiet 2>/dev/null || true
    rm -rf "$WORK"
}
trap cleanup EXIT

hdiutil attach -readonly -nobrowse -noautoopen -mountpoint "$MOUNT" "$1" >/dev/null
APP="$MOUNT/KaneoPilot.app"
PY="$APP/Contents/Resources/python/bin/python3"
[ -x "$PY" ] || { echo "ERROR: $PY is missing or not executable" >&2; exit 1; }

echo "== signature and Mach-O audit of the mounted app =="
codesign --verify --deep --strict "$APP"
python3 "$HERE/app_macho.py" audit "$APP"

# What a user's Mac gives a process that the App starts: the system directories and a scratch home.
run() { env -i HOME="$WORK/home" PATH=/usr/bin:/bin TMPDIR="$WORK" PYTHONDONTWRITEBYTECODE=1 "$@"; }

echo "== bundled interpreter in an empty environment =="
run "$PY" - <<'PY'
import bz2, ctypes, fcntl, hashlib, http.server, json, lzma, os, plistlib, sqlite3, ssl, sys, urllib.error, urllib.request, zlib

print("python", sys.version.split()[0], sys.executable)
prefix = os.path.realpath(sys.prefix)
if not prefix.endswith("/Contents/Resources/python"):
    raise SystemExit("sys.prefix is not inside the app: " + prefix)
for entry in sys.path:
    if entry and not os.path.realpath(entry).startswith(prefix):
        raise SystemExit("sys.path leaves the app: " + entry)
paths = ssl.get_default_verify_paths()
print("ssl", ssl.OPENSSL_VERSION, "cafile", paths.cafile, "capath", paths.capath, "(openssl default", paths.openssl_cafile + ")")
if not (paths.cafile or paths.capath):
    raise SystemExit("no usable CA certificates; the system file " + paths.openssl_cafile + " is missing")
print("sqlite", sqlite3.sqlite_version)
# Any HTTP answer proves the TLS handshake and the certificate chain; only a TLS failure is an error here.
request = urllib.request.Request("https://api.github.com/", headers={"User-Agent": "KaneoPilot-smoke"})
try:
    with urllib.request.urlopen(request, timeout=30) as response:
        print("https api.github.com", response.status)
except urllib.error.HTTPError as error:
    print("https api.github.com", error.code)
PY

echo "== core doctor with the bundled interpreter =="
# An explicit empty payload and a closed stdin: without --json the CLI waits for a payload on a piped stdin.
# Most other checks are legitimately not ok in a clean environment (no Kaneo login, no folders), so the exit code is not judged.
run KANPILOT_DB="$WORK/smoke.db" "$PY" "$APP/Contents/Resources/core/kanpilot.py" doctor --json '{}' < /dev/null > "$WORK/doctor.json" || true
python3 - "$WORK/doctor.json" "$HERE/python-runtime.lock.json" <<'PY'
import json, sys

checks = {item["name"]: item for item in json.load(open(sys.argv[1], encoding="utf-8"))["checks"]}
locked = json.load(open(sys.argv[2], encoding="utf-8"))["python_version"]
for name in ("python", "db_writable"):
    if not checks[name]["ok"]:
        raise SystemExit("doctor check failed: %s" % checks[name])
if checks["python"]["detail"] != locked:
    raise SystemExit("the app runs Python %s, the lock file says %s" % (checks["python"]["detail"], locked))
print("doctor: python", checks["python"]["detail"], "(as locked) | db_writable", checks["db_writable"]["ok"])
PY
echo "== bundled Python smoke passed =="
