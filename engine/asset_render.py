"""Compile vetted asset assignments into native HyperFrames clips."""
from html import escape
import json

from asset_library import ffmpeg, input_options, original as uploaded_file

CSS = """
.vrs-original {position:absolute;inset:0;z-index:2}
.vrs-asset {position:absolute;left:var(--ax);top:var(--ay);width:83%;height:51%;
 transform:translate(-50%,-50%) scale(var(--az));transform-origin:center;pointer-events:none}
.vrs-asset[data-layout=full] {width:100%;height:100%}
.vrs-asset[data-layout=laptop] {width:83%;height:33%}
.vrs-asset[data-layout=mobile] {width:43%;height:51%}
.vrs-asset-frame {position:absolute;inset:0;box-sizing:border-box;overflow:hidden}
.vrs-asset[data-layout=laptop] .vrs-asset-frame,.vrs-asset[data-layout=mobile] .vrs-asset-frame {
 border:14px solid #182334;border-radius:30px;background:#182334;box-shadow:0 22px 55px #17213350}
.vrs-asset[data-layout=laptop]::after {content:"";position:absolute;bottom:-16px;left:-5%;width:110%;
 height:24px;background:#344259;border-radius:0 0 55% 55%}
.vrs-asset-media {position:absolute;inset:0;width:100%;height:100%;object-fit:var(--af);object-position:var(--ac)}
.vrs-asset-highlight {position:absolute;inset:12%;border:5px solid var(--brand-highlight,#ffd400);border-radius:14px}
.vrs-asset-callout {position:absolute;left:5%;bottom:7%;max-width:85%;padding:10px 22px;border-radius:12px;
 background:#182334;color:white;font-size:29px;line-height:1.25;overflow-wrap:anywhere}
"""


def prepare(target, slot, asset, width, fps):
    original = uploaded_file(asset)
    options = (*input_options(original.suffix[1:]), "-threads", "2")
    if asset["kind"] == "image":
        ffmpeg(*options, "-i", original, "-map", "0:v:0", "-frames:v", "1",
               "-vf", f"scale=w='min({width*2},iw)':h='min({width*2},ih)':force_original_aspect_ratio=decrease",
               "-threads", "1", target)
    else:
        duration = slot["end"] - slot["start"]
        # Give HyperFrames a zero-based, constant-FPS clip. Freeze the final image if
        # the chosen source interval ends before the assigned screen interval.
        ffmpeg(*options, "-ss", slot["trimIn"], "-i", original, "-map", "0:v:0", "-an",
               "-vf", f'trim=duration={slot["trimOut"]-slot["trimIn"]},setpts=(PTS-STARTPTS)/{slot["speed"]},'
                      f"fps={fps}:round=up,scale=w='min({width*2},iw)':h='min({width*2},ih)':force_original_aspect_ratio=decrease:"
                      f"force_divisible_by=2,setsar=1,tpad=stop_mode=clone:stop_duration={duration}",
               "-t", f"{max(duration, 1/fps):.9f}", "-threads", "2", "-c:v", "libx264", "-preset", "veryfast",
               "-pix_fmt", "yuv420p", "-map_metadata", "-1", "-movflags", "+faststart", target)


def markup(target, actual, selection, assets, width, fps, offset):
    folder = target / "assets" / "assigned-media"
    folder.mkdir(parents=True, exist_ok=True)
    layers = []
    motion = []
    for index, slot in enumerate(selection["layers"]):
        asset = assets[slot["asset"]]
        ident = f'vrs-assigned-{actual["id"]}-{index}'
        is_video = asset["kind"] == "video"
        path = folder / (ident + (".mp4" if is_video else ".png"))
        prepare(path, slot, asset, width, fps)
        start = actual["start"] + slot["start"]
        duration = slot["end"] - slot["start"]
        relative = path.relative_to(target).as_posix()
        timing = f'data-start="{start:.9f}" data-duration="{duration:.9f}" data-track-index="{offset+index}"'
        # Time video on the element itself. Its frame remains deterministic under native seek.
        item = (f'<video id="{ident}-video" class="clip vrs-asset-media" {timing} muted playsinline preload="auto" src="{relative}"></video>'
                if is_video else f'<img class="vrs-asset-media" src="{relative}" alt="">')
        z = 1 if slot["placement"] == "background" else 3 + index
        decoration = ('<span class="vrs-asset-highlight"></span>' if slot["highlight"] else "")
        if slot["callout"]:
            decoration += f'<span class="vrs-asset-callout">{escape(slot["callout"])}</span>'
        wrapper = 'class="vrs-asset"' if is_video else f'class="clip vrs-asset" {timing}'
        layers.append(
            f'<div {wrapper} id="{ident}" data-layout="{slot["layout"]}" '
            f'style="z-index:{z};--ax:{slot["x"]}%;--ay:{slot["y"]}%;--az:{slot["scale"]};'
            f'--af:{slot["fit"]};--ac:{slot["cropX"]}% {slot["cropY"]}%'
            f'{";visibility:hidden" if is_video else ""}">'
            f'<div class="vrs-asset-frame" style="opacity:{slot["opacity"]}">{item}{decoration}</div></div>')
        if is_video:
            motion.append(f'tl.set("#{ident}",{{visibility:"visible"}},{start});')
            motion.append(f'tl.set("#{ident}",{{visibility:"hidden"}},{start+duration});')
        if slot["motion"] != "static":
            initial = {"opacity": 0}
            if slot["motion"] == "slide":
                initial["y"] = 36
            if slot["motion"] == "depth":
                initial.update(scale=.88, rotationY=-13, transformPerspective=850)
            final = {key: (1 if key == "scale" else 0) for key in initial}
            final["opacity"] = slot["opacity"]
            selector = json.dumps(f"#{ident} .vrs-asset-frame")
            edge = min(.36, duration / 3)
            motion.append(f'tl.fromTo({selector},{json.dumps(initial)},'
                          f'{{...{json.dumps(final)},duration:{edge},ease:"power2.out",immediateRender:false}},{start});')
            motion.append(f'tl.to({selector},{{opacity:0,duration:{edge},ease:"power1.in"}},{start+duration-edge});')
    return "".join(layers), "".join(motion)
