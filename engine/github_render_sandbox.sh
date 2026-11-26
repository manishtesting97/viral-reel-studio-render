#!/bin/bash
# Fail closed if the runner cannot isolate the browser and FFmpeg.
set -euo pipefail
engine=$(cd -- "$(dirname -- "$0")" && pwd)
node=$(realpath "$(command -v node)")
ffmpeg=$(realpath "$(command -v ffmpeg)")
ffprobe=$(realpath "$(command -v ffprobe)")
if [[ ${1:-} == probe ]]; then
  command=(/usr/bin/python3 -c '
import os, socket, subprocess
assert {name for _, name in socket.if_nameindex()} == {"lo"}
assert "GH_TOKEN" not in os.environ and "GITHUB_ENV" not in os.environ
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
subprocess.run(["/opt/google/chrome/chrome", "--headless", "--disable-gpu",
                "--disable-dev-shm-usage", "--dump-dom", "about:blank"],
               check=True, timeout=30)
subprocess.run(["/tools/ffmpeg", "-version"], check=True)
')
else
  command=(/usr/bin/python3 /engine/github_production.py render /job)
fi
exec bwrap --unshare-all --die-with-parent --new-session --clearenv \
  --ro-bind /usr /usr --ro-bind /bin /bin --ro-bind /lib /lib --ro-bind /lib64 /lib64 \
  --ro-bind /etc/fonts /etc/fonts --ro-bind /opt/google/chrome /opt/google/chrome \
  --proc /proc --dev /dev --tmpfs /tmp \
  --ro-bind "$engine" /engine --bind "$VRS_JOB" /job \
  --ro-bind "$VRS_JOB/transfer.json" /job/transfer.json \
  --ro-bind "$node" /tools/node --ro-bind "$ffmpeg" /tools/ffmpeg --ro-bind "$ffprobe" /tools/ffprobe \
  --setenv PATH /tools:/usr/bin --setenv HOME /tmp --setenv LANG C.UTF-8 \
  --setenv VRS_REMOTE_BROWSER /opt/google/chrome/chrome \
  --chdir /job "${command[@]}"
