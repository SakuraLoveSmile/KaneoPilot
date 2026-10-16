"""Owned verification watchdog: registration precedes execution; no model input."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import threading

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kanpilot.process_owner import process_birth
from kanpilot import verification_redaction


def _alive(pid, birth):
    if birth and process_birth(pid) != birth:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _signal_group(pid, signum):
    try:
        os.killpg(pid, signum)
    except PermissionError:
        # On macOS a group containing a reaped/zombie leader can return EPERM
        # instead of ESRCH. Signal only live members of our registered group;
        # never interpret a live process's permission error as successful exit.
        view = subprocess.run(["/bin/ps", "-axo", "pid=,pgid=,stat="], capture_output=True, text=True, check=True)
        for line in view.stdout.splitlines():
            fields = line.split()
            if len(fields) == 3 and fields[1] == str(pid) and not fields[2].startswith("Z"):
                try:
                    os.kill(int(fields[0]), signum)
                except ProcessLookupError:
                    pass


def _stop_group(pid):
    # The leader exiting does not prove its descendants have exited.
    try:
        _signal_group(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    time.sleep(0.1)
    try:
        _signal_group(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def main():
    owner, handshake, command, timeout = int(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], float(sys.argv[4])
    owner_birth = json.loads(sys.argv[5])
    deadline = time.monotonic() + 30
    while not handshake.exists():
        if not _alive(owner, owner_birth) or time.monotonic() >= deadline:
            return 125
        time.sleep(0.02)
    try:
        registration = json.loads(handshake.read_text())
    except (OSError, ValueError):
        return 125
    if (registration != {"owner_pid": owner, "command_pid": os.getpid(),
                         "owner_birth": owner_birth, "command_birth": process_birth(os.getpid())}
            or not _alive(owner, owner_birth)):
        return 125
    # Pipe through a bounded redactor before any command output reaches the log.
    keys = json.loads(sys.argv[6]) if len(sys.argv) > 6 else []
    secrets = verification_redaction.values(os.environ, keys)
    process = subprocess.Popen(["/bin/bash", "--noprofile", "--norc", "-c", command],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    def copy_output():
        pending = ""
        overlap = max([len(value) for value in secrets] + [4096])
        import codecs
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        while True:
            chunk = process.stdout.read(4096)
            pending += decoder.decode(chunk, final=not chunk)
            cut = max(0, len(pending) - overlap) if chunk else len(pending)
            # Do not emit the beginning of a value whose end is still buffered.
            for secret in secrets:
                start = pending.rfind(secret, 0, min(len(pending), cut + len(secret)))
                if 0 <= start < cut < start + len(secret):
                    cut = start
            if cut:
                sys.stdout.write(verification_redaction.redact(pending[:cut], secrets))
                sys.stdout.flush()
                pending = pending[cut:]
            if not chunk:
                break
    copier = threading.Thread(target=copy_output, daemon=True)
    copier.start()
    def cancelled(signum, frame):
        _stop_group(process.pid)
        process.wait()
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, cancelled)
    signal.signal(signal.SIGINT, cancelled)
    try:
        code = process.wait(timeout=timeout)
        # Verification checks cannot leave background writers behind either.
        _stop_group(process.pid)
        return code if code >= 0 else 128 - code
    except subprocess.TimeoutExpired:
        _stop_group(process.pid)
        process.wait()
        return 124
    finally:
        _stop_group(process.pid)
        copier.join(timeout=5)


if __name__ == "__main__":
    sys.exit(main())
