"""Local immutable uploads and portable, validated scene assignments."""
from hashlib import file_digest
import json
import math
from pathlib import Path, PureWindowsPath
import re
import shutil
import subprocess
import tempfile
from uuid import uuid4
from xml.etree import ElementTree as ET

from production_plan import BRAND_DIR, PROJECTS, ROOT, load, save

LIBRARY = ROOT / "assets" / "library"
ID = r"[a-f0-9]{32}"
SIDE = "scene-assets.json"
MODES = ("ai", "image", "video", "hybrid")
MAX_LAYERS = 12
FORMATS = {
    "glb": ("model", None, None),
    "png": ("image", "png_pipe", "png"), "jpg": ("image", "jpeg_pipe", "mjpeg"),
    "webp": ("image", "webp_pipe", "webp"), "svg": ("image", "svg_pipe", "svg"),
    "mp4": ("video", "mp4", None), "mov": ("video", "mov", None),
    "webm": ("video", "webm", None),
    "wav": ("audio", "wav", None), "mp3": ("audio", "mp3", "mp3"),
    "m4a": ("audio", "m4a", "aac"), "flac": ("audio", "flac", "flac"),
    "ogg": ("audio", "ogg", None),
}
ACCEPT = ",".join("." + ext for ext in (*FORMATS, "jpeg"))
SAFE_ID = r"[a-z0-9][a-z0-9-]{0,63}"


def hash_file(path):
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def limit(config):
    size = config.get("assetMiB") or "128"
    if not isinstance(size, str) or not re.fullmatch(r"[0-9]{1,4}", size) or not 1 <= int(size) <= 1024:
        raise ValueError("Asset upload limit must be between 1 and 1024 MiB.")
    return int(size) * 1024**2


def label(value, maximum=160, *, multiline=False):
    if (not isinstance(value, str) or len(value) > maximum
            or any((ord(c) < 32 or ord(c) == 127) and not (multiline and c in "\n\r\t") for c in value)):
        raise ValueError("Invalid asset name or description.")
    return value.strip()


def scope_key(scope, owner):
    if scope == "global" and owner == "":
        return
    if not isinstance(owner, str) or not re.fullmatch(SAFE_ID, owner):
        raise ValueError("Invalid asset library owner.")
    if (scope == "project" and (PROJECTS / owner / "brief.json").is_file()
            or scope == "brand" and (BRAND_DIR / (owner + ".json")).is_file()):
        return
    raise ValueError("Choose an existing brand or project library.")


def catalog():
    path = LIBRARY / "catalog.json"
    if not path.is_file():
        return {}
    data = load(path)
    if data["version"] != 1:
        raise ValueError("Unsupported asset catalog version.")
    return data["assets"]


def record(asset_id, entries):
    if not isinstance(asset_id, str) or not re.fullmatch(ID, asset_id) or asset_id not in entries:
        raise ValueError(f"Missing or invalid asset ID: {str(asset_id)[:65]}")
    return entries[asset_id]


def blob(asset):
    sha = asset["sha256"]
    if not re.fullmatch(r"[a-f0-9]{64}", sha) or asset["format"] not in FORMATS:
        raise ValueError("Invalid asset file record.")
    return LIBRARY / "files" / sha


def original(asset):
    extension = asset.get("originalFormat", asset["format"])
    if extension not in FORMATS:
        raise ValueError("Invalid original media format.")
    return blob(asset) / f"original.{extension}"


def public(asset):
    if asset["kind"] == "model":
        return {**asset, "url": None, "thumbnail": None}
    return {**asset, "url": f'/asset-media/{asset["id"]}/preview',
            "thumbnail": f'/asset-media/{asset["id"]}/thumb' if asset["kind"] != "audio" else None}


def visible(asset, project, plan):
    return asset["scope"] == "global" or (asset["scope"], asset["owner"]) in {
        ("project", project.name), ("brand", plan["brand"])}


def media(asset_id, variant):
    asset = record(asset_id, catalog())
    if asset["kind"] == "model":
        raise ValueError("3D models are previewed in production scenes.")
    suffix = {"image": "png", "video": "mp4", "audio": "mp3"}[asset["kind"]]
    if variant == "preview":
        path = blob(asset) / f"preview.{suffix}"
    elif variant == "thumb" and asset["kind"] != "audio":
        path = blob(asset) / "thumb.png"
    else:
        raise ValueError("Invalid asset preview.")
    if path.is_symlink() or not path.resolve().is_relative_to(LIBRARY.resolve()):
        raise ValueError("Invalid asset preview path.")
    return path


def ffmpeg(*args, timeout=90):
    result = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-xerror", *map(str, args)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=timeout)
    if result.returncode:
        raise ValueError("Media decoding or conversion failed. Check the file and its format.")


def svg_safe(source):
    if source.stat().st_size > 1024**2:
        raise ValueError("SVG exceeds the 1 MiB XML limit.")
    raw = source.read_bytes().decode("utf-8")
    scan = re.sub(r"^\ufeff?\s*<\?xml\s+version=['\"]1\.0['\"](?:\s+encoding=['\"][Uu][Tt][Ff]-8['\"])?\s*\?>",
                  "", raw, count=1)
    if "<!" in raw or "<?" in scan:
        raise ValueError("SVG declarations and processing instructions are unsupported.")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as error:
        raise ValueError("Invalid SVG XML.") from error
    ns = "{http://www.w3.org/2000/svg}"
    elements = {"svg", "g", "defs", "title", "desc", "path", "rect", "circle", "ellipse", "line",
                "polyline", "polygon", "linearGradient", "radialGradient", "stop", "clipPath", "text", "tspan"}
    attrs = set(("id width height viewBox preserveAspectRatio fill fill-rule fill-opacity stroke stroke-width "
                 "stroke-opacity stroke-linecap stroke-linejoin stroke-dasharray opacity transform d x y x1 x2 y1 y2 "
                 "cx cy r rx ry points gradientUnits gradientTransform offset stop-color stop-opacity clip-path "
                 "font-size font-family font-weight text-anchor dx dy").split())
    if root.tag != ns + "svg":
        raise ValueError("Use a standalone SVG with the standard namespace.")
    for i, node in enumerate(root.iter()):
        if i >= 5000 or not node.tag.startswith(ns) or node.tag[len(ns):] not in elements:
            raise ValueError("SVG contains unsupported or active elements.")
        for name, value in node.attrib.items():
            if (name not in attrs or re.search(r"(?i)url\s*\(|[:\\]", value)
                    and not (name in {"fill", "stroke", "clip-path"} and re.fullmatch(r"url\(#[\w.-]+\)", value))):
                raise ValueError("SVG contains unsupported attributes or external references.")
    for name in ("width", "height"):
        value = root.get(name)
        if value and not re.fullmatch(r"[0-9]{1,4}(?:\.[0-9]+)?(?:px|%)?", value):
            raise ValueError("SVG dimensions are unsupported.")


def input_options(ext):
    container = FORMATS[ext][1]
    demuxer = {"mp4": "mov", "m4a": "mov", "webm": "matroska"}.get(container, container)
    return ("-protocol_whitelist", "file", "-f", demuxer)


def inspect_glb(source):
    import struct

    size = source.stat().st_size
    if not 20 <= size <= 24 * 1024**2:
        raise ValueError("Use a self-contained GLB no larger than 24 MiB.")
    try:
        with source.open("rb") as stream:
            magic, version, total, length, kind = struct.unpack("<4sIII4s", stream.read(20))
            if (magic, version, total, kind) != (b"glTF", 2, size, b"JSON") or not 0 < length <= min(size - 28, 2**20):
                raise ValueError("Invalid GLB 2.0 header.")
            gltf = json.loads(stream.read(length))
            binary, kind = struct.unpack("<I4s", stream.read(8))
            if kind != b"BIN\x00" or binary + length + 28 != size:
                raise ValueError("GLB must contain one embedded binary chunk.")
        buffers = gltf["buffers"]
        if len(buffers) != 1 or "uri" in buffers[0] or not 0 < buffers[0]["byteLength"] <= binary:
            raise ValueError("GLB buffers must be embedded.")
        for image in gltf.get("images", []):
            if "uri" in image or "bufferView" not in image or image.get("mimeType") not in {"image/png", "image/jpeg"}:
                raise ValueError("GLB textures must be embedded PNG or JPEG images.")
        # Restrict allocation and draw calls before the browser parses the model.
        nodes, meshes = gltf["nodes"], gltf["meshes"]
        accessors = gltf["accessors"]
        if (not 0 < len(nodes) <= 128 or not 0 < len(meshes) <= 64 or len(accessors) > 512
                or len(gltf.get("images", [])) > 8
                or gltf.get("extensionsRequired")
                or any("extensions" in item for item in [*nodes, *meshes, *gltf.get("textures", [])])
                or sum(a["count"] for a in accessors) > 900_000
                or any(type(a["count"]) is not int or a["count"] <= 0 for a in accessors)
                or sum(len(m["primitives"]) for m in meshes) > 96):
            raise ValueError("GLB exceeds the supported complexity or needs an unsupported decoder.")
        views = gltf["bufferViews"]
        for view in views:
            offset, count = view.get("byteOffset", 0), view["byteLength"]
            if "extensions" in view or view["buffer"] != 0 or not 0 <= offset < offset + count <= buffers[0]["byteLength"]:
                raise ValueError("Invalid GLB buffer view.")
        for i, node in enumerate(nodes):
            children = node.get("children", [])
            if any(type(child) is not int or not 0 <= child < len(nodes) for child in children):
                raise ValueError(f"Invalid children for GLB node {i}.")
        parents = [child for node in nodes for child in node.get("children", [])]
        if len(set(parents)) != len(parents):
            raise ValueError("GLB nodes may have only one parent.")
        visiting, visited = set(), set()

        def visit(i):
            if i in visiting:
                raise ValueError("GLB node hierarchy contains a cycle.")
            if i in visited:
                return
            visiting.add(i)
            for child in nodes[i].get("children", []):
                visit(child)
            visiting.remove(i)
            visited.add(i)

        for i in range(len(nodes)):
            visit(i)
        with source.open("rb") as stream:
            for image in gltf.get("images", []):
                index = image["bufferView"]
                if type(index) is not int or not 0 <= index < len(views):
                    raise ValueError("Invalid GLB image buffer view.")
                view = views[index]
                stream.seek(28 + length + view.get("byteOffset", 0))
                image_data = stream.read(view["byteLength"])
                ext = "png" if image["mimeType"] == "image/png" else "jpg"
                result = subprocess.run(
                    ["ffprobe", "-v", "error", "-protocol_whitelist", "pipe", "-f", FORMATS[ext][1],
                     "-show_entries", "stream=width,height", "-of", "json", "-i", "pipe:0"],
                    input=image_data, capture_output=True, check=False, timeout=10)
                images = json.loads(result.stdout or "{}").get("streams", [])
                if result.returncode or len(images) != 1 or not (
                        0 < images[0]["width"] <= 2048 and 0 < images[0]["height"] <= 2048):
                    raise ValueError("GLB textures must decode and fit within 2048 × 2048 pixels.")
    except (KeyError, TypeError, AttributeError, struct.error, RecursionError, subprocess.TimeoutExpired) as error:
        raise ValueError("Malformed GLB model.") from error
    return {"kind": "model"}


def model_source(project, plan, asset_id):
    asset = record(asset_id, catalog())
    if asset["kind"] != "model" or not visible(asset, project, plan):
        raise ValueError(f"Model asset {asset_id} is unavailable to this project.")
    path = original(asset)
    if path.is_symlink() or not path.resolve().is_relative_to(LIBRARY.resolve()) or not path.is_file():
        raise ValueError(f"Model asset {asset_id} is missing or invalid.")
    if hash_file(path) != asset["sha256"]:
        raise ValueError(f"Model asset {asset_id} failed its integrity check.")
    return path


def inspect(source, ext):
    if ext == "glb":
        return inspect_glb(source)
    kind, container, codec = FORMATS[ext]
    if ext == "svg":
        svg_safe(source)
    result = subprocess.run(["ffprobe", "-v", "error", *input_options(ext), "-show_format",
                             "-show_streams", "-of", "json", source], capture_output=True, check=False, timeout=25)
    if result.returncode:
        raise ValueError("Unrecognized or malformed media file.")
    raw = json.loads(result.stdout)
    format_info = raw.get("format", {})
    stream = next((s for s in raw.get("streams", []) if s["codec_type"] == ("audio" if kind == "audio" else "video")
                   and not s.get("disposition", {}).get("attached_pic")), None)
    if container not in format_info.get("format_name", "").split(",") or stream is None or codec and stream["codec_name"] != codec:
        raise ValueError("The uploaded file does not match its declared media format.")
    meta = {"kind": kind}
    if kind != "audio":
        w, h = stream["width"], stream["height"]
        if not (0 < w <= 8192 and 0 < h <= 8192 and w*h <= 36_000_000):
            raise ValueError("Asset has unsupported image dimensions.")
        rotation = next((s.get("rotation", 0) for s in stream.get("side_data_list", [])
                         if "rotation" in s), 0)
        meta.update(width=h if rotation % 180 else w, height=w if rotation % 180 else h)
    if kind != "image":
        duration = float(format_info.get("duration", 0))
        if not math.isfinite(duration) or not 0 < duration <= 3600:
            raise ValueError("Media duration must be known and at most one hour.")
        meta["duration"] = round(duration, 6)
    return meta


def previews(source, folder, meta):
    kind = meta["kind"]
    inputs = (*input_options(source.suffix[1:]), "-threads", "2", "-i", source)
    if kind == "audio":
        ffmpeg(*inputs, "-map", "0:a:0", "-vn", "-t", "60", "-c:a", "libmp3lame", folder / "preview.mp3")
        return
    ffmpeg(*inputs, "-map", "0:v:0", "-frames:v", "1", "-vf",
           "scale=320:320:force_original_aspect_ratio=decrease", "-threads", "1", folder / "thumb.png")
    if kind == "image":
        ffmpeg(*inputs, "-map", "0:v:0", "-frames:v", "1", "-vf",
               "scale=w='min(1920,iw)':h='min(1920,ih)':force_original_aspect_ratio=decrease",
               "-threads", "1", folder / "preview.png")
    else:
        ffmpeg(*inputs, "-map", "0:v:0", "-an", "-t", "30", "-vf",
               "fps=24,scale=640:640:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1",
               "-threads", "2", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
               "-map_metadata", "-1", "-movflags", "+faststart", folder / "preview.mp4")


def upload(stream, length, maximum, filename, scope, owner):
    scope_key(scope, owner)
    name = label(filename)
    if not name or name in {".", ".."} or PureWindowsPath(name).name != name or "/" in name or ":" in name:
        raise ValueError("Unsafe asset filename.")
    ext = Path(name).suffix.lower().lstrip(".")
    ext = "jpg" if ext == "jpeg" else ext
    if ext not in FORMATS or not 0 < length <= maximum:
        raise ValueError("Unsupported asset type or upload exceeds the configured limit.")
    LIBRARY.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=LIBRARY) as folder:
        temp = Path(folder)
        source = temp / f"original.{ext}"
        with source.open("xb") as output:
            remaining = length
            while remaining:
                data = stream.read(min(remaining, 1024**2))
                if not data or len(data) > remaining:
                    raise ValueError("Asset upload was incomplete.")
                output.write(data)
                remaining -= len(data)
        sha = hash_file(source)
        entries = catalog()
        meta = inspect(source, ext)
        same = next((a for a in entries.values() if a["sha256"] == sha
                     and (a["scope"], a["owner"]) == (scope, owner)), None)
        if same:
            return {"asset": public(same), "duplicate": True}
        asset = {"id": uuid4().hex, "scope": scope, "owner": owner, "name": name, "filename": name,
                 "description": "", "format": ext, "bytes": length, "sha256": sha, **meta}
        target = blob(asset)
        if not target.exists():
            if meta["kind"] != "model":
                previews(source, temp, meta)
            target.parent.mkdir(exist_ok=True)
            temp.rename(target)
        else:
            existing = next((a for a in entries.values() if a["sha256"] == sha), None)
            if existing:
                asset["originalFormat"] = existing.get("originalFormat", existing["format"])
            elif not (target / source.name).is_file():
                source.rename(target / source.name)
        entries[asset["id"]] = asset
        save(LIBRARY / "catalog.json", {"version": 1, "assets": entries})
        return {"asset": public(asset), "duplicate": False}


def edit(asset_id, data):
    import sponsorship

    entries = catalog()
    asset = record(asset_id, entries)
    if data.get("action") == "delete" or data.get("scope", asset["scope"]) != "global":
        if any(asset_id in sponsorship.references(profile) for profile in sponsorship.profiles().values()):
            raise ValueError("Asset is assigned to a sponsor profile. Remove that reference first.")
    if data.get("action") == "delete":
        if data.get("confirmed") is not True:
            raise ValueError("Confirm asset deletion.")
        for path in PROJECTS.glob(f"*/{SIDE}"):
            if any(layer["asset"] == asset_id for scene in load(path)["scenes"].values() for layer in scene["layers"]):
                raise ValueError(f"Asset is assigned in project {path.parent.name}. Remove the assignment first.")
        del entries[asset_id]
    elif data.get("action") == "edit":
        asset["name"] = label(data.get("name")) or asset["filename"]
        asset["description"] = label(data.get("description", ""), 2000, multiline=True)
        scope, owner = data.get("scope", asset["scope"]), data.get("owner", asset["owner"])
        scope_key(scope, owner)
        if (scope, owner) != (asset["scope"], asset["owner"]):
            for path in PROJECTS.glob(f"*/{SIDE}"):
                if any(layer["asset"] == asset_id for scene in load(path)["scenes"].values() for layer in scene["layers"]):
                    updated = {**asset, "scope": scope, "owner": owner}
                    if not visible(updated, path.parent, load(path.parent / "brief.json")):
                        raise ValueError(f"New scope would hide the asset from project {path.parent.name}.")
            asset.update(scope=scope, owner=owner)
    else:
        raise ValueError("Unknown asset action.")
    save(LIBRARY / "catalog.json", {"version": 1, "assets": entries})
    if data["action"] == "delete" and not any(a["sha256"] == asset["sha256"] for a in entries.values()):
        shutil.rmtree(blob(asset))
    return {"saved": True}


def number(obj, key, default, low, high):
    value = obj.get(key, default)
    if type(value) not in (int, float) or not low <= value <= high:
        raise ValueError(f"{key} must be between {low:g} and {high:g}.")
    return value


def choice(obj, key, values):
    value = obj.get(key, values[0])
    if value not in values:
        raise ValueError(f"Unsupported asset {key}.")
    return value


def assignment(value, assets, duration):
    if not isinstance(value, dict):
        raise ValueError("Use a scene assignment JSON object.")
    mode = choice(value, "mode", MODES)
    layers = value.get("layers", [])
    if not isinstance(layers, list) or len(layers) > MAX_LAYERS or mode in {"image", "video"} and not layers:
        raise ValueError("Choose at least one asset for an image/video scene; maximum 12 layers.")
    if mode == "ai" and layers:
        raise ValueError("Use Hybrid to show assets with the reviewed visual.")
    cleaned = []
    for layer in layers:
        if not isinstance(layer, dict):
            raise ValueError("Each media layer must be a JSON object.")
        asset = record(layer.get("asset"), assets)
        kind = asset["kind"]
        if kind in {"audio", "model"} or mode in {"image", "video"} and kind != mode:
            raise ValueError("The selected asset type does not match the scene mode.")
        start = number(layer, "start", 0, 0, duration)
        end = number(layer, "end", duration, start, duration)
        if end - start < 0.001:
            raise ValueError("Asset timing must span a positive interval inside the scene.")
        row = dict(asset=asset["id"], start=start, end=end,
                   fit=choice(layer, "fit", ("contain", "cover")), placement=choice(layer, "placement", ("foreground", "background")),
                   layout=choice(layer, "layout", ("safe", "full", "laptop", "mobile")),
                   motion=choice(layer, "motion", ("static", "fade", "slide", "depth")),
                   x=number(layer, "x", 50, 0, 100), y=number(layer, "y", 50, 0, 100),
                   cropX=number(layer, "cropX", 50, 0, 100), cropY=number(layer, "cropY", 50, 0, 100),
                   scale=number(layer, "scale", 1, .1, 3), opacity=number(layer, "opacity", 1, 0, 1),
                   callout=label(layer.get("callout", ""), 100),
                   highlight=layer.get("highlight") is True)
        if kind == "video":
            trim_in = number(layer, "trimIn", 0, 0, asset["duration"])
            trim_out = number(layer, "trimOut", asset["duration"], trim_in, asset["duration"])
            speed = number(layer, "speed", 1, .25, 4)
            if trim_out - trim_in < .04:
                raise ValueError("Video trim must be at least 0.04 seconds.")
            row.update(trimIn=trim_in, trimOut=trim_out, speed=speed)
        cleaned.append(row)
    return {"mode": mode, "instructions": label(value.get("instructions", ""), 2000, multiline=True), "layers": cleaned}


def saved(project):
    path = project / SIDE
    if not path.is_file():
        return {}
    data = load(path)
    if data["version"] != 1:
        raise ValueError("Unsupported scene asset version.")
    return data["scenes"]


def scene_duration(scene):
    return round(scene["end"] - scene["start"], 6)


def for_project(project, plan, timing=None, *, verify=False):
    slots = saved(project)
    if not slots:
        return {}, {}
    scenes = {s["id"]: s for s in (timing or plan)["scenes"]}
    if not slots.keys() <= scenes.keys():
        raise ValueError("Scene asset references no longer match the plan. Remove old scene assignments.")
    entries = {key: asset for key, asset in catalog().items() if visible(asset, project, plan)}
    normalized = {sid: assignment(value, entries, scene_duration(scenes[sid])) for sid, value in slots.items()}
    used = {layer["asset"] for scene in normalized.values() for layer in scene["layers"]}
    subset = {key: entries[key] for key in sorted(used)}
    if verify:
        for asset in subset.values():
            path = original(asset)
            if (path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(LIBRARY.resolve())
                    or hash_file(path) != asset["sha256"]):
                raise ValueError(f'Asset missing or checksum changed: {asset["name"]}')
    return normalized, subset


def put_scene(project, sid, value, plan, timing=None):
    scenes = {s["id"]: s for s in (timing or plan)["scenes"]}
    slots = saved(project)
    if value is None:
        slots.pop(sid, None)
    else:
        if sid not in scenes:
            raise ValueError("Unknown scene.")
        entries = {key: asset for key, asset in catalog().items() if visible(asset, project, plan)}
        slots[sid] = assignment(value, entries, scene_duration(scenes[sid]))
    save(project / SIDE, {"version": 1, "scenes": slots})
    return slots
