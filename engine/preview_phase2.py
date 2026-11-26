"""Assemble, check, render, and verify the Phase 2 style-reference visual preview."""

import argparse
from html import escape
import json
import os
from pathlib import Path
import shutil
import subprocess
from uuid import uuid4

from studio_paths import ENGINE, ROOT, CACHE, MODELS, browser_path
STYLE_LIB = ROOT / "style-library"
BRAND_DIR = ROOT / "brand-profiles"
COMPONENTS = ENGINE / "components"
EXPORTS = ROOT / "export"
GSAP_VENDOR = (
    ENGINE
    / "node_modules"
    / "hyperframes"
    / "dist"
    / "vendor"
    / "npm"
    / "gsap@3.15.0"
    / "dist"
    / "gsap.min.js"
)

THREE_VENDOR = (
    ENGINE / "node_modules" / "three" / "build" / "three.module.min.js"
)

EXPORT_PRESETS = {
    "draft": {"quality": "draft", "crf": 24, "fps": 30},
    "standard": {"quality": "standard", "crf": 18, "fps": 30},
    "premium": {"quality": "high", "crf": 15, "fps": 60},
}

DEFAULT_SEGMENTS = [
    {
        "id": "scene1",
        "component": "cost-comparison.html",
        "text": "Manual edits waste hours every week.",
        "vars": {
            "headlineMain": "Manual edits waste",
            "headlineAccent": "hours every week",
            "itemALabel": "Manual Cuts",
            "itemBLabel": "Static Slides",
            "costValue": "14 hrs",
            "stampText": "WASTED",
            "impactNote": "Demo example · ~700 hrs/yr",
        },
    },
    {
        "id": "scene2",
        "component": "workflow-pipeline.html",
        "text": "Connect once and verify every step.",
        "vars": {
            "headlineMain": "Connect once,",
            "headlineAccent": "verify every step",
            "windowTitle": "studio.pipeline",
            "commandText": "studio align --verify-sync",
            "node1Title": "Voice",
            "node1Sub": "Audio",
            "node2Title": "Align",
            "node2Sub": "Words",
            "node3Title": "Export",
            "node3Sub": "1080p",
            "row1Text": "Audio synced (demo)",
            "row2Text": "Reels safe-zone OK",
            "row3Text": "Brand tokens isolated",
        },
    },
    {
        "id": "scene3",
        "component": "three-studio-stage.html",
        "text": "Track real scores before you export.",
        "vars": {
            "headlineMain": "3D-layered clarity",
            "headlineAccent": "before you export",
            "terminalLine": "$ reel check",
            "terminalOut": "1080p · CRF 15",
            "targetScore": 96,
            "scoreTitle": "DEMO SCORE (ILLUSTRATIVE)",
            "badgeLabel": "DEMO · 3D",
        },
    },
    {
        "id": "scene4",
        "component": "cta-endcard.html",
        "text": "Comment below to try this workflow.",
        "vars": {},
    },
]


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def synthesize_voice(segments, out_wav):
    """Synthesize offline flite narration per beat with brief breath pauses."""
    part_files = []
    part_durations = []
    tmp_dir = out_wav.parent / "parts"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    for idx, seg in enumerate(segments):
        part = tmp_dir / f"part_{idx}.wav"
        text = seg["text"].replace("'", "")
        run(
            "ffmpeg", "-nostdin", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"flite=text='{text}':voice=slt",
            "-af", "apad=pad_dur=0.45", "-ac", "1", "-ar", "16000", part,
        )
        probe = run(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1", part, capture_output=True, text=True,
        )
        part_durations.append(float(probe.stdout.strip()))
        part_files.append(part)

    # Pad final end-card hold (+2.50s so closing card holds >= 2.2s cleanly)
    end_pad = tmp_dir / "end_pad.wav"
    run(
        "ffmpeg", "-nostdin", "-v", "error", "-y",
        "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
        "-t", "2.50", end_pad,
    )
    list_file = tmp_dir / "concat.txt"
    lines = [f"file '{p.name}'" for p in [*part_files, end_pad]]
    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    run(
        "ffmpeg", "-nostdin", "-v", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", list_file,
        "-ac", "1", "-ar", "16000", out_wav,
    )
    return part_durations


def probe_duration(media_path):
    probe = run(
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", media_path, capture_output=True, text=True,
    )
    return float(probe.stdout.strip())


def transcribe_words(voice_path, total_duration):
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    os.environ["HF_HUB_OFFLINE"] = "1"
    from faster_whisper import WhisperModel

    model = WhisperModel(
        "tiny.en", device="cpu", compute_type="int8", cpu_threads=2,
        download_root=str(MODELS),
    )
    segments, _ = model.transcribe(str(voice_path), language="en", word_timestamps=True)
    words = []
    prev_end = 0.0
    for seg in segments:
        for w in seg.words:
            txt = w.word.strip()
            if not txt:
                continue
            start = max(prev_end, float(w.start))
            end = min(total_duration - 0.05, max(start + 0.08, float(w.end)))
            if end <= start:
                continue
            words.append({"text": txt, "start": round(start, 3), "end": round(end, 3)})
            prev_end = end
    if not words:
        raise ValueError("Whisper detected no words in audio.")
    return words


def group_captions(words, max_words=3, max_gap=0.25):
    groups = []
    current = []
    for w in words:
        if current:
            gap = w["start"] - current[-1]["end"]
            ends_sentence = current[-1]["text"].endswith((".", "!", "?"))
            if len(current) >= max_words or gap >= max_gap or ends_sentence:
                groups.append(current)
                current = []
        current.append(w)
    if current:
        groups.append(current)
    return groups


def build_captions_html(groups, total_duration, style_cfg, brand_cfg):
    mode = style_cfg["captions"]["mode"]
    accent = brand_cfg["colors"]["brand-accent"]
    bright = brand_cfg["colors"]["brand-accent-bright"]
    upcoming = style_cfg["tokens"]["style-upcoming-ink"]
    card_cls = " vrs-caption-group--pill-card" if mode == "inline-card" else ""

    group_divs = []
    timeline_js = []
    for gi, grp in enumerate(groups):
        g_start = max(0.08, grp[0]["start"] - 0.05)
        g_end = min(total_duration - 0.04, grp[-1]["end"] + 0.16)
        if gi + 1 < len(groups):
            next_start = max(0.08, groups[gi + 1][0]["start"] - 0.05)
            g_end = min(g_end, next_start - 0.02)
        if g_end <= g_start + 0.18:
            g_end = g_start + 0.18

        gid = f"cap-g{gi}"
        word_spans = []
        for wi, w in enumerate(grp):
            wid = f"{gid}-w{wi}"
            word_spans.append(f'<span id="{wid}" class="vrs-cap-word">{escape(w["text"])}</span>')

            w_start = max(g_start, w["start"])
            w_end = max(w_start + 0.08, min(g_end - 0.04, w["end"]))
            tween_in = min(0.09, (w_end - w_start) * 0.45)
            tween_out = min(0.09, max(0.04, (g_end - w_end) * 0.6))
            if mode == "inline-card":
                timeline_js.append(
                    f'tl.to("#{wid}", {{ color: "{bright}", backgroundColor: "rgba(37,99,235,0.12)", scale: 1.05, duration: {tween_in:.3f}, ease: "power2.out" }}, {w_start:.3f});'
                    f'tl.to("#{wid}", {{ color: "#181412", backgroundColor: "transparent", scale: 1, duration: {tween_out:.3f}, ease: "power2.inOut" }}, {w_end:.3f});'
                )
            else:
                timeline_js.append(
                    f'tl.to("#{wid}", {{ color: "#ffffff", backgroundColor: "{bright}", scale: 1.06, boxShadow: "0 10px 24px rgba(234,88,12,0.28)", duration: {tween_in:.3f}, ease: "power2.out" }}, {w_start:.3f});'
                    f'tl.to("#{wid}", {{ color: "{accent}", backgroundColor: "transparent", scale: 1, boxShadow: "none", duration: {tween_out:.3f}, ease: "power2.inOut" }}, {w_end:.3f});'
                )

        group_divs.append(
            f'<div id="{gid}" class="vrs-caption-group{card_cls}">\n        '
            + "\n        ".join(word_spans)
            + "\n      </div>"
        )
        timeline_js.insert(
            len(timeline_js) - len(grp),
            f'tl.set("#{gid}", {{ visibility: "visible" }}, {g_start:.3f});'
            f'tl.fromTo("#{gid}", {{ opacity: 0, y: 8 }}, {{ opacity: 1, y: 0, duration: 0.08, ease: "power2.out" }}, {g_start:.3f});',
        )
        fade_at = max(g_start + 0.08, g_end - 0.12)
        timeline_js.append(
            f'tl.to("#{gid}", {{ opacity: 0, duration: 0.12, ease: "power2.in" }}, {fade_at:.3f});'
            f'tl.set("#{gid}", {{ opacity: 0, visibility: "hidden" }}, {g_end:.3f});'
        )

    return f"""<!doctype html>
<html lang="en">
  <body>
    <template>
      <link rel="stylesheet" href="assets/master-stylesheet.css" />
      <style>
        #root {{
          position: absolute;
          inset: 0;
        }}
        [data-composition-id="captions"] .vrs-cap-word {{
          color: {upcoming};
        }}
      </style>
      <div id="root" data-composition-id="captions" data-width="1080" data-height="1920" data-duration="{total_duration:.3f}">
        <div class="vrs-caption-stage" data-layout-allow-overflow>
          {chr(10).join("          " + d for d in group_divs)}
        </div>
      </div>
      <script src="assets/vendor/gsap.min.js"></script>
      <script>
        (function () {{
          const tl = gsap.timeline({{ paused: true }});
          {chr(10).join("          " + line for line in timeline_js)}
          window.__timelines = window.__timelines || {{}};
          window.__timelines["captions"] = tl;
        }})();
      </script>
    </template>
  </body>
</html>
"""


def build_index_html(style_cfg, brand_cfg, scene_slots, total_duration):
    merged_vars = [
        {"id": "brand-name", "type": "string", "label": "Brand Name", "default": brand_cfg["brandName"]},
        {"id": "brand-monogram", "type": "string", "label": "Monogram", "default": brand_cfg["monogram"]},
        {"id": "brand-badge", "type": "string", "label": "Status Badge", "default": brand_cfg["badgeText"]},
    ]
    for k, v in style_cfg["tokens"].items():
        merged_vars.append({"id": k, "type": "color" if str(v).startswith("#") else "string", "label": k, "default": v})
    for k, v in brand_cfg["colors"].items():
        merged_vars.append({"id": k, "type": "color" if str(v).startswith("#") else "string", "label": k, "default": v})

    css_vars_lines = []
    for k, v in {**style_cfg["tokens"], **brand_cfg["colors"]}.items():
        css_vars_lines.append(f"  --{k}: {v};")

    scene_hosts = []
    for idx, slot in enumerate(scene_slots, start=1):
        val_json = json.dumps(slot["vars"], ensure_ascii=False)
        scene_hosts.append(
            f'      <div id="{slot["id"]}" data-composition-id="{slot["id"]}" '
            f'data-composition-src="compositions/{slot["id"]}.html" '
            f'data-start="{slot["start"]:.3f}" data-duration="{slot["duration"]:.3f}" '
            f'data-track-index="{idx}" data-track-kind="graphics" '
            f'data-width="1080" data-height="1920" '
            f"data-variable-values='{escape(val_json, quote=True)}'></div>"
        )

    backdrop_cls = style_cfg["canvas"]["backdropClass"]
    backdrop_mode = "isometric" if style_cfg["id"] == "isometric-tech" else "editorial"
    accent_hex = brand_cfg["colors"]["brand-accent"]

    return f"""<!doctype html>
<html lang="en" data-composition-variables='{escape(json.dumps(merged_vars), quote=True)}'>
  <head>
    <meta charset="utf-8" />
    <title>{escape(brand_cfg["brandName"])} — {escape(style_cfg["name"])}</title>
    <link rel="stylesheet" href="assets/master-stylesheet.css" />
    <style>
      :root {{
{chr(10).join(css_vars_lines)}
      }}
      body {{
        margin: 0;
        width: 1080px;
        height: 1920px;
        overflow: hidden;
      }}
      #main {{
        position: relative;
        width: 1080px;
        height: 1920px;
        overflow: hidden;
      }}
      #vrs-bg-canvas {{
        position: absolute;
        inset: 0;
        width: 1080px;
        height: 1920px;
        pointer-events: none;
        z-index: 2;
      }}
    </style>
    <script src="assets/vendor/gsap.min.js"></script>
    <script src="assets/iso-backdrop.js"></script>
  </head>
  <body>
    <main id="main" data-composition-id="main" data-start="0" data-duration="{total_duration:.3f}" data-width="1080" data-height="1920">
      <div class="vrs-backdrop {backdrop_cls}" data-layout-ignore></div>
      <canvas id="vrs-bg-canvas" width="1080" height="1920" data-layout-ignore></canvas>

      <div class="vrs-progress-track" data-layout-ignore>
        <div id="vrs-progress" class="vrs-progress-fill"></div>
      </div>

      <!-- Subtle decorative studio framing in non-text Reels margins (waveform animated via canvas) -->
      <svg viewBox="0 0 1080 1920" style="position:absolute;inset:0;width:1080px;height:1920px;pointer-events:none;z-index:3;opacity:0.20;" data-layout-ignore>
        <line x1="72" y1="224" x2="1008" y2="224" stroke="{accent_hex}" stroke-width="2" stroke-dasharray="8 10" />
        <line x1="72" y1="1308" x2="1008" y2="1308" stroke="{accent_hex}" stroke-width="2" stroke-dasharray="8 10" />
        <rect x="72" y="1352" width="936" height="118" rx="22" fill="none" stroke="{accent_hex}" stroke-width="1.5" />
      </svg>

      <header class="vrs-header">
        <div class="vrs-brand-lockup">
          <div class="vrs-brand-avatar" data-var-text="brand-monogram" data-layout-ignore>{escape(brand_cfg["monogram"])}</div>
          <div class="vrs-brand-name" data-var-text="brand-name">{escape(brand_cfg["brandName"])}</div>
        </div>
        <div class="vrs-status-pill">
          <span class="vrs-status-dot"></span>
          <span data-var-text="brand-badge">{escape(brand_cfg["badgeText"])}</span>
        </div>
      </header>

{chr(10).join(scene_hosts)}

      <div id="captions" data-composition-id="captions" data-composition-src="compositions/captions.html"
           data-start="0" data-duration="{total_duration:.3f}" data-track-index="10"
           data-track-kind="captions" data-width="1080" data-height="1920"></div>

      <audio id="voice" class="clip" src="voice.wav" data-start="0" data-duration="{total_duration:.3f}" data-track-index="20"></audio>
    </main>

    <script>
      (function () {{
        window.initVrsBackdropCanvas(document.getElementById("vrs-bg-canvas"), {{
          mode: "{backdrop_mode}",
          accent: "{accent_hex}",
          secondary: "#06b6d4"
        }});
        const tl = gsap.timeline({{ paused: true }});
        tl.fromTo("#vrs-progress", {{ scaleX: 0 }}, {{ scaleX: 1, duration: {total_duration:.3f}, ease: "none" }}, 0);
        window.__timelines = window.__timelines || {{}};
        window.__timelines["main"] = tl;
      }})();
    </script>
  </body>
</html>
"""


def verify_video(video_path, expected_duration, expected_fps):
    probe = run(
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json",
        video_path, capture_output=True, text=True,
    )
    info = json.loads(probe.stdout)
    streams = {s["codec_type"]: s for s in info["streams"]}
    pic = streams["video"]
    dur = float(info["format"]["duration"])
    if (
        (pic["width"], pic["height"]) != (1080, 1920)
        or pic["avg_frame_rate"] != f"{expected_fps}/1"
        or pic["codec_name"] != "h264"
        or streams["audio"]["codec_name"] != "aac"
        or abs(dur - expected_duration) > 0.12
    ):
        raise ValueError(f"Output failed validation: {pic}, duration={dur} vs {expected_duration}")
    run("ffmpeg", "-v", "error", "-xerror", "-i", video_path, "-f", "null", "-")
    return info


def infer_segment_durations(words, total_duration, count):
    """Split timeline at major spoken phrase pauses when reusing pre-aligned audio."""
    breaks = []
    for idx in range(1, len(words)):
        gap = words[idx]["start"] - words[idx - 1]["end"]
        if gap >= 0.42:
            breaks.append((words[idx - 1]["end"] + words[idx]["start"]) / 2.0)
    if len(breaks) >= count - 1:
        # Pick the count-1 widest pauses
        scored = sorted(
            (
                (words[i]["start"] - words[i - 1]["end"], (words[i - 1]["end"] + words[i]["start"]) / 2.0)
                for i in range(1, len(words))
            ),
            reverse=True,
        )[: count - 1]
        cuts = sorted(pt for _, pt in scored)
        bounds = [0.0, *cuts, total_duration]
        return [bounds[i + 1] - bounds[i] for i in range(count)]
    return [total_duration / count] * count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--style", type=Path, default=STYLE_LIB / "warm-editorial.json")
    parser.add_argument("--brand", type=Path, default=BRAND_DIR / "sample-studio.json")
    parser.add_argument("--voice", type=Path, help="Optional local English WAV/MP3 override")
    parser.add_argument("--words", type=Path, help="Optional pre-computed words.json matching --voice")
    parser.add_argument("--preset", choices=sorted(EXPORT_PRESETS), default="premium")
    parser.add_argument("--fps", type=int, help="Override FPS (e.g. 30 or 60)")
    parser.add_argument("--crf", type=int, help="Override H.264 CRF (e.g. 15-24)")
    parser.add_argument("--skip-render", action="store_true", help="Run lint/check only without MP4 encode")
    args = parser.parse_args()

    preset_cfg = EXPORT_PRESETS[args.preset]
    render_fps = args.fps or preset_cfg["fps"]
    render_crf = args.crf if args.crf is not None else preset_cfg["crf"]
    render_quality = preset_cfg["quality"]

    style_cfg = json.loads(args.style.read_text(encoding="utf-8"))
    brand_cfg = json.loads(args.brand.read_text(encoding="utf-8"))

    run_id = f"phase2-{style_cfg['id']}-{uuid4().hex[:8]}"
    project = CACHE / run_id
    assets_dir = project / "assets"
    vendor_dir = assets_dir / "vendor"
    comp_dir = project / "compositions"
    vendor_dir.mkdir(parents=True)
    comp_dir.mkdir(parents=True)

    shutil.copy2(STYLE_LIB / "master-stylesheet.css", assets_dir / "master-stylesheet.css")
    shutil.copy2(ENGINE / "assets" / "iso-backdrop.js", assets_dir / "iso-backdrop.js")
    shutil.copy2(GSAP_VENDOR, vendor_dir / "gsap.min.js")
    shutil.copy2(THREE_VENDOR, vendor_dir / "three.module.min.js")

    voice_wav = project / "voice.wav"
    if args.voice:
        run(
            "ffmpeg", "-nostdin", "-v", "error", "-y", "-i", args.voice.resolve(),
            "-vn", "-ac", "1", "-ar", "16000", voice_wav,
        )
        total_duration = probe_duration(voice_wav)
        if args.words and args.words.is_file():
            words = json.loads(args.words.read_text(encoding="utf-8"))
        else:
            words = transcribe_words(voice_wav, total_duration)
        part_durations = infer_segment_durations(words, total_duration, len(DEFAULT_SEGMENTS))
    else:
        part_durations = synthesize_voice(DEFAULT_SEGMENTS, voice_wav)
        total_duration = probe_duration(voice_wav)
        words = transcribe_words(voice_wav, total_duration)
    (project / "words.json").write_text(json.dumps(words, indent=2) + "\n", encoding="utf-8")
    groups = group_captions(words)

    # Compute contiguous scene slots matching spoken segments + end card hold
    scene_slots = []
    cursor = 0.0
    for idx, seg in enumerate(DEFAULT_SEGMENTS):
        if idx < len(DEFAULT_SEGMENTS) - 1:
            dur = part_durations[idx]
        else:
            dur = max(2.8, total_duration - cursor)
        vars_copy = dict(seg["vars"])
        if seg["id"] == "scene4":
            vars_copy.update({
                "headlineMain": "Comment",
                "headlineAccent": f"{brand_cfg['ctaKeyword']} below",
                "ctaKeyword": brand_cfg["ctaKeyword"],
                "replyText": "Demo link sent to inbox",
                "brandName": brand_cfg["brandName"],
                "monogram": brand_cfg["monogram"],
                "handle": brand_cfg["handle"],
                "tagline": brand_cfg["tagline"],
            })
        scene_slots.append({"id": seg["id"], "start": cursor, "duration": dur, "vars": vars_copy})

        comp_src = (COMPONENTS / seg["component"]).read_text(encoding="utf-8")
        comp_out = (
            comp_src.replace("__COMP_ID__", seg["id"])
            .replace("__DURATION__", f"{dur:.3f}")
        )
        (comp_dir / f"{seg['id']}.html").write_text(comp_out, encoding="utf-8")
        cursor += dur

    (comp_dir / "captions.html").write_text(
        build_captions_html(groups, total_duration, style_cfg, brand_cfg),
        encoding="utf-8",
    )
    (project / "index.html").write_text(
        build_index_html(style_cfg, brand_cfg, scene_slots, total_duration),
        encoding="utf-8",
    )

    env = {
        **os.environ,
        "DO_NOT_TRACK": "1",
        "HYPERFRAMES_CACHE_DIR": str(CACHE / "hyperframes"),
        "HYPERFRAMES_CHROME_SANDBOX": "1",
    }
    if chrome := browser_path():
        env["HYPERFRAMES_BROWSER_PATH"] = str(chrome)

    cli = ("node", str(ENGINE / "node_modules" / "hyperframes" / "dist" / "cli.js"))
    print(f"Staged Phase 2 preview project: {project} ({total_duration:.2f}s, {len(words)} words)", flush=True)
    run(*cli, "lint", project, cwd=ENGINE, env=env)
    check_res = run(*cli, "check", project, "--safe-zone", "reels", "--json", cwd=ENGINE, env=env, capture_output=True, text=True)
    (project / "check.json").write_text(check_res.stdout, encoding="utf-8")
    check_data = json.loads(check_res.stdout)
    print(f"HyperFrames check summary: {json.dumps(check_data.get('summary', check_data.get('status')))}", flush=True)

    if args.skip_render:
        return

    EXPORTS.mkdir(exist_ok=True)
    out_mp4 = EXPORTS / f"{run_id}-{args.preset}-{render_fps}fps.mp4"
    run(
        *cli, "render", project, "--output", out_mp4,
        "--fps", str(render_fps), "--quality", render_quality, "--crf", str(render_crf),
        "--workers", "1", "--strict", "--no-best-effort",
        cwd=ENGINE, env=env,
    )
    info = verify_video(out_mp4, total_duration, render_fps)
    v_stream = next(s for s in info["streams"] if s["codec_type"] == "video")
    print(
        f"Verified Phase 2 preview MP4: {out_mp4} "
        f"(preset={args.preset}, quality={render_quality}, crf={render_crf}, "
        f"fps={v_stream['avg_frame_rate']}, bitrate={v_stream.get('bit_rate')})"
    )


if __name__ == "__main__":
    main()
