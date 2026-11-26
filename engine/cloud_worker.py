"""GitHub runner entrypoint for the repository-owned synthetic WebGL & UI validation fixtures."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
from time import monotonic


PRESETS = {"draft": (540, 960, 30, 24, "draft"),
           "standard": (1080, 1920, 30, 18, "standard"),
           "premium": (1080, 1920, 60, 15, "high")}

FIXTURES = ("smoke", "premium-showcase", "editorial-showcase")


def smoke(stage, preset, vendor):
    width, height, fps, _, _ = PRESETS[preset]
    (stage / "assets").mkdir(exist_ok=True)
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
    return {"preset": preset, "fixture": "smoke", "frames": 2 * fps, "duration": 2.0}


def _ttf_glyph_widths(font_path: Path):
    """Parse head/hhea/OS/2/cmap/hmtx tables cleanly with pure Python stdlib."""
    data = font_path.read_bytes()
    num_tables = struct.unpack(">H", data[4:6])[0]
    tables = {}
    for i in range(num_tables):
        off = 12 + i * 16
        tag, _, offset, length = struct.unpack(">4sIII", data[off:off + 16])
        tables[tag.decode("latin1")] = (offset, length)

    def view(name):
        off, ln = tables[name]
        return memoryview(data)[off:off + ln]

    upem = struct.unpack(">H", view("head")[18:20])[0]
    win_ratio = 0.565
    if "OS/2" in tables and len(view("OS/2")) >= 78:
        wa, wd = struct.unpack(">HH", view("OS/2")[74:78])
        if wa + wd > 0:
            win_ratio = upem / (wa + wd)
    hhea = view("hhea")
    num_hmetrics = struct.unpack(">H", hhea[34:36])[0]
    hmtx = view("hmtx")
    advances = [struct.unpack(">H", hmtx[i * 4:i * 4 + 2])[0] for i in range(num_hmetrics)]
    last_adv = advances[-1] if advances else round(upem * 0.55)

    # Parse cmap format 4 subtable (Windows Unicode BMP)
    cmap = view("cmap")
    sub_count = struct.unpack(">H", cmap[2:4])[0]
    sub_off = None
    for i in range(sub_count):
        pid, eid, offset = struct.unpack(">HHI", cmap[4 + i * 8:12 + i * 8])
        if (pid, eid) in ((3, 1), (0, 3), (0, 4)):
            fmt = struct.unpack(">H", cmap[offset:offset + 2])[0]
            if fmt == 4:
                sub_off = offset
                break
    glyph_map = {}
    if sub_off is not None:
        sub = cmap[sub_off:]
        seg_x2 = struct.unpack(">H", sub[6:8])[0]
        segs = seg_x2 // 2
        end_codes = struct.unpack(f">{segs}H", sub[14:14 + seg_x2])
        start_codes = struct.unpack(f">{segs}H", sub[16 + seg_x2:16 + 2 * seg_x2])
        id_deltas = struct.unpack(f">{segs}h", sub[16 + 2 * seg_x2:16 + 3 * seg_x2])
        id_range_offsets = struct.unpack(f">{segs}H", sub[16 + 3 * seg_x2:16 + 4 * seg_x2])
        for i in range(segs):
            sc, ec, delta, ro = start_codes[i], end_codes[i], id_deltas[i], id_range_offsets[i]
            if sc == 0xFFFF and ec == 0xFFFF:
                break
            for cp in range(sc, min(ec, 0x2000) + 1):
                if ro == 0:
                    gid = (cp + delta) & 0xFFFF
                else:
                    idx_pos = (16 + 3 * seg_x2 + i * 2) + ro + (cp - sc) * 2
                    gid = struct.unpack(">H", sub[idx_pos:idx_pos + 2])[0] if idx_pos + 2 <= len(sub) else 0
                    if gid:
                        gid = (gid + delta) & 0xFFFF
                glyph_map[cp] = gid

    def measure(text: str, font_px: float) -> float:
        scale = (font_px * win_ratio) / max(1, upem)
        total = 0.0
        for ch in text:
            gid = glyph_map.get(ord(ch), 0)
            adv = advances[gid] if gid < len(advances) else last_adv
            total += adv * scale
        return total

    return measure


def _pill_ass_path(w: float, h: float, r: int = 14) -> str:
    r = min(r, w / 2, h / 2)
    x0, y0, x1, y1 = 0, 0, round(w), round(h)
    k = round(r * 0.552)
    return (
        f"m {x0 + r} {y0} l {x1 - r} {y0} "
        f"b {x1 - r + k} {y0} {x1} {y0 + r - k} {x1} {y0 + r} l {x1} {y1 - r} "
        f"b {x1} {y1 - r + k} {x1 - r + k} {y1} {x1 - r} {y1} l {x0 + r} {y1} "
        f"b {x0 + r - k} {y1} {x0} {y1 - r + k} {x0} {y1 - r} l {x0} {y0 + r} "
        f"b {x0} {y0 + r - k} {x0 + r - k} {y0} {x0 + r} {y0}"
    )


def _stamp_ass(sec: float) -> str:
    cs = round(sec * 100)
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def build_synthetic_hinglish_ass(ass_path: Path, font_path: Path | None, lines_spec: list[tuple] | None = None) -> dict:
    """Create 12.0s Hinglish + Devanagari ASS captions matching active-word #FFD400 styling."""
    measure = _ttf_glyph_widths(font_path) if (font_path and font_path.is_file()) else (lambda txt, px: len(txt) * px * 0.48)
    font_size = 70
    margin_v = 340
    pill_h = round(font_size * 0.78)
    pill_y = round(1920 - margin_v - font_size * 0.59 - pill_h / 2)
    center_x = 90 + (1080 - 90 - 130) / 2

    lines_spec = lines_spec or [
        (0.25, 3.85, ["Local", "3D", "engine", "तेज़", "है"], "Default"),
        (4.15, 7.85, ["Smart", "UI", "aur", "commit", "sync"], "Default"),
        (8.15, 11.75, ["Cloud", "render", "1080p", "तैयार", "है"], "Default"),
    ]
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Poppins,{font_size},&H00121418,&H00FFFFFF,&H00181412,&H70181412,-1,0,0,0,100,100,0,0,1,0,0,2,90,130,{margin_v},1
Style: DarkScene,Poppins,{font_size},&H00FCFAF8,&H00FFFFFF,&H00181412,&H70181412,-1,0,0,0,100,100,0,0,1,0,0,2,90,130,{margin_v},1
Style: Pill,Arial,10,&H0000D4FF,&H0000D4FF,&H0000D4FF,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    total_words = 0
    for start, end, words, style_name in lines_spec:
        step = (end - start) / len(words)
        full_line = " ".join(words)
        full_w = measure(full_line, font_size)
        x_left = center_x - full_w / 2
        for idx, word in enumerate(words):
            total_words += 1
            ws = start + idx * step
            we = start + (idx + 1) * step
            prefix = " ".join(words[:idx]) + (" " if idx > 0 else "")
            px = x_left + measure(prefix, font_size) - 7
            pw = measure(word, font_size) + 14
            events.append(
                f"Dialogue: 0,{_stamp_ass(ws)},{_stamp_ass(we)},Pill,,0,0,0,,{{\\pos({round(px)},{pill_y})\\p1}}{_pill_ass_path(pw, pill_h, 14)}{{\\p0}}"
            )
            events.append(f"Dialogue: 1,{_stamp_ass(ws)},{_stamp_ass(we)},{style_name},,0,0,0,,{full_line}")
    ass_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return {"captionWords": total_words, "captionLines": len(lines_spec)}


def premium_validation_fixture(stage: Path, preset: str, vendor: Path, duration: float = 12.0) -> dict:
    """Build original synthetic 12s 3D + GSAP UI + Hinglish benchmark composition."""
    width, height, fps, _, _ = PRESETS[preset]
    assets = stage / "assets"
    assets.mkdir(exist_ok=True)
    three_src = vendor / "node_modules/three/build/three.module.min.js"
    gsap_matches = sorted((vendor / "node_modules/hyperframes/dist/vendor").glob("**/gsap.min.js"))
    if not gsap_matches:
        raise ValueError("Bundled gsap.min.js missing inside hyperframes dependencies.")
    gsap_src = gsap_matches[-1]
    (assets / "three.module.min.js").write_bytes(three_src.read_bytes())
    (assets / "gsap.min.js").write_bytes(gsap_src.read_bytes())

    fonts_src = vendor / "fonts"
    font_face_rules = []
    if fonts_src.is_dir():
        fonts_dst = assets / "fonts"
        fonts_dst.mkdir(exist_ok=True)
        for name in ("Poppins-Regular.ttf", "Poppins-Bold.ttf", "Hind-Regular.ttf", "Hind-Bold.ttf"):
            src_f = fonts_src / name
            if src_f.is_file():
                (fonts_dst / name).write_bytes(src_f.read_bytes())
                fam = "Poppins" if name.startswith("Poppins") else "Hind"
                wt = 700 if "Bold" in name else 400
                font_face_rules.append(
                    f'@font-face{{font-family:"{fam}";src:url("assets/fonts/{name}") format("truetype");font-weight:{wt};font-style:normal;}}'
                )

    scale = width / 1080.0
    frames = round(duration * fps)
    html = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<style>
{"".join(font_face_rules)}
html, body {{ margin:0; padding:0; background:#f5f7fc; }}
#production {{ position:relative; width:{width}px; height:{height}px; overflow:hidden; }}
#stage {{
  position:absolute; inset:0; width:1080px; height:1920px;
  transform:scale({scale:.9f}); transform-origin:top left;
  font-family:"Poppins","Hind",sans-serif; color:#141c3a;
  background:
    radial-gradient(circle at 86% 12%, rgba(99,102,241,0.16) 0%, transparent 44%),
    radial-gradient(circle at 14% 78%, rgba(14,165,233,0.14) 0%, transparent 46%),
    radial-gradient(ellipse at 52% 45%, rgba(227,236,255,0.70) 0%, transparent 58%),
    radial-gradient(rgba(114,134,206,0.24) 1.8px, transparent 1.8px) 0 0 / 28px 28px,
    #f5f7fc;
}}
#stage * {{ box-sizing:border-box; }}
.rail {{ position:absolute; top:0; left:0; width:1080px; height:7px; background:rgba(15,23,42,0.08); z-index:40; }}
.rail-fill {{ width:100%; height:100%; transform-origin:left center; background:linear-gradient(90deg,#ea580c,#ffd400,#4350d8); }}
.hdr-left {{
  position:absolute; top:204px; left:68px; z-index:20; display:flex; align-items:center; gap:16px;
  background:#fffef9; padding:10px 24px 10px 12px; border:1.5px solid #e2e8f0; border-radius:999px;
  box-shadow:0 12px 26px rgba(15,23,42,0.09);
}}
.hdr-avatar {{
  width:58px; height:58px; border-radius:50%; background:linear-gradient(135deg,#ea580c,#ffd400);
  display:flex; align-items:center; justify-content:center; font-weight:800; font-size:24px; color:#121418;
}}
.hdr-right {{
  position:absolute; top:212px; right:68px; z-index:20; display:inline-flex; align-items:center; gap:10px;
  background:rgba(255,255,255,0.94); padding:10px 22px; border:1.5px solid #cbd7f6; border-radius:999px;
  box-shadow:0 12px 26px rgba(15,23,42,0.09); font-size:22px; font-weight:700; letter-spacing:0.05em;
}}
.dot {{ width:12px; height:12px; border-radius:50%; background:#ea580c; box-shadow:0 0 0 5px rgba(234,88,12,0.16); }}
.clip {{ position:absolute; inset:0; }}
.heading {{
  position:absolute; top:344px; left:68px; width:944px; margin:0; text-align:center;
  font-size:68px; font-weight:700; line-height:1.08; letter-spacing:-0.03em; color:#141c3a;
}}
.heading em {{ font-style:normal; color:#ea580c; }}
.stage-box {{ position:absolute; top:478px; left:68px; width:944px; height:944px; display:flex; flex-direction:column; gap:18px; }}
.three-frame {{
  position:relative; width:100%; height:708px; border-radius:28px; overflow:hidden;
  background:radial-gradient(circle at 52% 40%, #1e293b 0%, #0f172a 68%, #090d1a 100%);
  border:2px solid #2b3954; box-shadow:0 28px 60px rgba(15,23,42,0.22);
}}
.three-frame canvas {{ display:block; width:100%; height:100%; }}
.hud-top, .hud-bot {{ position:absolute; left:22px; right:22px; display:flex; justify-content:space-between; align-items:center; pointer-events:none; z-index:6; }}
.hud-top {{ top:20px; }} .hud-bot {{ bottom:20px; }}
.pill {{
  display:inline-flex; align-items:center; gap:8px; padding:9px 18px; border-radius:999px;
  font-size:22px; font-weight:700; background:#eef2ff; color:#141c3a; border:1.5px solid #c7d5f7;
}}
.pill-dark {{ background:#1e293b; color:#f8fafc; border-color:#334155; }}
.pill-accent {{ background:#ea580c; color:#ffffff; border-color:transparent; }}
.pill-ok {{ background:#dcfce7; color:#065f46; border-color:#6ee7b7; }}
.dock {{
  flex:1; background:#ffffff; border:2px solid #cfdcf8; border-radius:26px; padding:20px 26px;
  box-shadow:0 18px 40px rgba(15,23,42,0.09); display:grid; grid-template-columns:1fr auto 1fr auto 1fr; align-items:center; gap:14px;
}}
.t-node {{ background:#f1f5f9; border:1.5px solid #cbd5e1; border-radius:18px; padding:14px 16px; }}
.t-node strong {{ display:block; font-size:24px; }} .t-node span {{ font-size:18px; color:#475569; font-weight:600; }}
.win {{
  flex:1; position:relative; background:#ffffff; border:2px solid #cfdcf8; border-radius:30px;
  box-shadow:0 26px 58px rgba(15,23,42,0.11); overflow:hidden; display:flex; flex-direction:column;
}}
.win-dark {{ background:linear-gradient(165deg,#111827 0%,#0b1120 100%); border-color:#283548; color:#f8fafc; }}
.win-head {{
  display:flex; align-items:center; justify-content:space-between; padding:16px 24px;
  border-bottom:1.5px solid rgba(148,163,184,0.22); background:rgba(15,23,42,0.04);
}}
.dots {{ display:flex; gap:9px; }} .dots i {{ width:14px; height:14px; border-radius:50%; display:block; font-style:normal; }}
.url {{ flex:1; margin:0 16px; padding:7px 18px; border-radius:999px; background:#0f172a; color:#94a3b8; font-family:monospace; font-size:21px; }}
.stamp {{
  display:inline-flex; padding:8px 20px; border-radius:14px; border:4px solid currentColor;
  font-family:monospace; font-size:24px; font-weight:800; letter-spacing:0.08em; transform:rotate(-7deg);
}}
.cursor {{ position:absolute; width:42px; height:42px; z-index:25; filter:drop-shadow(0 8px 12px rgba(15,23,42,0.34)); }}
</style>
</head>
<body>
<main id="production" data-composition-id="production" data-start="0" data-duration="{duration:.2f}" data-width="{width}" data-height="{height}">
<div id="stage">
  <div class="rail"><div class="rail-fill"></div></div>
  <aside class="hdr-left"><div class="hdr-avatar">VR</div><div><strong style="display:block;font-size:27px">Synth Studio</strong><small style="font-size:18px;color:#475569">@render.engine.test</small></div></aside>
  <div class="hdr-right"><span class="dot"></span><span>ENGINE BENCHMARK</span></div>

  <!-- Act 1: 0..4s High-Detail 3D Aluminum MacBook Pro Workstation -->
  <section id="act-workstation" class="clip" data-start="0" data-duration="4" data-track-index="0">
    <h1 class="heading">Local 3D <em>Workstation</em></h1>
    <div class="stage-box">
      <div class="three-frame" id="mount-laptop">
        <div class="hud-top"><span class="pill pill-dark">$ build --preset premium</span><span class="pill pill-accent">PBR 60 FPS</span></div>
        <div class="hud-bot"><span class="pill pill-dark">WEBGL 2.0 + SHADOWS</span><span class="pill pill-ok" id="ws-stamp">SNAPSHOTTED ✓</span></div>
      </div>
      <div class="dock">
        <div class="t-node"><strong>Scene AST</strong><span>Compiled Locally</span></div>
        <strong style="font-size:26px;color:#ea580c">→</strong>
        <div class="t-node"><strong>Shader Pipeline</strong><span>PCFSoftShadowMap</span></div>
        <strong style="font-size:26px;color:#10b981">→</strong>
        <div class="t-node" style="border-color:#6ee7b7;background:#ecfdf5"><strong>1080×1920</strong><span style="color:#065f46">60fps Verified ✓</span></div>
      </div>
    </div>
  </section>

  <!-- Act 2: 4..8s Interactive Cloud Sync & Merge Software UI -->
  <section id="act-ui" class="clip" data-start="4" data-duration="4" data-track-index="0">
    <h1 class="heading">Cloud Sync <em>Pipeline</em></h1>
    <div class="stage-box">
      <div class="win" style="position:relative">
        <div class="win-head">
          <div class="dots"><i style="background:#ef4444"></i><i style="background:#f59e0b"></i><i style="background:#10b981"></i></div>
          <div class="url" style="background:#eef2ff;color:#334155">studio-pipeline · synthetic-fixture</div>
          <span class="pill pill-accent" id="ui-btn">Merge &amp; Sync</span>
        </div>
        <div style="padding:28px;display:flex;flex-direction:column;gap:20px;flex:1;justify-content:space-between;position:relative">
          <svg viewBox="0 0 880 120" style="width:100%;height:120px;overflow:visible">
            <path id="dag-base" d="M70 72 H810" stroke="#94a3b8" stroke-width="6" fill="none"/>
            <path id="dag-arc" d="M160 72 C310 12, 570 12, 720 72" stroke="#10b981" stroke-width="7" stroke-dasharray="12 8" fill="none"/>
            <circle cx="160" cy="72" r="18" fill="#4350d8" stroke="#fff" stroke-width="4"/>
            <circle cx="440" cy="72" r="18" fill="#ea580c" stroke="#fff" stroke-width="4"/>
            <circle cx="720" cy="72" r="18" fill="#10b981" stroke="#fff" stroke-width="4"/>
          </svg>
          <div class="win win-dark" style="padding:24px 28px;flex:none;gap:12px">
            <div style="font-family:monospace;font-size:26px;color:#38bdf8">$ engine render --fps 60 --crf 15</div>
            <div style="font-family:monospace;font-size:21px;color:#34d399">↳ Validated 720 frames · H.264 High Profile · zero frame drops</div>
            <div style="height:14px;background:#1e293b;border-radius:999px;overflow:hidden"><div id="ui-bar" style="width:0%;height:100%;background:linear-gradient(90deg,#ea580c,#10b981);border-radius:999px"></div></div>
          </div>
          <div style="display:flex;justify-content:space-between;align-items:center;padding:22px 26px;border-radius:22px;background:#f0fdf4;border:2px solid #6ee7b7" id="ui-res">
            <div><strong style="font-size:32px;display:block">Artifact Master Ready</strong><span style="font-size:21px;color:#065f46;font-family:monospace">SHA-256 verified · libass captions</span></div>
            <div class="stamp" id="ui-stamp" style="color:#059669;background:#ecfdf5">SYNCED ✓</div>
          </div>
          <svg class="cursor" id="ui-cursor" viewBox="0 0 36 36"><path d="M4 3 L14 31 L18 20 L29 17 Z" fill="#141c3a" stroke="#ffffff" stroke-width="2.5" stroke-linejoin="round"/></svg>
        </div>
      </div>
    </div>
  </section>

  <!-- Act 3: 8..12s Titanium 3D Smartphone & Hinglish Output Verification -->
  <section id="act-phone" class="clip" data-start="8" data-duration="4" data-track-index="0">
    <h1 class="heading">Premium Reel <em>Ready</em></h1>
    <div class="stage-box">
      <div class="three-frame" id="mount-phone">
        <div class="hud-top"><span class="pill pill-dark">TITANIUM OLED 3D</span><span class="pill pill-accent">1080×1920 @ 60FPS</span></div>
        <div class="hud-bot"><span class="pill pill-ok" id="ph-stamp">CRF 15 · CLOUD VERIFIED ✓</span></div>
      </div>
      <div class="dock" style="grid-template-columns:1fr 1fr">
        <div class="t-node"><strong>Hinglish ASS Burn</strong><span>Poppins + Hind OFL</span></div>
        <div class="t-node" style="border-color:#a5b4fc;background:#eef2ff"><strong>Zero Local Encode</strong><span style="color:#3730a3">Runner Executed ✓</span></div>
      </div>
    </div>
  </section>
</div>
</main>
<script src="assets/gsap.min.js"></script>
<script type="module">
import * as T from "./assets/three.module.min.js";
window.__hf = window.__hf || {{}};
window.__hf.buildReady = window.__hf.buildReady || {{}};
const tl = window.reelTimeline = gsap.timeline({{ paused: true }});
window.__timelines = window.__timelines || {{}};
window.__timelines.production = tl;
tl.to({{}}, {{ duration: {duration:.2f} }}, 0);
tl.fromTo(".rail-fill", {{ scaleX: 0 }}, {{ scaleX: 1, duration: {duration:.2f}, ease: "none" }}, 0);

// Act 1 & 3 headings + stamps
for (const [id, st] of [["act-workstation", 0], ["act-ui", 4], ["act-phone", 8]]) {{
  tl.fromTo("#" + id + " .heading", {{ opacity: 0, y: 20 }}, {{ opacity: 1, y: 0, duration: 0.4, ease: "power3.out" }}, st);
}}
tl.fromTo("#ws-stamp", {{ opacity: 0, scale: 0.7 }}, {{ opacity: 1, scale: 1, duration: 0.35, ease: "back.out(1.6)" }}, 0.85);
tl.fromTo("#dag-base", {{ strokeDasharray: 760, strokeDashoffset: 760 }}, {{ strokeDashoffset: 0, duration: 0.7, ease: "power2.out" }}, 4.2);
tl.fromTo("#dag-arc", {{ strokeDasharray: 760, strokeDashoffset: 760 }}, {{ strokeDashoffset: 0, duration: 0.85, ease: "power2.inOut" }}, 4.5);
tl.fromTo("#ui-bar", {{ width: "0%" }}, {{ width: "100%", duration: 1.1, ease: "power2.inOut" }}, 4.65);
tl.fromTo("#ui-cursor", {{ opacity: 0, x: 560, y: 460 }}, {{ opacity: 1, x: 775, y: 22, duration: 0.55, ease: "power2.out" }}, 4.95);
tl.to("#ui-cursor", {{ scale: 0.84, duration: 0.1, yoyo: true, repeat: 1 }}, 5.55);
tl.to("#ui-btn", {{ backgroundColor: "#059669", textContent: "✓ Merged & Synced", duration: 0.22 }}, 5.65);
tl.fromTo("#ui-stamp", {{ opacity: 0, scale: 1.5, rotation: -14 }}, {{ opacity: 1, scale: 1, rotation: -7, duration: 0.35, ease: "back.out(1.7)" }}, 5.85);
tl.to("#ui-cursor", {{ opacity: 0, duration: 0.25 }}, 6.1);
tl.fromTo("#ph-stamp", {{ opacity: 0, scale: 0.75 }}, {{ opacity: 1, scale: 1, duration: 0.35, ease: "back.out(1.6)" }}, 8.75);

const makeScreen = (kind) => {{
  const c = document.createElement("canvas");
  c.width = kind === "phone" ? 512 : 1024;
  c.height = kind === "phone" ? 960 : 640;
  const g = c.getContext("2d");
  g.fillStyle = "#0b1120";
  g.fillRect(0, 0, c.width, c.height);
  if (kind === "laptop") {{
    g.fillStyle = "#1e293b"; g.fillRect(0, 0, 1024, 54);
    ["#ef4444","#f59e0b","#10b981"].forEach((col,i)=>{{ g.fillStyle=col; g.beginPath(); g.arc(34+i*28,27,9,0,6.3); g.fill(); }});
    g.fillStyle = "#cbd5e1"; g.font = "bold 22px monospace"; g.fillText("studio-benchmark — main", 145, 34);
    g.fillStyle = "#0f172a"; g.fillRect(0, 54, 240, 586);
    g.fillStyle = "#34d399"; g.fillRect(265, 92, 6, 48);
    g.fillStyle = "#e2e8f0"; g.font = "24px monospace"; g.fillText("+ const mesh = laptopRig(PBR);", 288, 124);
    g.fillStyle = "#38bdf8"; g.fillText("  renderer.shadowMap = Soft;", 288, 178);
    g.fillStyle = "#090d1a"; g.fillRect(240, 320, 784, 320);
    g.fillStyle = "#38bdf8"; g.fillText("$", 272, 392);
    g.fillStyle = "#f8fafc"; g.fillText("hyperframes render --fps 60 --crf 15", 302, 392);
    g.fillStyle = "#10b981"; g.fillRect(272, 544, 260, 42);
    g.fillStyle = "#fff"; g.font = "bold 22px sans-serif"; g.fillText("60 FPS CAPTURE ✓", 294, 572);
  }} else {{
    g.fillStyle = "#111827"; g.fillRect(0, 0, 512, 960);
    g.fillStyle = "#1e293b"; g.fillRect(28, 92, 456, 150);
    g.fillStyle = "#f8fafc"; g.font = "bold 32px sans-serif"; g.fillText("1080×1920 @ 60fps", 52, 160);
    g.fillStyle = "#38bdf8"; g.font = "bold 24px monospace"; g.fillText("CRF 15 · H.264 + ASS", 52, 208);
    g.fillStyle = "#162238"; g.fillRect(28, 270, 456, 260);
    g.strokeStyle = "#ea580c"; g.lineWidth = 8; g.beginPath();
    [470, 430, 445, 375, 340, 310].forEach((py,i)=>{{ const px=62+i*74; if(i===0) g.moveTo(px,py); else g.lineTo(px,py); }});
    g.stroke();
    g.fillStyle = "#059669"; g.fillRect(28, 560, 456, 120);
    g.fillStyle = "#fff"; g.font = "bold 28px sans-serif"; g.fillText("SYNTHETIC PASSED ✓", 56, 630);
  }}
  const t = new T.CanvasTexture(c); t.colorSpace = T.SRGBColorSpace; return t;
}};

const initScene = (mountId, isPhone, startSec) => {{
  const mount = document.getElementById(mountId);
  const w = 944, h = 708;
  const renderer = new T.WebGLRenderer({{ alpha: true, antialias: true, powerPreference: "low-power" }});
  if (!renderer.capabilities.isWebGL2) throw new Error("WebGL 2 unavailable");
  renderer.setSize(w, h, false);
  renderer.setClearColor(0x000000, 0);
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = T.PCFSoftShadowMap;
  renderer.toneMapping = T.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.18;
  mount.append(renderer.domElement);

  const world = new T.Scene();
  const camera = new T.PerspectiveCamera(32, w / h, 0.1, 80);
  world.add(new T.HemisphereLight(0xdcebff, 0x162044, 1.45));
  const key = new T.DirectionalLight(0xfffaf3, 2.9);
  key.position.set(-5.2, 10.8, 6.2); key.castShadow = true; key.shadow.mapSize.set(1024, 1024);
  key.shadow.camera.left = -6.5; key.shadow.camera.right = 6.5; key.shadow.camera.top = 6.5; key.shadow.camera.bottom = -6.5;
  key.shadow.bias = -0.0012; world.add(key);
  const rim = new T.DirectionalLight(0x38bdf8, 1.9); rim.position.set(6.2, 5.2, -5.8); world.add(rim);
  const aura = new T.PointLight(0x38bdf8, 3.8, 9.5, 1.5); aura.position.set(0.2, 2.2, 1.0); world.add(aura);

  const mat = (col, o={{}}) => new T.MeshStandardMaterial({{ color: col, roughness: 0.36, metalness: 0.25, ...o }});
  const add = (g, m, x, y, z, parent=world, cast=true, recv=true) => {{ const o = new T.Mesh(g,m); o.position.set(x,y,z); o.castShadow=cast; o.receiveShadow=recv; parent.add(o); return o; }};
  const alum = mat(0xd8e0ee, {{ roughness: 0.24, metalness: 0.72 }});
  const dark = mat(0x1e293b, {{ roughness: 0.34, metalness: 0.58 }});
  add(new T.BoxGeometry(10.2, 0.52, 6.8), mat(0x111a3d), 0, -0.82, 0, world, false, true);
  add(new T.BoxGeometry(9.64, 0.18, 6.24), mat(0x23356d), 0, -0.35, 0, world, false, true);

  let hero, ring, beads = [];
  if (!isPhone) {{
    hero = new T.Group();
    add(new T.BoxGeometry(3.85, 0.16, 2.62), alum, 0, 0, 0, hero);
    add(new T.BoxGeometry(1.38, 0.02, 0.82), mat(0x94a3b8), 0, 0.085, 0.78, hero, false, true);
    for (let r=0; r<4; r++) for (let c=0; c<11; c++) {{
      if (r===3 && (c===4||c===6)) continue;
      add(new T.BoxGeometry(r===3&&c===5?0.92:0.24, 0.045, 0.21), mat(0x0f172a), -1.45+c*0.29, 0.105, -0.7+r*0.27, hero, true, false);
    }}
    const pivot = new T.Group(); pivot.position.set(0, 0.10, -1.27);
    add(new T.BoxGeometry(3.85, 2.48, 0.09), alum, 0, 1.24, 0, pivot);
    const tex = makeScreen("laptop");
    add(new T.PlaneGeometry(3.52, 2.16), new T.MeshStandardMaterial({{ map: tex, emissiveMap: tex, emissive: 0xffffff, emissiveIntensity: 0.72 }}), 0, 1.24, 0.065, pivot, false, false);
    pivot.rotation.x = -0.22; hero.add(pivot);
    hero.scale.setScalar(0.90); hero.position.set(-1.15, -0.16, 0.22); hero.rotation.y = 0.30;
    world.add(hero);
    const curve = new T.CatmullRomCurve3([new T.Vector3(0.1,0.12,-0.15), new T.Vector3(1.1,0.75,-0.25), new T.Vector3(2.2,1.15,0.2)]);
    add(new T.TubeGeometry(curve, 36, 0.045, 10, false), mat(0xea580c, {{ emissive: 0xea580c, emissiveIntensity: 0.4 }}), 0, 0, 0);
    const bead = add(new T.SphereGeometry(0.12, 16, 16), mat(0x10b981, {{ emissive: 0x059669, emissiveIntensity: 1.0 }}), 0, 0, 0);
    beads.push({{ curve, bead }});
    ring = add(new T.OctahedronGeometry(0.54, 0), mat(0x4350d8, {{ emissive: 0x4350d8, emissiveIntensity: 0.5 }}), 2.35, 1.35, 0.2);
  }} else {{
    hero = new T.Group();
    add(new T.BoxGeometry(2.38, 4.72, 0.24), alum, 0, 0, 0, hero);
    add(new T.BoxGeometry(2.26, 4.60, 0.26), dark, 0, 0, 0, hero);
    const tex = makeScreen("phone");
    add(new T.PlaneGeometry(2.12, 4.40), new T.MeshStandardMaterial({{ map: tex, emissiveMap: tex, emissive: 0xffffff, emissiveIntensity: 0.65 }}), 0, 0, 0.135, hero, false, false);
    hero.scale.setScalar(0.76);
    hero.position.set(0, 1.42, 0.15);
    hero.rotation.set(-0.08, -0.22, 0.03);
    world.add(hero);
    add(new T.CylinderGeometry(1.55, 1.75, 0.22, 40), dark, 0, -0.35, 0.15);
    ring = add(new T.TorusGeometry(1.85, 0.045, 16, 64), mat(0x4350d8, {{ emissive: 0x38bdf8, emissiveIntensity: 0.7 }}), 0, 1.42, 0.15);
    ring.rotation.x = 1.25;
  }}

  const draw = sec => {{
    const p = Math.max(0, Math.min(1, (sec - startSec) / 4.0));
    if (isPhone) {{
      camera.position.set(Math.sin(p * Math.PI * 0.85) * 0.95, 2.25 + Math.cos(p * Math.PI) * 0.18, 9.65 - p * 0.55);
      camera.lookAt(0, 1.38, 0);
      hero.rotation.y = -0.35 + Math.sin(p * Math.PI) * 0.65;
      hero.position.y = 1.42 + Math.sin(p * Math.PI * 2) * 0.06;
      ring.rotation.z = p * 3.1;
    }} else {{
      camera.position.set(-0.25 + Math.sin(p * Math.PI * 0.85) * 1.05, 2.85 + Math.cos(p * Math.PI) * 0.22, 9.15 - p * 0.62);
      camera.lookAt(0.22, 0.78, 0);
      ring.rotation.y = p * 4.2;
      beads.forEach(b => b.bead.position.copy(b.curve.getPointAt((p * 1.3) % 1)));
    }}
    renderer.render(world, camera);
  }};
  draw(startSec);
  return draw;
}};

window.__hf.buildReady.benchmark = (async () => {{
  const draw1 = initScene("mount-laptop", false, 0);
  const draw3 = initScene("mount-phone", true, 8);
  const onSeek = t => {{
    const sec = Number(t);
    document.querySelectorAll(".clip").forEach(el => {{
      const s = Number(el.dataset.start), d = Number(el.dataset.duration);
      el.style.display = (sec >= s && sec < s + d) ? "block" : "none";
    }});
    tl.seek(sec, false);
    if (sec <= 4.1) draw1(sec);
    if (sec >= 7.9) draw3(sec);
  }};
  onSeek(0);
  window.addEventListener("hf-seek", e => onSeek(e.detail?.time ?? e.detail ?? 0));
}})();
</script>
</body>
</html>
"""
    (stage / "index.html").write_text(html, encoding="utf-8")
    return {"preset": preset, "fixture": "premium-showcase", "frames": frames, "duration": duration}


def editorial_validation_fixture(stage: Path, preset: str, vendor: Path, duration: float = 12.0) -> dict:
    """Build original 12s Warm Editorial Motion + GSAP UI + Hinglish benchmark composition."""
    width, height, fps, _, _ = PRESETS[preset]
    assets = stage / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    gsap_matches = sorted((vendor / "node_modules/hyperframes/dist/vendor").glob("**/gsap.min.js"))
    if not gsap_matches:
        raise ValueError("Bundled gsap.min.js missing inside hyperframes dependencies.")
    (assets / "gsap.min.js").write_bytes(gsap_matches[-1].read_bytes())

    fonts_src = vendor / "fonts"
    font_face_rules = []
    if fonts_src.is_dir():
        fonts_dst = assets / "fonts"
        fonts_dst.mkdir(exist_ok=True)
        for name in ("Poppins-Regular.ttf", "Poppins-Bold.ttf", "Hind-Regular.ttf", "Hind-Bold.ttf"):
            src_f = fonts_src / name
            if src_f.is_file():
                (fonts_dst / name).write_bytes(src_f.read_bytes())
                fam = "Poppins" if name.startswith("Poppins") else "Hind"
                wt = 700 if "Bold" in name else 400
                font_face_rules.append(
                    f'@font-face{{font-family:"{fam}";src:url("assets/fonts/{name}") format("truetype");font-weight:{wt};font-style:normal;}}'
                )

    scale = width / 1080.0
    frames = round(duration * fps)
    html = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<style>
{"".join(font_face_rules)}
html, body {{ margin:0; padding:0; background:#faf5f0; }}
#production {{ position:relative; width:{width}px; height:{height}px; overflow:hidden; }}
#stage {{
  position:absolute; inset:0; width:1080px; height:1920px;
  transform:scale({scale:.9f}); transform-origin:top left;
  font-family:"Poppins","Hind",sans-serif; color:#181412;
  background:
    radial-gradient(circle at 88% 10%, rgba(234,88,12,0.12) 0%, transparent 40%),
    radial-gradient(circle at 12% 84%, rgba(251,146,60,0.11) 0%, transparent 44%),
    radial-gradient(rgba(180,152,132,0.22) 1.5px, transparent 1.5px) 0 0 / 28px 28px,
    #faf5f0;
}}
#stage * {{ box-sizing:border-box; }}
.rail {{ position:absolute; top:0; left:0; width:1080px; height:6px; background:rgba(24,20,18,0.06); z-index:40; }}
.rail-fill {{ width:100%; height:100%; transform-origin:left center; background:linear-gradient(90deg,#ea580c,#ffd400); }}
.hdr-left {{ position:absolute; top:196px; left:68px; z-index:20; display:flex; align-items:center; gap:14px; }}
.hdr-avatar {{ width:58px; height:58px; border-radius:50%; background:linear-gradient(135deg,#ea580c,#ffd400); display:flex; align-items:center; justify-content:center; font-weight:800; font-size:22px; color:#181412; box-shadow:0 8px 18px rgba(24,20,18,0.14); }}
.hdr-right {{ position:absolute; top:200px; right:68px; z-index:20; display:inline-flex; align-items:center; gap:10px; background:rgba(255,255,255,0.95); padding:8px 18px; border:1.5px solid #e5dbd0; border-radius:999px; font-size:21px; font-weight:700; letter-spacing:0.04em; }}
.dot {{ width:10px; height:10px; border-radius:50%; background:#ea580c; }}
.clip {{ position:absolute; inset:0; }}
.heading {{ position:absolute; top:318px; left:68px; width:944px; margin:0; text-align:center; font-size:64px; font-weight:700; line-height:1.10; letter-spacing:-0.025em; }}
.heading em {{ font-family:"Georgia",serif; font-style:italic; font-weight:700; color:#d9480f; }}
.stage-box {{ position:absolute; top:468px; left:68px; width:944px; height:896px; display:flex; flex-direction:column; justify-content:center; gap:22px; }}
.card {{ background:#ffffff; border:1.5px solid #e5dbd0; border-radius:22px; box-shadow:0 18px 40px rgba(24,20,18,0.08); overflow:hidden; }}
.win-head {{ display:flex; align-items:center; justify-content:space-between; gap:12px; padding:13px 20px; border-bottom:1.5px solid rgba(120,113,108,0.16); background:rgba(24,20,18,0.025); }}
.dots {{ display:flex; gap:8px; }} .dots span {{ width:12px; height:12px; border-radius:50%; display:block; }}
.pill {{ display:inline-flex; align-items:center; gap:8px; padding:8px 16px; border-radius:999px; font-size:20px; font-weight:700; background:#fff7ed; color:#9a3412; border:1.5px solid #fed7aa; }}
.pill-accent {{ background:#ea580c; color:#fff; border-color:transparent; }}
.stamp {{ display:inline-flex; padding:7px 16px; border-radius:12px; border:3.5px solid currentColor; font-family:monospace; font-size:21px; font-weight:800; letter-spacing:0.08em; transform:rotate(-7deg); }}
.cur {{ position:absolute; top:0; left:0; width:40px; height:40px; z-index:30; pointer-events:none; filter:drop-shadow(0 8px 12px rgba(24,20,18,0.28)); }}
</style>
</head>
<body>
<div id="production" data-composition-id="editorial-showcase" data-start="0" data-duration="{duration:.2f}" data-width="{width}" data-height="{height}">
<div id="stage">
  <div class="rail"><div class="rail-fill" id="rail-fill"></div></div>
  <div class="hdr-left"><div class="hdr-avatar">ED</div><div><strong style="display:block;font-size:27px">Editorial Motion</strong><small style="font-size:18px;color:#57534e">Synthetic Cloud Fixture</small></div></div>
  <div class="hdr-right"><span class="dot"></span><span>CLOUD VALIDATION</span></div>

  <section class="clip" id="c1" data-start="0" data-duration="4">
    <h1 class="heading">Step 1 <em>folder banao</em></h1>
    <div class="stage-box" style="position:relative">
      <div class="card" id="dropzone" style="padding:28px;border:2px dashed #ea580c;background:#fffaf5;display:flex;flex-direction:column;align-items:center;gap:14px">
        <div style="font-size:30px;font-weight:800">Project Workspace Root</div>
        <span class="pill pill-accent" id="btn-create">Create Folders</span>
      </div>
      <div id="folder-row" style="display:grid;grid-template-columns:repeat(3,1fr);gap:18px">
        <div class="card" style="padding:22px;text-align:center"><div style="font-size:24px;font-weight:800">raw-footage</div><div style="font-size:17px;color:#78716c;margin-top:4px">Clips Ready</div></div>
        <div class="card" style="padding:22px;text-align:center"><div style="font-size:24px;font-weight:800">style-ref</div><div style="font-size:17px;color:#78716c;margin-top:4px">Warm Tokens</div></div>
        <div class="card" style="padding:22px;text-align:center"><div style="font-size:24px;font-weight:800">export</div><div style="font-size:17px;color:#78716c;margin-top:4px">1080p Master</div></div>
      </div>
      <svg class="cur" id="cur1" viewBox="0 0 36 36"><path d="M4 3 L14 31 L18 20 L29 17 Z" fill="#181412" stroke="#fff" stroke-width="2.5"/></svg>
    </div>
  </section>

  <section class="clip" id="c2" data-start="4" data-duration="4" style="display:none">
    <h1 class="heading">Code bigad gaya? <em>Restore</em></h1>
    <div class="stage-box" style="gap:16px">
      <div class="card" style="padding:20px 24px;display:flex;align-items:center;justify-content:space-between;background:#f0fdf4;border-color:#86efac">
        <div><div style="font-size:18px;font-weight:800;color:#059669">v1 · STABLE SNAPSHOT</div><div style="font-size:28px;font-weight:800">Initial Working Build</div></div>
        <span class="pill" id="btn-restore">Restore v1</span>
      </div>
      <div class="card" id="broken-card" style="padding:20px 24px;display:flex;align-items:center;justify-content:space-between;background:#fef2f2;border-color:#fca5a5">
        <div><div style="font-size:18px;font-weight:800;color:#dc2626">v3 · REGRESSION</div><div style="font-size:28px;font-weight:800">Broken Update</div></div>
        <div class="stamp" id="broken-stamp" style="color:#dc2626;background:#fff">BROKEN !</div>
      </div>
      <svg viewBox="0 0 880 96" style="width:100%;height:96px"><path id="rb-arc" d="M760 48 C760 4, 140 4, 140 48" stroke="#059669" stroke-width="6" stroke-dasharray="11 8" fill="none"/></svg>
    </div>
  </section>

  <section class="clip" id="c3" data-start="8" data-duration="4" style="display:none">
    <h1 class="heading">Cloud Sync <em>Verified</em></h1>
    <div class="stage-box" style="gap:20px">
      <div class="card" style="padding:26px;display:flex;align-items:center;gap:28px">
        <svg width="170" height="110" viewBox="0 0 170 110"><path d="M 18 90 A 66 66 0 0 1 152 90" stroke="#ece4da" stroke-width="14" stroke-linecap="round" fill="none"/><path id="gauge-arc" d="M 18 90 A 66 66 0 0 1 152 90" stroke="#ea580c" stroke-width="14" stroke-linecap="round" fill="none"/></svg>
        <div style="flex:1">
          <div style="font-size:66px;font-weight:900;color:#ea580c" id="score-val">92%</div>
          <div style="font-size:25px;font-weight:700;color:#44403c">Word-Anchored Verification Score</div>
        </div>
      </div>
      <div class="card" style="padding:24px 28px;display:flex;align-items:center;justify-content:space-between;background:#f0fdf4;border-color:#86efac">
        <div style="font-size:28px;font-weight:800;color:#065f46">1080x1920 @ 60fps Pipeline</div>
        <div class="stamp" id="sync-stamp" style="color:#059669;background:#fff">READY ✓</div>
      </div>
    </div>
  </section>
</div>
</div>
<script src="assets/gsap.min.js"></script>
<script>
window.__hf = window.__hf || {{}};
window.__hf.buildReady = window.__hf.buildReady || {{}};
const tl = gsap.timeline({{ paused: true }});
tl.fromTo("#rail-fill", {{ scaleX: 0 }}, {{ scaleX: 1, duration: {duration:.2f}, ease: "none" }}, 0);
tl.set("#folder-row", {{ opacity: 0, y: 18 }}, 0);
tl.fromTo("#cur1", {{ opacity: 0, x: 180, y: 310 }}, {{ opacity: 1, x: 470, y: 165, duration: 0.45 }}, 0.8);
tl.to("#btn-create", {{ backgroundColor: "#059669", textContent: "Folders Ready ✓", duration: 0.2 }}, 1.35);
tl.to("#folder-row", {{ opacity: 1, y: 0, duration: 0.45, ease: "expo.out" }}, 1.55);
tl.to("#cur1", {{ opacity: 0, duration: 0.25 }}, 2.05);
tl.set("#broken-stamp", {{ opacity: 0, scale: 1.4 }}, 4.0);
tl.to("#broken-stamp", {{ opacity: 1, scale: 1, duration: 0.35, ease: "back.out(1.6)" }}, 5.1);
const rb = document.getElementById("rb-arc");
const rbLen = rb.getTotalLength();
tl.set(rb, {{ strokeDasharray: rbLen, strokeDashoffset: rbLen }}, 4.0);
tl.to(rb, {{ strokeDashoffset: 0, duration: 0.75, ease: "power2.inOut" }}, 6.1);
tl.to("#btn-restore", {{ backgroundColor: "#059669", color: "#fff", textContent: "Restored ✓", duration: 0.2 }}, 6.4);
const ga = document.getElementById("gauge-arc");
const gaLen = ga.getTotalLength();
tl.set(ga, {{ strokeDasharray: gaLen, strokeDashoffset: gaLen }}, 8.0);
tl.to(ga, {{ strokeDashoffset: gaLen * 0.08, duration: 1.1, ease: "power2.out" }}, 8.6);
tl.fromTo("#sync-stamp", {{ opacity: 0, scale: 1.35 }}, {{ opacity: 1, scale: 1, duration: 0.35, ease: "back.out(1.5)" }}, 10.2);

window.__hf.buildReady.editorial = (async () => {{
  const onSeek = t => {{
    const sec = Number(t);
    document.querySelectorAll(".clip").forEach(el => {{
      const s = Number(el.dataset.start), d = Number(el.dataset.duration);
      el.style.display = (sec >= s && sec < s + d) ? "block" : "none";
    }});
    tl.seek(sec, false);
  }};
  onSeek(0);
  window.addEventListener("hf-seek", e => onSeek(e.detail?.time ?? e.detail ?? 0));
}})();
</script>
</body>
</html>
"""
    (stage / "index.html").write_text(html, encoding="utf-8")
    return {"preset": preset, "fixture": "editorial-showcase", "frames": frames, "duration": duration}


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


def _runner_specs() -> dict:
    mem_kib = None
    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        m = re.search(r"^MemTotal:\s+(\d+)\s+kB", meminfo.read_text(encoding="utf-8"), re.M)
        if m:
            mem_kib = int(m.group(1))
    return {
        "cpuCount": os.cpu_count(),
        "memTotalKiB": mem_kib,
        "machine": platform.machine(),
        "os": platform.platform(),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--preset", choices=PRESETS, required=True)
    parser.add_argument("--fixture", choices=FIXTURES, default="premium-showcase")
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dependencies", type=Path, required=True)
    args = parser.parse_args()
    args.stage.mkdir(parents=True, exist_ok=True)
    args.output.mkdir(parents=True, exist_ok=True)
    if args.fixture == "smoke":
        manifest = smoke(args.stage, args.preset, args.dependencies)
    elif args.fixture == "editorial-showcase":
        manifest = editorial_validation_fixture(args.stage, args.preset, args.dependencies, duration=12.0)
    else:
        manifest = premium_validation_fixture(args.stage, args.preset, args.dependencies, duration=12.0)

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
            codes = re.findall(r'@hf-diagnostic .*?"code":"([A-Z_]+)"', (args.stage.parent / "capture.log").read_text())
            print("Capture failed:", ", ".join(sorted(set(codes))) or f"exit {error.returncode}", flush=True)
            raise SystemExit(1) from None
    capture_sec = monotonic() - started

    encode_started = monotonic()
    master = args.output / "visual-only.mp4"
    caption_meta = {}
    if args.fixture in ("premium-showcase", "editorial-showcase"):
        work = args.stage.parent / "caption-work"
        work.mkdir(exist_ok=True)
        fonts_dir = args.dependencies / "fonts"
        if fonts_dir.is_dir():
            shutil.copytree(fonts_dir, work / "fonts", dirs_exist_ok=True)
        font_probe = work / "fonts" / "Poppins-Bold.ttf"
        if args.fixture == "editorial-showcase":
            caption_meta = build_synthetic_hinglish_ass(
                work / "captions.ass",
                font_probe if font_probe.is_file() else None,
                lines_spec=[
                    (0.30, 3.80, ["Step", "one", "folder", "banao", "jaldi"], "Default"),
                    (4.30, 7.80, ["Code", "bigad", "gaya?", "Restore", "v1"], "Default"),
                    (8.30, 11.75, ["Cloud", "sync", "ab", "verified", "hai"], "Default"),
                ],
            )
        else:
            caption_meta = build_synthetic_hinglish_ass(work / "captions.ass", font_probe if font_probe.is_file() else None)
        vf = "ass=captions.ass:fontsdir=fonts" if (work / "fonts").is_dir() else "ass=captions.ass"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", capture, "-vf", vf,
                        "-an", "-c:v", "libx264", "-crf", str(crf), "-preset", "medium",
                        "-pix_fmt", "yuv420p", "-movflags", "+faststart", master], cwd=work, check=True)
    else:
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", capture, "-map", "0:v:0",
                        "-c:v", "copy", "-an", master], check=True)
    encode_sec = monotonic() - encode_started

    details = probe(master, args.preset, manifest["frames"])
    (args.output / "render-metrics.json").write_text(json.dumps({
        **details,
        **caption_meta,
        "captureSeconds": round(capture_sec, 3),
        "encodeSeconds": round(encode_sec, 3),
        "workerSeconds": round(monotonic() - started, 3),
        "preset": args.preset,
        "fixture": args.fixture,
        "crf": crf,
        "chrome": version,
        "runner": _runner_specs(),
    }, indent=2) + "\n")


if __name__ == "__main__":
    main()
