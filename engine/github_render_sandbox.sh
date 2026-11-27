#!/bin/bash
# Fail closed if the runner cannot isolate the browser and FFmpeg.
set -euo pipefail
chrome_bin="${VRS_CHROME_BIN:-/opt/google/chrome/chrome}"
if [[ ! -x "$chrome_bin" ]]; then
  chrome_bin=$(command -v google-chrome || command -v google-chrome-stable || command -v chromium || command -v chromium-browser || true)
fi
if [[ ${0##*/} == vrs-chrome ]]; then
  # Bubblewrap prevents setuid elevation. Require Chrome’s user-namespace sandbox instead.
  exec "$chrome_bin" --disable-setuid-sandbox "$@"
fi
if [[ -z "$chrome_bin" || ! -x "$chrome_bin" ]]; then
  echo "Sanctioned Chrome executable not found for Bubblewrap sandbox." >&2
  exit 1
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
  command=(/tools/node /engine/github_sandbox_probe.mjs)
else
  command=("$python" /engine/github_production.py execute /job)
fi
extra_binds=()
if [[ -d /opt/google/chrome ]]; then
  extra_binds+=(--ro-bind /opt/google/chrome /opt/google/chrome)
fi
if [[ -n ${VRS_MODEL_DIR:-} && -d ${VRS_MODEL_DIR} ]]; then
  extra_binds+=(
    --ro-bind "$VRS_MODEL_DIR" /models
    --setenv VRS_MODEL_DIR /models
    --setenv HF_HUB_OFFLINE 1
    --setenv HF_HUB_DISABLE_TELEMETRY 1
    --setenv HF_HUB_DISABLE_IMPLICIT_TOKEN 1
  )
fi
exec bwrap --unshare-all --die-with-parent --new-session --clearenv \
  --ro-bind /usr /usr --ro-bind /bin /bin --ro-bind /lib /lib --ro-bind /lib64 /lib64 \
  --ro-bind /etc/fonts /etc/fonts \
  --proc /proc --dev /dev --tmpfs /tmp \
  --ro-bind "$engine" /engine --bind "$VRS_JOB" /job \
  --ro-bind "$VRS_JOB/transfer.json" /job/transfer.json \
  --ro-bind "$python_home" "$python_home" \
  --ro-bind "$node" /tools/node --ro-bind "$ffmpeg" /tools/ffmpeg --ro-bind "$ffprobe" /tools/ffprobe \
  --setenv PATH /tools:/usr/bin --setenv HOME /tmp --setenv LANG C.UTF-8 \
  --ro-bind "$engine/github_render_sandbox.sh" /tools/vrs-chrome \
  --setenv VRS_CHROME_BIN "$chrome_bin" \
  --setenv VRS_REMOTE_BROWSER /tools/vrs-chrome \
  "${extra_binds[@]}" \
  --chdir /job "${command[@]}"
