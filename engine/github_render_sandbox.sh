#!/bin/bash
# Fail closed if the runner cannot isolate the browser and FFmpeg.
set -euo pipefail
if [[ ${0##*/} == vrs-chrome ]]; then
  # Bubblewrap prevents setuid elevation. Require Chrome’s user-namespace sandbox instead.
  exec /opt/google/chrome/chrome --disable-setuid-sandbox "$@"
fi
engine=$(cd -- "$(dirname -- "$0")" && pwd)
node=$(realpath "$(command -v node)")
# Jammy's system Python is too old for the renderer. Mount only the selected toolchain.
python=$(realpath "$(command -v python3)")
python_home=$("$python" -c 'import sys; print(sys.base_prefix)')
test "${python#"$python_home"/bin/}" != "$python"
ffmpeg=$(realpath "$(command -v ffmpeg)")
ffprobe=$(realpath "$(command -v ffprobe)")
if [[ ${1:-} == probe ]]; then
  command=("$python" /engine/github_sandbox_probe.py)
else
  command=("$python" /engine/github_production.py render /job)
fi
exec bwrap --unshare-all --die-with-parent --new-session --clearenv \
  --ro-bind /usr /usr --ro-bind /bin /bin --ro-bind /lib /lib --ro-bind /lib64 /lib64 \
  --ro-bind /etc/fonts /etc/fonts --ro-bind /opt/google/chrome /opt/google/chrome \
  --proc /proc --dev /dev --tmpfs /tmp \
  --ro-bind "$engine" /engine --bind "$VRS_JOB" /job \
  --ro-bind "$VRS_JOB/transfer.json" /job/transfer.json \
  --ro-bind "$python_home" "$python_home" \
  --ro-bind "$node" /tools/node --ro-bind "$ffmpeg" /tools/ffmpeg --ro-bind "$ffprobe" /tools/ffprobe \
  --setenv PATH /tools:/usr/bin --setenv HOME /tmp --setenv LANG C.UTF-8 \
  --ro-bind "$engine/github_render_sandbox.sh" /tools/vrs-chrome \
  --setenv VRS_REMOTE_BROWSER /tools/vrs-chrome \
  --chdir /job "${command[@]}"
