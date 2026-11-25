"""GitHub runner entrypoint for the repository-owned synthetic WebGL test."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from time import monotonic


PRESETS = {"draft": (540, 960, 30, 24, "draft"),
           "standard": (1080, 1920, 30, 18, "standard"),
           "premium": (1080, 1920, 60, 15, "high")}


def smoke(stage, preset, vendor):
    width, height, fps, _, _ = PRESETS[preset]
    (stage / "assets").mkdir()
    # An actual WebGL scene exercises the same browser path without any user assets.
    module = vendor / "node_modules/three/build/three.module.min.js"
    (stage / "assets/three.js").write_bytes(module.read_bytes())
    (stage / "index.html").write_text(f'''<!doctype html><html><meta charset="utf-8">
<style>body{{margin:0}} #smoke{{position:relative;width:{width}px;height:{height}px}}</style>
<div id="smoke" data-composition-id="smoke" data-start="0" data-duration="2"
 data-width="{width}" data-height="{height}" data-no-timeline></div>
<script type="module">
import * as T from './assets/three.js';
window.__hf = window.__hf || {{}};
window.__hf.buildReady = window.__hf.buildReady || {{}};
window.__hf.buildReady.smoke = (async () => {{
  const renderer = new T.WebGLRenderer({{antialias:true}});
  if (!renderer.capabilities.isWebGL2) throw new Error("WebGL 2 unavailable");
  renderer.setSize({width},{height});
  document.querySelector('#smoke').append(renderer.domElement);
  const scene = new T.Scene();
  scene.background = new T.Color(0x152136);
  const camera = new T.PerspectiveCamera(45,{width}/{height},0.1,20);
  camera.position.z = 5;
  const mesh = new T.Mesh(new T.BoxGeometry(),new T.MeshNormalMaterial());
  scene.add(mesh);
  const draw = time => {{ mesh.rotation.set(time * 0.7,time,0.3); renderer.render(scene,camera); }};
  draw(0);
  window.addEventListener('hf-seek',event=>draw(event.detail.time));
}})();
</script></html>''', encoding="utf-8")
    return {"preset": preset, "frames": 2 * fps}


def probe(path, preset, frames):
    width, height, fps, _, _ = PRESETS[preset]
    data = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_streams",
                                                "-show_format", "-of", "json", path]))
    streams = data["streams"]
    if len(streams) != 1:
        raise ValueError("Master must contain only one video stream.")
    video = streams[0]
    if (video["codec_name"], video["pix_fmt"], video["width"], video["height"],
        video["avg_frame_rate"], int(video["nb_frames"])) != (
            "h264", "yuv420p", width, height, f"{fps}/1", frames):
        raise ValueError("Master dimensions, codec, pixel format or frame cadence differ.")
    if abs(float(data["format"]["duration"]) - frames / fps) > 1 / fps:
        raise ValueError("Master duration differs.")
    subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", path, "-f", "null", "-"], check=True)
    return {"width": width, "height": height, "fps": fps, "frames": frames, "bytes": path.stat().st_size,
            "videoBitrate": int(video["bit_rate"]), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--preset", choices=PRESETS, required=True)
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dependencies", type=Path, required=True)
    args = parser.parse_args()
    args.stage.mkdir()
    args.output.mkdir()
    manifest = smoke(args.stage, args.preset, args.dependencies)
    _, _, fps, crf, quality = PRESETS[args.preset]
    env = {key: os.environ[key] for key in (
        "PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TMP", "TEMP", "XDG_CACHE_HOME",
        "HYPERFRAMES_BROWSER_PATH"
    ) if key in os.environ}
    env.update(DO_NOT_TRACK="1", HYPERFRAMES_CHROME_SANDBOX="1", PRODUCER_RESTRICT_PAGE_NETWORK="1")
    if not Path(env.get("HYPERFRAMES_BROWSER_PATH", "")).is_file():
        raise ValueError("An installed sandboxed Chrome executable is required.")
    version = subprocess.check_output([env["HYPERFRAMES_BROWSER_PATH"], "--version"], text=True).strip()
    capture = args.stage.parent / "raw-capture.mp4"
    started = monotonic()
    with (args.stage.parent / "capture.log").open("w") as log:
        try:
            subprocess.run(["node", args.dependencies / "node_modules/hyperframes/dist/cli.js", "render",
                            args.stage, "--output", capture, "--fps", str(fps), "--crf", str(crf),
                            "--quality", quality, "--workers", "1", "--strict", "--no-best-effort",
                            "--refuse-outside-files"], env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
        except subprocess.CalledProcessError as error:
            # Show only known diagnostic IDs; raw renderer logs may contain project data.
            codes = re.findall(r'@hf-diagnostic .*?"code":"([A-Z_]+)"', (args.stage.parent / "capture.log").read_text())
            print("Capture failed:", ", ".join(sorted(set(codes))) or f"exit {error.returncode}", flush=True)
            raise SystemExit(1) from None
    capture_sec = monotonic() - started
    master = args.output / "visual-only.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", capture, "-map", "0:v:0",
                    "-c:v", "copy", "-an", master], check=True)
    details = probe(master, args.preset, manifest["frames"])
    (args.output / "render-metrics.json").write_text(json.dumps({
        **details, "captureSeconds": capture_sec, "workerSeconds": monotonic() - started,
        "preset": args.preset, "crf": crf, "chrome": version}, indent=2) + "\n")


if __name__ == "__main__":
    main()
