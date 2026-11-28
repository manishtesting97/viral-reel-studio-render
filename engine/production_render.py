"""Stage reviewed original scenes and export standard video, audio and timing files."""
from html import escape
import json
import math
import os
from pathlib import Path
import re
import shutil
import struct
import tempfile

import asset_library
import asset_render
import sponsorship
from studio_paths import browser_path
from preview_phase2 import EXPORT_PRESETS, EXPORTS, GSAP_VENDOR, THREE_VENDOR, group_captions
from production_plan import BRAND_DIR, CACHE, ENGINE, digest, profiles, run, save, write

CLI = ("node", ENGINE / "node_modules/hyperframes/dist/cli.js")


def browser_env():
    env = {**os.environ, "DO_NOT_TRACK": "1", "HYPERFRAMES_CHROME_SANDBOX": "1",
           "HYPERFRAMES_CACHE_DIR": str(CACHE / "hyperframes")}
    chrome = browser_path()
    if chrome:
        env.setdefault("HYPERFRAMES_BROWSER_PATH", str(chrome))
    return env


def scene_prompts(project, plan, timing):
    brand, style = profiles(plan, project=project)
    sponsor_context = sponsorship.prompt_context(project, plan)
    for scene, actual in zip(plan["scenes"], timing["scenes"]):
        save(project / "prompts" / f"{scene['id']}.json",
             {"assignment": "Build an original scene matching this narration. Use the project "
              "README's reviewed scene source contract. Deliver one HTML fragment and optional "
              "scene JavaScript and local assets. Keep copy in Reels safe zones. Use meaningful "
              "composition and motivated motion. Do not add voice or captions.",
              "scene": scene, "actualTiming": actual, "brand": brand, "referenceStyle": style,
              **({"sponsorship": sponsor_context} if sponsor_context else {}),
              "language": plan["language"], "topic": plan["topic"], "audience": plan["audience"],
              "niche": plan["niche"]})


def source_files(source, plan, assignments=None):
    assignments = assignments or {}
    scenes = {}
    for scene in plan["scenes"]:
        sid = scene["id"]
        if assignments.get(sid, {}).get("mode") in {"image", "video"}:
            continue
        path = source / "scenes" / f"{sid}.html"
        if not path.is_file():
            mode = assignments.get(sid, {}).get("mode", "ai")
            if mode == "hybrid":
                raise ValueError(f'Scene "{scene["heading"]}" uses Hybrid, which needs reviewed visual code and media. '
                                 f'Add {path}, or choose User images or User videos in Step 7 for media only.')
            raise ValueError(f'Scene "{scene["heading"]}" needs reviewed AI visual code: {path}')
        scenes[sid] = path
    assets = source / "assets"
    files = sorted(path for path in assets.rglob("*") if path.is_file())
    if any(path.is_symlink() or not path.resolve().is_relative_to(source.resolve())
           for path in [*scenes.values(), *files]):
        raise ValueError("Reviewed scene sources must stay inside their source directory.")
    for scene in plan["scenes"]:
        if scene["id"] not in scenes:
            continue
        for asset in scene["assets"]:
            if assets / asset not in files:
                raise ValueError(f"Missing required reviewed asset: {asset}")
    if any(path.is_relative_to(assets / "assigned-media") for path in files):
        raise ValueError("assets/assigned-media is reserved for the asset library.")
    if any(path.name in {"gsap.min.js", "three.module.min.js", "timing.js"}
           and path.parent == assets for path in files):
        raise ValueError("Source assets use a reserved vendor filename.")
    return scenes, files


def profile_assets(brand):
    files = {}

    def add(name, value, suffixes):
        path = (BRAND_DIR / value).resolve()
        if (not path.is_relative_to(BRAND_DIR.resolve()) or path.suffix.lower() not in suffixes
                or not path.is_file()):
            raise ValueError(f"Missing or invalid brand asset: {value}")
        files[name] = path
        return path

    picture = brand.get("profilePicture")
    if picture:
        add("profile", picture, {".jpg", ".jpeg", ".png"})
    for lang, settings in brand.get("fonts", {}).items():
        if lang not in {"english", "hindi"} or not re.fullmatch(r"[\w -]+", settings["family"]):
            raise ValueError("Invalid brand font configuration.")
        for weight in ("regular", "bold"):
            add(f"{lang}-{weight}", settings[weight], {".ttf", ".otf"})
    return files


def profile_markup(brand, photo, last_scene, duration, *, mobile=False):
    if not photo:
        return "", ""
    text = {field: escape(str(brand.get(field, ""))) for field in
            ("ownerName", "brandName", "instagramUsername", "profileTitle", "cta")}
    name = text["ownerName"] or text["brandName"]
    handle = f'@{text["instagramUsername"]}' if text["instagramUsername"] else ""
    img = f'<img src="{photo}" alt="" />'
    header = (f'<aside id="profile-header">{img}<div><strong>{name}</strong>'
              f'<small>{text["profileTitle"]}</small></div></aside>')
    if mobile and handle:
        header = f'<aside id="profile-header" class="mobile-profile">{img}<strong>{handle}</strong></aside>'
    if last_scene["id"] not in {"outro", "brand-outro"}:
        return header, ""
    start = last_scene["start"]
    outro = (f'<aside id="profile-outro" class="clip" data-start="{start:.9f}" '
             f'data-duration="{duration-start:.9f}" data-track-index="1">'
             f'<div class="portrait">{img}</div><strong>{name}</strong><span>{handle}</span>'
             f'<span>{text["profileTitle"]}</span><b>{text["cta"]}</b></aside>')
    motion = f"""
const brandTimeline = window.__timelines.production;
brandTimeline.to("#profile-header", {{opacity:0,duration:0.26}}, {start});
brandTimeline.from("#profile-outro .portrait", {{opacity:0,scale:0.78,y:34,duration:0.55,ease:"power3.out"}}, {start});
brandTimeline.from("#profile-outro strong,#profile-outro span,#profile-outro b",
  {{opacity:0,y:28,duration:0.34,stagger:0.075,ease:"power2.out"}}, {start}+0.18);
"""
    return header + outro, motion


def stage_inputs(project, plan, timing, source, *, verify=False):
    assignments, assigned = asset_library.for_project(project, plan, timing, verify=verify)
    scenes, assets = source_files(source, plan, assignments)
    brand, style = profiles(plan, project=project)
    brand_files = profile_assets(brand)
    sponsor, partners, partner_assets = sponsorship.render_context(project, plan, verify=verify, assignments=assignments)
    if any(path.is_relative_to(source / "assets" / "brand-profile") for path in assets):
        raise ValueError("assets/brand-profile is reserved for staged profile assets.")
    if any(path.is_relative_to(source / "assets" / "sponsor-media") for path in assets):
        raise ValueError("assets/sponsor-media is reserved for staged sponsor logos.")
    revision = digest(Path(__file__), plan, timing,
                 GSAP_VENDOR, THREE_VENDOR, {name: digest(path) for name, path in brand_files.items()},
                 {str(p.relative_to(source)): digest(p) for p in [*scenes.values(), *assets]},
                 *([assignments, {key: value["sha256"] for key, value in assigned.items()},
                    Path(asset_render.__file__)] if assignments else []), brand, style,
                 *([sponsor, partners, partner_assets, Path(sponsorship.__file__)] if partners else []))
    return revision, assignments, assigned, scenes, assets, brand, style, brand_files, sponsor, partners


def stage(project, plan, timing, source, preset, *, asset_project=None):
    revision, assignments, assigned, scenes, assets, brand, style, brand_files, sponsor, partners = stage_inputs(
        asset_project or project, plan, timing, source, verify=True)
    key = digest(revision, preset)
    target = project / "renders" / key
    if (target / "index.html").is_file():
        return target
    target.mkdir(parents=True, exist_ok=True)
    for path in assets:
        dest = target / path.relative_to(source)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
    (target / "assets").mkdir(exist_ok=True)
    for path in (GSAP_VENDOR, THREE_VENDOR):
        shutil.copyfile(path, target / "assets" / path.name)
    brand_urls = {}
    for name, path in brand_files.items():
        dest = target / "assets" / "brand-profile" / f"{name}{path.suffix.lower()}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
        brand_urls[name] = dest.relative_to(target).as_posix()
    badge_cfg = plan.get("badge") or {"mode": "normal", "title": plan["topic"]}
    if partners:
        badge_cfg = {**badge_cfg, "mode": "normal"}
    save(target / "timing.json", timing["scenes"])
    write(target / "assets/timing.js",
          "window.reelScenes = " + (target / "timing.json").read_text(encoding="utf-8") + ";\n"
          + "window.reelMetaBadge = " + json.dumps(badge_cfg, ensure_ascii=False) + ";\n")
    width, height = (540, 960) if preset == "draft" else (1080, 1920)
    fps = EXPORT_PRESETS[preset]["fps"]
    render_duration = math.ceil(timing["duration"] * fps) / fps
    tokens = {**style.get("tokens", {}), **brand.get("colors", {})}
    variables = ";".join(f"--{k}:{v}" for k, v in tokens.items()
                         if re.fullmatch(r"(brand|style)-[a-z0-9-]+", k)
                         and isinstance(v, str) and re.fullmatch(r"#[\da-fA-F]{3,8}|rgba?\([\d., %]+\)", v))
    layers = []
    asset_layers = []
    asset_motion = []
    for index, actual in enumerate(timing["scenes"]):
        sid = actual["id"]
        end = render_duration if index + 1 == len(timing["scenes"]) else actual["end"]
        original = scenes[sid].read_text(encoding="utf-8") if sid in scenes else ""
        selection = assignments.get(sid)
        if selection and selection["layers"]:
            addition, effects = asset_render.markup(target, actual, selection, assigned, width, fps, 20+15*index)
            asset_motion.append(effects)
            asset_layers.append(addition)
            branded_scene = partners and (sponsor["mode"] == "full" or sid in sponsor["scenes"]
                                          or (index+1 == len(timing["scenes"]) and sponsor["endCTA"]))
            if sid in scenes:
                original = f'<div class="vrs-original">{original}</div>'
            elif not branded_scene:
                original = '<div class="vrs-meta-badge vrs-media-badge"></div>'
        layers.append(
            f'<section id="{sid}" class="clip scene" data-start="{actual["start"]:.9f}" '
            f'data-duration="{end - actual["start"]:.9f}" data-track-index="0">'
            + original + "</section>")
    scripts = ['<script src="assets/timing.js"></script>', '<script src="assets/gsap.min.js"></script>']
    for sid in scenes:
        path = source / "assets" / f"{sid}.js"
        if path in assets:
            scripts.append(f'<script src="assets/{sid}.js"></script>')
    lang = "hi" if plan["language"] == "hi" else "en"
    font_css = []
    for language, settings in brand.get("fonts", {}).items():
        for weight, value in (("regular", 400), ("bold", 700)):
            url = brand_urls[f"{language}-{weight}"]
            font_css.append(f'@font-face{{font-family:"{settings["family"]}";src:url("{url}");font-weight:{value}}}')
    stack = ", ".join(f'"{brand["fonts"][key]["family"]}"' for key in
                      (("hindi", "english") if lang == "hi" else ("english", "hindi"))
                      if key in brand.get("fonts", {}))
    if stack:
        variables += f';--brand-font:{stack}, sans-serif'
    highlight = brand.get("captions", {}).get("highlight", "")
    if re.fullmatch(r"#[\da-fA-F]{6}", highlight):
        variables += f";--brand-highlight:{highlight}"
    mobile = style.get("mobile", {})
    visual = {**style.get("layout", {}), **brand.get("layout", {}), **mobile.get("heading", {})}
    if mobile:
        variables += f";--style-ground:{mobile['background']};--vrs-artwork-bottom:420px"
        username = mobile.get("username", {})
        variables += f";--vrs-username-size:{username.get('size', 28)}px;--vrs-username-color:{username.get('color', '#181412')}"
        variables += f";--vrs-heading-align:{mobile.get('heading', {}).get('alignment', 'center')}"
        variables += f";--vrs-artwork-scale:{mobile.get('scale2d', 1)}"
    caption_size, caption_margin = caption_geometry(brand, style)
    variables += f";--vrs-caption-clearance:{caption_size + caption_margin + 36}px"
    for setting, token in {"headerTop":"header-top", "headerLeft":"header-left",
                           "headerSize":"header-size", "stageTop":"stage-top",
                           "headingSize":"heading-size", "headingSpacing":"heading-spacing",
                           "cardGap":"card-gap"}.items():
        value = visual.get(setting)
        if type(value) is int and 0 <= value <= 1080:
            variables += f";--vrs-{token}:{value}px"
    for setting, token in (("headingFont","heading-font"), ("headingColor","heading-color")):
        value = visual.get(setting, "")
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9 -]{0,45}|#[0-9a-fA-F]{6}", value):
            variables += f";--vrs-{token}:{value}"
    hidden = "".join(selector + "{{display:none!important}}" for flag, selector in (
        ("headerVisible","#profile-header"), ("progressVisible",".k8s-progress-rail"),
        ("outroVisible","#profile-outro")) if visual.get(flag) is False).replace("{{","{").replace("}}","}")
    overlay, motion = profile_markup(brand, brand_urls.get("profile"), timing["scenes"][-1], render_duration, mobile=bool(mobile))
    header_bottom = max(330, visual.get("headerTop", 283) + visual.get("headerSize", 64) + 29) if (
        brand_urls.get("profile") and visual.get("headerVisible") is not False) else 330
    disclosures = sponsorship.panels(target, sponsor, partners, timing, render_duration, header_bottom)
    write(target / "index.html", f"""<!doctype html>
<html lang="{lang}"><meta charset="utf-8"><title>{escape(plan["topic"])}</title>
<style>
{''.join(font_css)}
body {{margin:0}}
#production {{position:relative;overflow:hidden;width:{width}px;height:{height}px}}
#stage {{position:absolute;inset:0;width:1080px;height:1920px;
  transform:scale({width / 1080});transform-origin:top left;{variables}}}
.scene {{position:absolute;inset:0;overflow:hidden;background:var(--style-ground,#faf5f0);
  color:var(--brand-ink,#181412);font-family:var(--brand-font,sans-serif)}}
#profile-header,#profile-outro {{position:absolute;color:var(--brand-ink,#181412);
  font-family:var(--brand-font,sans-serif);z-index:10;pointer-events:none}}
#profile-header {{top:var(--vrs-header-top,283px);left:var(--vrs-header-left,89px);display:flex;align-items:center;gap:18px;
  background:#fffef9;padding:10px 22px 10px 10px;border:1px solid #e4ded4;border-radius:60px}}
#profile-header img {{width:var(--vrs-header-size,64px);height:var(--vrs-header-size,64px);border-radius:50%;object-fit:cover}}
#profile-header strong,#profile-header small {{display:block;font-size:29px;line-height:1.1}}
#profile-header small {{font-size:19px;color:#484036}}
#profile-header.mobile-profile {{padding:7px 16px 7px 7px;gap:10px}}
#profile-header.mobile-profile img {{width:44px;height:44px}}
#profile-header.mobile-profile strong {{font-size:var(--vrs-username-size);color:var(--vrs-username-color)}}
#profile-outro {{top:470px;left:158px;right:158px;display:flex;flex-direction:column;align-items:center;
  gap:9px;text-align:center;font-size:31px;line-height:1.2}}
#profile-outro .portrait {{padding:10px;border-radius:50%;background:conic-gradient(var(--brand-highlight,#ffd400),#fff1a6,var(--brand-highlight,#ffd400));
  box-shadow:0 25px 64px #5c441b38}}
#profile-outro img {{display:block;width:218px;height:218px;border-radius:50%;object-fit:cover}}
#profile-outro strong {{font-size:66px;margin-top:13px}}
#profile-outro b {{background:var(--brand-highlight,#ffd400);color:#181412;padding:13px 32px;border-radius:40px;margin-top:16px}}
{hidden}
{asset_render.CSS if assigned else ""}
{sponsorship.CSS if partners else ""}
{"#profile-header,#profile-outro{z-index:31}.vrs-media-badge{position:absolute;top:390px;left:89px;z-index:30;background:#fffef9;padding:10px 22px;border-radius:28px;font-size:26px}.vrs-media-badge .vrs-badge-tag{background:#ffd400;margin-right:12px;padding:2px 8px;border-radius:8px}" if assigned else ""}
</style>
<main id="production" data-composition-id="production" data-start="0"
 data-duration="{render_duration:.9f}" data-width="{width}" data-height="{height}">
<div id="stage" {"data-mobile-template" if mobile else ""}>{''.join(layers)}{''.join(asset_layers)}{overlay}{disclosures}</div></main>
{''.join(scripts)}
<script>
if (document.getElementById("stage").style.getPropertyValue("--vrs-artwork-bottom")) {{
  window.__hf = window.__hf || {{}};
  window.__hf.buildReady = window.__hf.buildReady || {{}};
  window.__hf.buildReady.templateTypography = document.fonts.ready.then(() => {{
    for (const title of document.querySelectorAll(".vrs-auto-heading")) {{
      let size = parseFloat(getComputedStyle(title).fontSize);
      while (size > 60 && title.offsetHeight > size * 2.18) {{
        size -= 2;
        title.style.fontSize = Math.max(60, size) + "px";
      }}
    }}
  }});
}}
// Measure the shared profile in composition pixels before laying out auto scenes.
const profile = document.getElementById("profile-header");
if (profile && document.querySelector(".vrs-auto-layout")) {{
  const canvas = document.getElementById("stage");
  const measureProfile = () => {{
    if (profile.offsetHeight) {{
      canvas.style.setProperty("--vrs-profile-bottom", (profile.offsetTop + profile.offsetHeight) + "px");
    }} else {{
      canvas.style.removeProperty("--vrs-profile-bottom");
    }}
  }};
  measureProfile();
  new ResizeObserver(measureProfile).observe(profile);
}}
if (window.reelMetaBadge) {{
  const cfg = window.reelMetaBadge;
  const mode = cfg.mode || "normal";
  document.querySelectorAll(".vrs-meta-badge, .k8s-chapter").forEach((el) => {{
    el.setAttribute("data-mode", mode);
    let dot = el.querySelector(".k8s-dot");
    let tag = el.querySelector(".vrs-badge-tag");
    let title = el.querySelector(".vrs-badge-title");
    if (!tag || !title) {{
      el.innerHTML = "";
      dot = document.createElement("span");
      dot.className = "k8s-dot";
      tag = document.createElement("span");
      tag.className = "vrs-badge-tag";
      title = document.createElement("span");
      title.className = "vrs-badge-title";
      el.append(dot, tag, title);
    }}
    if (dot) dot.style.display = (mode === "normal" || mode === "educational") ? "inline-block" : "none";
    if (mode === "ad") {{
      tag.textContent = "AD";
      tag.style.display = "inline-flex";
      title.textContent = cfg.title || "";
    }} else if (mode === "sponsor") {{
      tag.textContent = "SPONSOR";
      tag.style.display = "inline-flex";
      title.textContent = cfg.title || "";
    }} else {{
      tag.textContent = "";
      tag.style.display = "none";
      title.textContent = cfg.title || "";
    }}
  }});
}}
window.__timelines = window.__timelines || {{}};
window.__timelines.production = window.reelTimeline || gsap.timeline({{paused:true}});
window.__timelines.production.to({{}}, {{duration:{render_duration:.9f}}},0);
{motion}
{("(()=>{const tl=window.__timelines.production;" + "".join(asset_motion) + "})();") if any(asset_motion) else ""}
</script>
</html>
""")
    return target


def stamp(seconds, precision=3, separator=","):
    hours, remainder = divmod(round(seconds * 10**precision), 3600 * 10**precision)
    minutes, remainder = divmod(remainder, 60 * 10**precision)
    secs, fraction = divmod(remainder, 10**precision)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{fraction:0{precision}d}"


def _ttf_win_ratio(font_path):
    try:
        data = Path(font_path).read_bytes()
        num_tables = struct.unpack(">H", data[4:6])[0]
        tables = {}
        for i in range(num_tables):
            off = 12 + i * 16
            tag, _, offset, length = struct.unpack(">4sIII", data[off:off + 16])
            tables[tag.decode("latin1")] = data[offset:offset + length]
        upem = struct.unpack(">H", tables["head"][18:20])[0]
        os2 = tables.get("OS/2", b"")
        if len(os2) >= 78:
            w_asc, w_desc = struct.unpack(">HH", os2[74:78])
            if w_asc + w_desc > 0:
                return upem / (w_asc + w_desc)
        h_asc, h_desc = struct.unpack(">hh", tables["hhea"][4:8])
        return upem / max(1, h_asc - h_desc)
    except Exception:
        return 0.565


def _pill_ass_path(w, h, r=14):
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


def caption_settings(brand, style):
    mobile = style.get("mobile")
    if mobile:
        return {**brand.get("captions", {}), **mobile["captions"], "mode": "active-word"}
    return brand.get("captions", {})


def caption_geometry(brand, style):
    caption = caption_settings(brand, style)
    size = min(96 if style.get("mobile") else 84, max(36, int(caption.get("size", style.get("typography", {}).get("captionSizePx", 52)))))
    margin = min(900, max(260, int(caption.get("marginBottom", style.get("captions", {}).get("marginBottom", 690)))))
    return size, margin


def subtitles(folder, plan, words, dark_scenes=None, *, project=None):
    brand, style = profiles(plan, project=project)
    dark_scenes = set(dark_scenes or ())
    caption = caption_settings(brand, style)
    mobile = style.get("mobile")
    words_per_group = min(6, max(1, int(caption.get("wordsPerGroup",
                          style.get("captions", {}).get("wordsPerGroup", 4)))))
    font_size, margin_v = caption_geometry(brand, style)
    use_box = bool(caption.get("box", style.get("captions", {}).get("box", True)))
    use_highlighter = bool(caption.get("highlighter", False)) and not use_box
    adaptive = use_highlighter and (
        bool(caption["autoContrast"]) if "autoContrast" in caption
        else ("textColor" not in caption and "outline" not in caption)
    )
    border_style = 3 if use_box else 1
    outline_px = (int(caption.get("outline", 5)) if mobile else 10) if use_box else (max(3, int(caption.get("outline", 3))) if adaptive else int(caption.get("outline", 0 if use_highlighter else 5)))
    shadow_px = 0 if use_box and not mobile else int(caption.get("shadow", 0 if use_highlighter else 3))
    back_colour = "&H00191716" if use_box else "&H70181412"
    font = "Noto Sans Devanagari" if plan["language"] != "en" else "DejaVu Sans"
    lang = "hindi" if plan["language"] == "hi" else "english"
    font_cfg = brand.get("fonts", {}).get(lang, {})
    font = font_cfg.get("family", font)
    active_mode = caption.get("mode") == "active-word"
    hex_color = caption.get("highlight", "#faf5ee")
    rgb = hex_color[1:] if re.fullmatch(r"#[\da-fA-F]{6}", hex_color) else "faf5ee"
    bgr = rgb[4:6] + rgb[2:4] + rgb[:2]
    primary = "121418" if use_highlighter else ("FFFFFF" if active_mode else bgr)
    def ass_color(value, fallback):
        return value[5:7] + value[3:5] + value[1:3] if isinstance(value, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", value) else fallback
    primary = ass_color(caption.get("textColor"), primary)
    dark = ass_color(caption.get("darkTextColor"), "FCFAF8")
    if adaptive:
        primary = dark
    margin_l, margin_r = (90, 130) if use_highlighter else (100, 140)
    center_x = margin_l + (1080 - margin_l - margin_r) / 2
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{font_size},&H00{primary},&H00FFFFFF,&H00181412,{back_colour},-1,0,0,0,100,100,0,0,{border_style},{outline_px},{shadow_px},2,{margin_l},{margin_r},{margin_v},1
Style: DarkScene,{font},{font_size},&H00{dark},&H00FFFFFF,&H00181412,{back_colour},-1,0,0,0,100,100,0,0,1,{outline_px},{shadow_px},2,{margin_l},{margin_r},{margin_v},1
Style: Pill,Arial,10,&H00{bgr},&H00{bgr},&H00{bgr},&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    pil_font, win_ratio = None, 0.565
    if mobile or (use_highlighter and active_mode and words and any(
        scene["caption_style"] == "word" for scene in plan["scenes"]
    )):
        font_rel = font_cfg.get("bold") or font_cfg.get("regular")
        if not font_rel and use_highlighter:
            raise ValueError("The caption highlighter requires a configured font file.")
        font_abs = (BRAND_DIR / font_rel).resolve() if font_rel else None
        try:
            from PIL import ImageFont
        except ImportError as error:
            raise ValueError("Pillow is required for the configured caption highlighter.") from error
        try:
            pil_font = ImageFont.truetype(str(font_abs) if font_abs else "DejaVuSans-Bold.ttf", font_size)
        except OSError as error:
            raise ValueError(f"Cannot load the configured caption highlighter font: {font_abs.name if font_abs else font}") from error
        if font_abs:
            win_ratio = _ttf_win_ratio(font_abs)

    srt, ass = [], []
    sanitize = str.maketrans({"{": "(", "}": ")", "\\": "／"})

    def cue(from_sec, to_sec, text, layer=1, style_name="Default"):
        start = stamp(from_sec, 2, ".")
        end = stamp(to_sec, 2, ".")
        if start < end:
            ass.append(f"Dialogue: {layer},{start},{end},{style_name},,0,0,0,,{text}")

    def split_width(group):
        if not pil_font or len(group) <= 1:
            return [group]
        chunks, cur = [], []
        for w in group:
            cand = cur + [w]
            cand_w = pil_font.getlength(" ".join(x["text"] for x in cand)) * win_ratio
            if cur and cand_w > (730 if mobile else 760):
                chunks.append(cur)
                cur = [w]
            else:
                cur = cand
        if cur:
            chunks.append(cur)
        return chunks

    for scene in plan["scenes"]:
        selected = [word for word in words if word["scene"] == scene["id"]]
        gap = style.get("captions", {}).get("maxPauseGapSec", 0.22)
        style_name = "DarkScene" if (scene["id"] in dark_scenes and use_highlighter) else "Default"
        groups = []
        for g in group_captions(selected, max_words=words_per_group, max_gap=gap):
            groups.extend(split_width(g))
        for group in groups:
            text = " ".join(w["text"] for w in group)
            srt.append(f"{len(srt) + 1}\n{stamp(group[0]['start'])} --> {stamp(group[-1]['end'])}\n{text}\n")
            if active_mode and scene["caption_style"] == "word":
                labels = [word["text"].translate(sanitize) for word in group]
                full_line = " ".join(labels)
                full_w = (pil_font.getlength(full_line) * win_ratio) if pil_font else 0
                pill_h = round(font_size * 0.78)
                pill_y = round(1920 - margin_v - font_size * 0.59 - pill_h / 2)
                for index, word in enumerate(group):
                    if use_highlighter and pil_font:
                        prefix = " ".join(labels[:index]) + (" " if index > 0 else "")
                        x_left = center_x - full_w / 2
                        px = x_left + pil_font.getlength(prefix) * win_ratio - 7
                        pw = pil_font.getlength(labels[index]) * win_ratio + 14
                        cue(word["start"], word["end"],
                            f"{{\\pos({round(px)},{pill_y})\\p1}}{_pill_ass_path(pw, pill_h, 14)}{{\\p0}}",
                            layer=0, style_name="Pill")
                        if adaptive or style_name == "DarkScene":
                            colored = labels.copy()
                            active = r"{\1c&H121418&\bord0\shad0}" if adaptive else r"{\1c&H121418&}"
                            reset = r"{\r}" if adaptive else r"{\1c}"
                            colored[index] = active + labels[index] + reset
                            cue(word["start"], word["end"], " ".join(colored), layer=1, style_name=style_name)
                        else:
                            cue(word["start"], word["end"], full_line, layer=1, style_name=style_name)
                        if index + 1 < len(group):
                            cue(word["end"], group[index + 1]["start"], full_line, layer=1, style_name=style_name)
                        continue
                    colored = labels.copy()
                    if use_box:
                        colored[index] = r"{\1c&H" + bgr + r"&}" + labels[index] + r"{\1c}"
                    else:
                        colored[index] = r"{\1c&H" + bgr + r"&}" + labels[index] + r"{\1c}"
                    cue(word["start"], word["end"], " ".join(colored), layer=1, style_name=style_name)
                    if index + 1 < len(group):
                        cue(word["end"], group[index + 1]["start"], full_line, layer=1, style_name=style_name)
                continue
            cards = [[word] for word in group] if scene["caption_style"] == "word" else [group]
            for card in cards:
                text = " ".join(w["text"] for w in card).translate(sanitize)
                cue(card[0]["start"], card[-1]["end"], text, layer=1, style_name=style_name)
    write(folder / "captions.srt", "\n".join(srt))
    write(folder / "captions.ass", header + "\n".join(ass) + "\n")


def ffmpeg(*args, cwd=None):
    run("ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-y", *args, cwd=cwd)


def mix_stem(inputs, target, duration):
    args, filters = [], []
    for index, (path, at, gain) in enumerate(inputs):
        args.extend(["-i", path])
        filters.append(f"[{index}:a]aresample=48000,aformat=channel_layouts=stereo,"
                       f"volume={gain},adelay={round(at * 48000)}S:all=1[a{index}]")
    joined = "".join(f"[a{i}]" for i in range(len(inputs)))
    filters.append(f"{joined}amix=inputs={len(inputs)}:normalize=0,apad,alimiter=limit=0.96:latency=1[out]")
    ffmpeg(*args, "-filter_complex", ";".join(filters), "-map", "[out]", "-t", str(duration),
           "-c:a", "pcm_s16le", target)


def mix_production_audio(voice, tracks, target, duration):
    audio = [(voice, 0, 1)]
    for kind, gain in (("music", 0.16), ("sfx", 0.32)):
        inputs = [(path, cue["at"], 1) for cue, path in tracks if cue["kind"] == kind]
        if inputs:
            stem = target.with_name(f"{kind}.wav")
            mix_stem(inputs, stem, duration)
            audio.append((stem, 0, gain))
    mix_stem(audio, target, duration)


def render(project, plan, timing, source, preset, preview=False, *, imported_master=None, caption_report=None):
    build = stage(project, plan, timing, source, preset)
    if preview:
        run(*CLI, "preview", build, "--background", "--no-open", cwd=ENGINE, env=browser_env())
        return
    settings = EXPORT_PRESETS[preset]
    fps = settings["fps"]
    duration = math.ceil(timing["duration"] * fps) / fps
    if imported_master is not None and not imported_master.is_file():
        raise ValueError("The validated cloud master is missing.")
    clean = imported_master if imported_master is not None else build / "visual-only.mp4"
    if not clean.is_file():
        with tempfile.TemporaryDirectory(dir=build.parent) as temp:
            raw = Path(temp) / "capture.mp4"
            run(*CLI, "render", build, "--output", raw, "--fps", str(fps),
                "--crf", str(settings["crf"]), "--quality", settings["quality"], "--workers", "1",
                "--strict", "--no-best-effort", "--refuse-outside-files", cwd=ENGINE, env=browser_env())
            draft = Path(temp) / clean.name
            ffmpeg("-i", raw, "-map", "0:v:0", "-c:v", "copy", "-an", draft)
            draft.replace(clean)
    cues = [cue for scene in timing["scenes"] for cue in scene["audio_cues"]]
    tracks = [(cue, project / "tracks" / f'{cue["id"]}.wav') for cue in cues]
    tracks = [(cue, path) for cue, path in tracks if path.is_file()]
    voice = project / "voice.wav"
    master_key = [digest(imported_master)] if imported_master is not None else []
    if caption_report is not None:
        master_key.append(digest(caption_report))
    key = digest(Path(__file__), build.name, timing, plan, [(cue, digest(path)) for cue, path in tracks],
                 *master_key)
    parent = EXPORTS / plan["id"]
    parent.mkdir(parents=True, exist_ok=True)
    output = parent / f"{preset}-{key}"
    if output.is_dir():
        print(f"Reusing verified export: {output}")
        return
    with tempfile.TemporaryDirectory(dir=parent) as temp:
        work = Path(temp) / "bundle"
        work.mkdir()
        shutil.copyfile(clean, work / clean.name)
        shutil.copyfile(voice, work / "voiceover.wav")
        for name in ("voiceover.txt", "instagram.json", "alignment-report.json"):
            shutil.copyfile(project / name, work / name)
        if caption_report is not None:
            save(work / "alignment-report.json", caption_report)
        save(work / "word-timings.json", timing)
        assignments = asset_library.saved(project)
        dark_scenes = {sid for sid, p in source_files(source, plan, assignments)[0].items()
                       if "k8s-scene--dark" in p.read_text(encoding="utf-8")}
        subtitles(work, plan, timing["words"], dark_scenes=dark_scenes, project=project)
        caption_filter = "ass=captions.ass"
        if profiles(plan, project=project)[0].get("fonts"):
            font_dir = os.path.relpath(build / "assets" / "brand-profile", work).replace(os.sep, "/")
            caption_filter += f":fontsdir={font_dir}"
        ffmpeg("-i", clean, "-vf", caption_filter, "-an", "-c:v", "libx264",
               "-crf", str(settings["crf"]), "-preset", "fast", "-pix_fmt", "yuv420p",
               "captioned-silent.mp4", cwd=work)
        mix_production_audio(voice, tracks, work / "mix.wav", duration)
        ffmpeg("-i", "captioned-silent.mp4", "-i", "mix.wav", "-map", "0:v:0", "-map", "1:a:0",
               "-c:v", "copy", "-c:a", "aac", "-movflags", "+faststart", "final.mp4", cwd=work)
        (work / "mix.wav").unlink()
        for name, has_sound in (("final.mp4", True), (clean.name, False), ("captioned-silent.mp4", False)):
            verify(work / name, duration, fps, preset, has_sound)
        if preset == "draft":
            save(work / "preview-revision.json", {"stage": build.name})
        if not output.exists():
            work.rename(output)
    print(f"Verified export: {output}")


def verify(path, duration, fps, preset, has_sound, *, decode=True):
    import json

    info = json.loads(run("ffprobe", "-v", "error", "-show_streams", "-show_format",
                          "-of", "json", path, capture_output=True, text=True).stdout)
    streams = {stream["codec_type"]: stream for stream in info["streams"]}
    picture = streams["video"]
    expected_size = (540, 960) if preset == "draft" else (1080, 1920)
    if ((picture["width"], picture["height"]) != expected_size
            or picture["avg_frame_rate"] != f"{fps}/1" or picture["codec_name"] != "h264"
            or int(picture["nb_frames"]) != round(duration * fps)
            or (streams.get("audio", {}).get("codec_name") != ("aac" if has_sound else None))
            or abs(float(info["format"]["duration"]) - duration) > 1 / fps):
        raise ValueError(f"Output failed video/audio checks: {path}")
    if decode:
        run("ffmpeg", "-v", "error", "-xerror", "-i", path, "-f", "null", "-")
