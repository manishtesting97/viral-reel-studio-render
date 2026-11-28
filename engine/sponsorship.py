"""Optional local sponsor profiles and scene disclosures."""
from datetime import date
from html import escape
from pathlib import Path
import re
from urllib.parse import urlsplit

import asset_library as media
from production_plan import PROJECTS, ROOT, load, save

FOLDER = ROOT / "assets" / "sponsors"
SIDE = "sponsorship.json"
MODES = ("normal", "full", "selected", "placement")


def profiles():
    return {path.stem: load(path) for path in sorted(FOLDER.glob("*.json"))}


def profile_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", value):
        raise ValueError("Use a sponsor ID starting with a letter, with letters, digits and hyphens.")
    return value


def read_profile(ident):
    path = FOLDER / (profile_id(ident) + ".json")
    if not path.is_file():
        raise ValueError(f"Missing sponsor profile: {ident}")
    return load(path)


def date_value(value):
    if value and (not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) or not date.fromisoformat(value)):
        raise ValueError("Enter dates as YYYY-MM-DD.")
    return value


def references(profile):
    return [asset for asset in [profile.get("logo"), *profile.get("assets", [])] if asset]


def profile_assets(profile):
    ids = references(profile)
    catalog = media.catalog() if ids else {}
    assets = {ident: media.record(ident, catalog) for ident in ids}
    if any(asset["scope"] != "global" for asset in assets.values()):
        raise ValueError("Reusable sponsor profiles use assets from the global library.")
    if profile.get("logo") and assets[profile["logo"]]["kind"] != "image":
        raise ValueError("The sponsor logo must be an image.")
    return assets


def edit_profile(data):
    ident = profile_id(data["id"])
    path = FOLDER / (ident + ".json")
    if data.get("action") == "delete":
        if data.get("confirmed") is not True:
            raise ValueError("Confirm sponsor profile deletion.")
        for side in PROJECTS.glob(f"*/{SIDE}"):
            config = load(side)
            if ident == config.get("sponsor") or any(ident == row.get("sponsor") for row in config["scenes"].values()):
                raise ValueError(f"Sponsor is assigned to project {side.parent.name}.")
        path.unlink()
        return
    name = media.label(data.get("name"), 64)
    if not name:
        raise ValueError("Enter the sponsor brand name.")
    value = {"version": 1, "id": ident, "name": name}
    for key in ("product", "claims", "instructions", "prohibited", "mentions"):
        value[key] = media.label(data.get(key, ""), 2000, multiline=True)
    for key, length in (("website", 240), ("coupon", 24), ("cta", 64), ("accent", 7), ("secondary", 7),
                        ("validFrom", 10), ("validUntil", 10)):
        value[key] = media.label(data.get(key, ""), length)
    if value["website"]:
        url = urlsplit(value["website"])
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or re.search(r"\s", value["website"]):
            raise ValueError("Use an HTTP or HTTPS landing page without credentials.")
    for key in ("accent", "secondary"):
        if not re.fullmatch(r"#[\da-fA-F]{6}", value[key]):
            raise ValueError("Use two six-digit hex brand colors.")
    date_value(value["validFrom"])
    date_value(value["validUntil"])
    if value["validFrom"] and value["validUntil"] and value["validFrom"] > value["validUntil"]:
        raise ValueError("The offer end date must follow its start date.")
    value["logo"] = data.get("logo") or ""
    ids = data.get("assets", [])
    if not isinstance(ids, list) or len(ids) > 30 or any(not isinstance(x, str) for x in ids):
        raise ValueError("Choose up to 30 promotional assets.")
    value["assets"] = list(dict.fromkeys(ids))
    profile_assets(value)
    save(path, value)


def saved(project):
    path = project / SIDE
    return load(path) if path.is_file() else {"version": 1, "mode": "normal", "scenes": {}}


def normalize(config, plan, *, rendering=False):
    if config.get("version") != 1 or config.get("mode") not in MODES:
        raise ValueError("Unsupported sponsorship settings.")
    mode = config["mode"]
    if mode == "normal":
        return {"version": 1, "mode": mode, "scenes": {}}, {}
    default = read_profile(config.get("sponsor"))
    rows = config.get("scenes", {})
    if not isinstance(rows, dict) or (plan and not rows.keys() <= {s["id"] for s in plan["scenes"]}):
        raise ValueError("Sponsorship references a scene missing from the current plan. Renew the scene selection in Step 7.")
    if rendering and mode in {"selected", "placement"} and not rows:
        raise ValueError("Choose at least one sponsored or product-placement scene in Step 7.")
    used = {default["id"]: default}
    clean = {}
    for sid, row in rows.items():
        profile = read_profile(row["sponsor"]) if row.get("sponsor") else default
        used[profile["id"]] = profile
        clean[sid] = {"sponsor": row.get("sponsor") or "", "cta": row.get("cta") is True,
                      "instructions": media.label(row.get("instructions", ""), 2000, multiline=True)}
    for profile in used.values():
        profile_assets(profile)
    disclosure = config.get("disclosure", "sponsor")
    if disclosure not in {"ad", "sponsor"}:
        raise ValueError("Choose AD or SPONSOR disclosure.")
    return {"version": 1, "mode": mode, "sponsor": default["id"], "disclosure": disclosure,
            "endCTA": config.get("endCTA") is True, "showCoupon": config.get("showCoupon") is True,
            "scenes": clean}, used


def store(project, config, plan):
    previous = saved(project)
    if config.get("mode") == "normal" and previous["mode"] != "normal" and config.get("confirmDisable") is not True:
        raise ValueError("Confirm that no paid collaboration remains before turning off disclosure.")
    value, _ = normalize(config, plan)
    save(project / SIDE, value)


def prompt_context(project, plan):
    config, used = normalize(saved(project), plan)
    if not used:
        return {}
    return {"project": config,
            "sponsors": [{**profile, "media": [{key: asset[key] for key in ("id", "name", "kind", "description")}
                                               for asset in profile_assets(profile).values()]}
                         for profile in used.values()],
            "rules": ("Use only explicitly approved claims and offer details. Never invent endorsements, personal use, "
                      "testimonials, benefits or statistics. Follow required mentions and prohibited wording. Keep paid "
                      "disclosure explicit. Keep stable scene IDs and approved recordings. Add sparse voice_direction "
                      "cues for natural promotional delivery; do not include delivery tags in narration. Asset IDs are "
                      "for media assignments, not paths in the approved scene code. Include a sponsor CTA only as configured.")}


# Composition pixels. Reserve the full card lane, including wrapped CTA text.
CARD_BOTTOM = 340
CAPTION_MARGIN = CARD_BOTTOM + 166 + 26


def scene_scope(config, sid, *, last):
    mode = config["mode"]
    if mode == "normal":
        return False, False
    return mode == "full" or sid in config["scenes"], last and config.get("endCTA", False)


CSS = """
.vrs-sponsor-badge {position:absolute;z-index:40;top:calc(var(--vrs-header-top,283px) + 6px);
 right:var(--vrs-header-left,68px);display:flex;align-items:center;min-height:44px;
 padding:4px 20px;border:1px solid #ffffffad;border-radius:999px;background:#242333;
 color:#fff;box-shadow:0 6px 18px #18141224;font:800 22px/1 var(--brand-font,sans-serif);
 letter-spacing:.065em;pointer-events:none}
.vrs-disclosure {position:absolute;z-index:40;bottom:var(--vrs-sponsor-bottom);
 left:50%;translate:-50% 0;width:max-content;max-width:760px;box-sizing:border-box;
 display:flex;align-items:center;gap:14px;padding:10px 22px;
 background:#fffdf9;color:#181412;border:1px solid #d8d4d0;
 border-inline-start:4px solid var(--sponsor-accent);border-bottom-color:var(--sponsor-secondary);
 border-radius:19px;box-shadow:0 7px 20px #1814121c;
 font:700 27px/1.12 var(--brand-font,sans-serif);overflow-wrap:anywhere;pointer-events:none}
.vrs-disclosure img {flex:none;width:52px;height:48px;object-fit:contain}
.vrs-disclosure small {display:block;color:#47434b;font-size:20px;line-height:1.16;font-weight:500}
.vrs-disclosure .vrs-sponsored-by {font-size:17px;margin-bottom:3px}
.vrs-disclosure strong {display:block}
.vrs-disclosure strong + small {margin-top:3px}
#stage .vrs-sponsored-scene .vrs-auto-layout {
 bottom:max(var(--vrs-artwork-bottom,550px),var(--vrs-caption-clearance));
}
"""


def panels(target, config, used, timing, duration):
    if not used:
        return "", ""
    assets = {}
    for profile in used.values():
        assets.update(profile_assets(profile))
    logo_dir = target / "assets" / "sponsor-media"
    overlays = []
    motion = []
    for index, scene in enumerate(timing["scenes"]):
        row = config["scenes"].get(scene["id"], {})
        last = index == len(timing["scenes"]) - 1
        sponsored, end_cta = scene_scope(config, scene["id"], last=last)
        if not sponsored and not end_cta:
            continue
        profile = used[row.get("sponsor") or config["sponsor"]]
        cta = (row.get("cta") or end_cta)
        start = scene["start"]
        end = duration if last else scene["end"]
        tail = max(start, end - 3)
        logo = ""
        if profile.get("logo"):
            asset = assets[profile["logo"]]
            logo_dir.mkdir(parents=True, exist_ok=True)
            output = logo_dir / (asset["id"] + ".png")
            if not output.is_file():
                source = media.original(asset)
                media.ffmpeg(*media.input_options(source.suffix[1:]), "-i", source, "-frames:v", 1,
                             "-vf", "scale=160:160:force_original_aspect_ratio=decrease",
                             "-threads", 1, output)
            logo = f'<img src="assets/sponsor-media/{output.name}" alt="">'
        detail = escape(profile["cta"]) if cta else ""
        if cta and config["showCoupon"] and profile["coupon"]:
            detail += (" · " if detail else "") + "Code " + escape(profile["coupon"])
        segments = [(start, end, detail)]
        if end_cta and not row.get("cta"):
            segments = ([(start, tail, "")] if sponsored and tail > start else []) + [(tail, end, detail)]
        badge = "AD" if config["disclosure"] == "ad" else "SPONSOR"
        badge_start = start if sponsored else tail
        badges = f'#{scene["id"]} :is(.vrs-meta-badge,.k8s-chapter)'
        motion.append(f'tl.set("{badges}",{{visibility:"hidden"}},{badge_start});')
        overlays.append(
            f'<aside class="clip vrs-sponsor-badge" id="vrs-sponsor-badge-{index}" data-start="{badge_start:.9f}" '
            f'data-duration="{end-badge_start:.9f}" data-track-index="501">{badge}</aside>')
        for j, (a, b, message) in enumerate(segments):
            card = f"vrs-disclosure-{index}-{j}"
            motion.append(f'tl.fromTo("#{card}",{{opacity:.65,y:4}},'
                          f'{{opacity:1,y:0,duration:{min(.24,(b-a)/3):.6f},ease:"power2.out",immediateRender:false}},{a});')
            notes = f"<small>{message}</small>" if message else ""
            overlays.append(
                f'<aside id="{card}" class="clip vrs-disclosure" data-start="{a:.9f}" '
                f'data-duration="{b-a:.9f}" data-track-index="500" '
                f'style="--sponsor-accent:{profile["accent"]};--sponsor-secondary:{profile["secondary"]};'
                f'--vrs-sponsor-bottom:{CARD_BOTTOM}px">{logo}<div><small class="vrs-sponsored-by">Sponsored by</small>'
                f'<strong>{escape(profile["name"])}</strong>{notes}</div></aside>')
    return "".join(overlays), "".join(motion)


def render_context(project, plan, *, verify=False, assignments=None):
    config, used = normalize(saved(project), plan, rendering=True)
    inventory = {}
    for profile in used.values():
        inventory.update(profile_assets(profile))
    if used:
        for scene in plan["scenes"]:
            sid = scene["id"]
            selection = (assignments or {}).get(sid, {})
            media_ids = {layer["asset"] for layer in selection.get("layers", [])}
            if selection.get("mode") not in {"image", "video"}:
                media_ids.update(part["asset"] for part in scene.get("storyboard", {}).get("model", {}).get("parts", [])
                                 if part["shape"] == "glb")
            registered = media_ids & inventory.keys()
            if not registered:
                continue
            if config["mode"] != "full" and sid not in config["scenes"]:
                raise ValueError(f"Scene {sid} uses a registered sponsor asset. Select that scene for disclosure in Step 7.")
            partner = config["scenes"].get(sid, {}).get("sponsor") or config["sponsor"]
            if registered.difference(references(used[partner])):
                raise ValueError(f"Scene {sid} uses media registered to another active sponsor. Review its sponsor and assets in Step 7.")
    if verify:
        for asset in inventory.values():
            path = media.original(asset)
            if (path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(media.LIBRARY.resolve())
                    or media.hash_file(path) != asset["sha256"]):
                raise ValueError(f'Sponsor asset is missing or changed: {asset["name"]}')
    if config.get("showCoupon"):
        coupon_sponsors = {row["sponsor"] or config["sponsor"] for row in config["scenes"].values() if row["cta"]}
        if config["endCTA"]:
            final = config["scenes"].get(plan["scenes"][-1]["id"], {})
            coupon_sponsors.add(final.get("sponsor") or config["sponsor"])
        today = date.today().isoformat()
        for key in coupon_sponsors:
            profile = used[key]
            if profile["coupon"] and (profile["validFrom"] and today < profile["validFrom"]
                                      or profile["validUntil"] and today > profile["validUntil"]):
                raise ValueError(f'Offer code for {profile["name"]} is outside its validity dates.')
    return config, used, {key: value["sha256"] for key, value in inventory.items()}
