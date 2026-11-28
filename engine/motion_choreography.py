"""Narration-driven word-anchor resolver and declarative semantic storyboard compiler."""
from __future__ import annotations

import re

SEMANTIC_ACTIONS = (
    "reveal",
    "type",
    "click",
    "drag",
    "connect",
    "count",
    "highlight",
    "transform",
    "confirm",
    "reject",
    "restore",
    "compare",
    "exit",
)

_TOKEN_RE = re.compile(r"[^\W_][\w\u0900-\u097f']*", re.UNICODE)
ARTWORK_ACTIONS = {"reveal", "type", "connect", "highlight", "focus", "transform", "exit",
                   "move", "scale", "pulse", "sequence"}
_PATH_TOKEN = re.compile(r"[A-Za-z]|[+-]?(?:\d+(?:\.\d*)?|\.\d+)")
_PATH_ARITY = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2, "A": 7, "Z": 0}


def _norm(token: str) -> str:
    return re.sub(r"[^\w\u0900-\u097f]", "", (token or "").casefold())


def _tokenize(text: str) -> list[str]:
    return [_norm(m.group(0)) for m in _TOKEN_RE.finditer(text or "") if _norm(m.group(0))]


def _path_valid(path):
    for match in re.finditer(r"([A-Za-z])([^A-Za-z]*)", path):
        command, tail = match.groups()
        arity = _PATH_ARITY[command.upper()]
        coords = _PATH_TOKEN.findall(tail)
        if not arity:
            if tail.strip():
                return False
            continue
        if (not coords or len(coords) % arity or _PATH_TOKEN.sub("", tail).strip(" ,")
                or any(abs(float(v)) > 1888 for v in coords)):
            return False
        if command.upper() == "A" and any(
            coords[i + j] not in {"0", "1"} for i in range(0, len(coords), 7) for j in (3, 4)
        ):
            return False
    return True


def _phrase_starts(tokens, phrase):
    return [i for i in range(len(tokens)) if tokens[i:i + len(phrase)] == phrase]


def validate_model(scene):
    storyboard = scene.get("storyboard", {})
    model = storyboard.get("model")
    if model is None:
        return
    from production_plan import SCHEMA, load, validate

    schema = load(SCHEMA)
    sid = scene["id"]
    validate(model, schema["$defs"]["visualModel"], f"{sid}.storyboard.model", schema=schema)
    if storyboard.get("limitation"):
        raise ValueError(f"{sid}: a procedural model and an unsupported requirement cannot both be selected.")
    if sum(part["shape"] == "glb" for part in model["parts"]) > 4:
        raise ValueError(f"{sid}: use at most four imported models in one scene.")
    ids = {part["id"] for part in model["parts"]}
    if ids & {"camera", "assembly"} or len(ids) != len(model["parts"]):
        raise ValueError(f"{sid}: model part IDs must be unique and cannot use camera or assembly.")
    if sum((a-b)**2 for a,b in zip(model["eye"], model["look"])) < 1:
        raise ValueError(f"{sid}: the camera must be at least one unit from its subject.")
    for part in model["parts"]:
        if (part["shape"] == "glb") != ("asset" in part):
            raise ValueError(f"{sid}: GLB model parts require a library asset ID.")
        if part["shape"] == "loft":
            rings = part.get("sections", [])
            if (len(rings) < 2 or any(min(ring[3:]) < .005 for ring in rings)
                    or any(a[0] >= b[0] for a,b in zip(rings, rings[1:]))):
                raise ValueError(f"{sid}: loft {part['id']} needs increasing cross sections with positive radii.")
        elif "sections" in part:
            raise ValueError(f"{sid}: sections apply to loft geometry.")
        points = part.get("points", [])
        if part["shape"] == "torus" and part["size"][0] <= 2*part["size"][2]:
            raise ValueError(f"{sid}: torus diameter must be more than twice its tube diameter.")
        if part["shape"] == "tube":
            if len(points) < 2 or any(a == b for a,b in zip(points, points[1:])):
                raise ValueError(f"{sid}: tube {part['id']} needs distinct successive points.")
        elif points:
            raise ValueError(f"{sid}: points apply to tube geometry.")
    tokens = _tokenize(scene["narration"])
    for event in model.get("events", []):
        camera_event = event["action"] in {"camera", "camera-orbit"}
        targets = {"camera"} if camera_event else ids | {"assembly"}
        if event["target"] not in targets or (event["action"] in {"reveal", "hide"} and event["target"] == "assembly"):
            raise ValueError(f"{sid}: unknown procedural model target {event['target']}.")
        if event["action"] != "rotate" and max(map(abs, event["value"])) > 24:
            raise ValueError(f"{sid}: model event position exceeds the scene bounds.")
        if camera_event and sum((a-b)**2 for a,b in zip(event["value"], model["look"])) < 1:
            raise ValueError(f"{sid}: camera destination is too close to the subject.")
        phrase = _tokenize(event["anchor"])
        if not phrase or len(_phrase_starts(tokens, phrase)) < event.get("occurrence", 1):
            raise ValueError(f"{sid}: model cue {event['anchor']!r} is missing from narration.")


def model_events(scene, words, start, duration):
    events = []
    for event in scene["storyboard"]["model"].get("events", []):
        at = resolve_anchor_time(event["anchor"], words, scene["narration"], start, duration,
                                 exact=True, occurrence=event.get("occurrence", 1))
        if at is None or at >= duration:
            raise ValueError(f"{scene['id']}: model cue {event['anchor']!r} has no aligned onset.")
        events.append({**event, "at": at, "duration": min(event.get("duration", .6), duration-at)})
    return sorted(events, key=lambda event: event["at"])


def validate_artwork(scene):
    storyboard = scene.get("storyboard", {})
    artwork = storyboard.get("artwork")
    if artwork is None:
        return
    if storyboard.get("limitation"):
        raise ValueError(f"{scene['id']}: provide artwork or a limitation, not both.")
    from production_plan import SCHEMA, load, validate

    schema = load(SCHEMA)
    rule = schema["properties"]["scenes"]["items"]["properties"]["storyboard"]["properties"]["artwork"]
    sid = scene["id"]
    validate(artwork, rule, f"{sid}.storyboard.artwork", schema=schema)
    if scene["visual"]["type"] in {"three", "canvas"}:
        raise ValueError(f"{sid}: this artwork uses SVG. Use supplied media or a reviewed composition for true 3D or canvas.")
    layers = {layer["id"]: layer["shapes"] for layer in artwork["layers"]}
    if len(layers) != len(artwork["layers"]):
        raise ValueError(f"{sid}: artwork layer IDs must be unique.")
    for shapes in layers.values():
        for shape in shapes:
            kind = shape["type"]
            required = {"rect": {"box"}, "ellipse": {"box"}, "path": {"d"},
                        "text": {"box", "text", "size"}, "image": {"box", "asset"}}[kind]
            allowed = {"type"} | required
            if kind != "image":
                allowed |= {"fill", "stroke", "strokeWidth"}
            if kind == "rect":
                allowed.add("radius")
            if required - shape.keys() or shape.keys() - allowed:
                raise ValueError(f"{sid}: invalid fields for artwork shape {kind}; requires {', '.join(sorted(required))}.")
            if kind == "path":
                if not _path_valid(shape["d"]):
                    raise ValueError(f"{sid}: invalid artwork SVG path coordinates.")
                continue
            x, y, w, h = shape["box"]
            if (x + w > 944 or y + h > 760 or
                    (kind == "text" and (w or h or y + shape["size"] > 760)) or
                    (kind != "text" and (w <= 0 or h <= 0))):
                raise ValueError(f"{sid}: artwork {kind} is outside the 944 × 760 illustration canvas or has an invalid size.")
            if kind == "image" and (shape["asset"] not in scene.get("assets", []) or
                                    not re.search(r"\.(png|jpe?g|webp)$", shape["asset"], re.I)):
                raise ValueError(f"{sid}: artwork images need a local PNG, JPG or WebP listed in scene assets.")
    tokens = _tokenize(scene["narration"])
    for ev in artwork.get("events", []):
        action, target = ev["action"], ev["target"]
        if action not in ARTWORK_ACTIONS or target not in layers:
            raise ValueError(f"{sid}: unsupported artwork action or unknown target: {action} / {target}.")
        if action == "transform" and (not isinstance(ev.get("value"), str) or ev["value"] not in layers or ev["value"] == target):
            raise ValueError(f"{sid}: transform needs a different destination layer ID in value.")
        if action == "move" and (not isinstance(ev.get("value"), list) or len(ev["value"]) != 2
                                  or any(abs(v) > 400 for v in ev["value"])):
            raise ValueError(f"{sid}: move needs [dx, dy] within ±400.")
        if action == "scale" and (not isinstance(ev.get("value"), list) or len(ev["value"]) != 2
                                   or any(v < 0.1 or v > 3.0 for v in ev["value"])):
            raise ValueError(f"{sid}: scale needs [sx, sy] in 0.1–3.0.")
        if action == "sequence" and (not isinstance(ev.get("value"), str) or ev["value"] not in layers or ev["value"] == target):
            raise ValueError(f"{sid}: sequence needs a different destination layer ID in value.")
        if action == "connect" and not any(s["type"] == "path" and s.get("stroke", "none") != "none"
                                           for s in layers[target]):
            raise ValueError(f"{sid}: connect needs a stroked path on layer {target}.")
        if action == "type" and any(s["type"] != "text" for s in layers[target]):
            raise ValueError(f"{sid}: type needs a text-only layer.")
        if ev.get("anchor"):
            phrase = _tokenize(ev["anchor"])
            if not phrase or len(_phrase_starts(tokens, phrase)) < ev.get("occurrence", 1):
                raise ValueError(f"{sid}: artwork anchor {ev['anchor']!r} is missing from the narration.")
        elif "at" not in ev or ev["at"] >= scene["end"] - scene["start"]:
            raise ValueError(f"{sid}: artwork event {action} needs a spoken anchor or an in-scene time.")


def resolve_anchor_time(
    anchors: str | list[str] | tuple[str, ...],
    scene_words: list[dict],
    narration: str,
    scene_start: float,
    scene_dur: float,
    *,
    fallback_ratio: float = 0.35,
    after_rel: float = 0.0,
    exact: bool = False,
    occurrence: int = 1,
) -> float | None:
    """Resolve narration word/phrase anchor into scene-relative seconds [0.05, scene_dur - 0.20]."""
    cands = [anchors] if isinstance(anchors, str) else list(anchors or [])
    dur = max(0.6, float(scene_dur))
    ceiling = max(0.18, dur - 0.22)
    phrase_tokens_list = [_tokenize(c) for c in cands if c and _tokenize(c)]

    if exact:
        # Reviewed timing can group multiple script tokens into one measured interval.
        timed = [(_token, float(word["start"]) - scene_start)
                 for word in scene_words for _token in _tokenize(word["text"])]
        tokens = [token for token, _ in timed] if timed else _tokenize(narration)
        if timed and all(type(word.get("scriptIndex")) is int for word in scene_words):
            # Bind to the original script positions so a skipped word cannot join two
            # formerly separate words into an apparent measured phrase.
            timed_indices = [word["scriptIndex"] + offset for word in scene_words
                             for offset in range(len(_tokenize(word["text"])))]
        else:
            timed_indices = None
        for phrase in phrase_tokens_list:
            matches = _phrase_starts(tokens, phrase)
            if timed_indices is not None:
                matches = [i for i in matches if timed_indices[i + len(phrase) - 1] - timed_indices[i] == len(phrase) - 1]
            if timed and len(matches) != len(_phrase_starts(_tokenize(narration), phrase)):
                continue
            if len(matches) >= occurrence:
                idx = matches[occurrence - 1]
                at = timed[idx][1] if timed else idx / len(tokens) * scene_dur
                return round(max(0.0, min(scene_dur, at)), 3)
        return None

    if scene_words and phrase_tokens_list:
        w_norms = [_norm(w.get("text", "")) for w in scene_words]
        for ptoks in phrase_tokens_list:
            plen = len(ptoks)
            for idx in range(len(w_norms)):
                t_rel = float(scene_words[idx].get("start", scene_start)) - float(scene_start)
                if t_rel < after_rel - 0.04:
                    continue
                if w_norms[idx : idx + plen] == ptoks:
                    return round(max(0.05, min(ceiling, t_rel)), 3)
            # Single-token substring/stem match for inflection tolerance (e.g., repository/repositories, push/pushed)
            head = ptoks[0]
            if len(head) >= 4:
                for idx, wn in enumerate(w_norms):
                    t_rel = float(scene_words[idx].get("start", scene_start)) - float(scene_start)
                    if t_rel >= after_rel - 0.04 and (head in wn or wn in head):
                        return round(max(0.05, min(ceiling, t_rel)), 3)

    # Fallback: proportional position inside narration script when timeline words are unavailable
    narr_tokens = _tokenize(narration)
    if narr_tokens and phrase_tokens_list:
        total = max(1, len(narr_tokens))
        for ptoks in phrase_tokens_list:
            head = ptoks[0]
            for idx, nt in enumerate(narr_tokens):
                est = (idx / total) * dur
                if est >= after_rel and (nt == head or (len(head) >= 4 and (head in nt or nt in head))):
                    return round(max(0.05, min(ceiling, est)), 3)

    base = max(after_rel + 0.12, fallback_ratio * dur)
    return round(max(0.05, min(ceiling, base)), 3)


def compile_scene_storyboard(
    scene: dict,
    component: str,
    scene_words: list[dict],
    scene_start: float,
    scene_dur: float,
    brief: dict | None = None,
    assignment: dict | None = None,
) -> dict:
    """Build deterministic scene storyboard and word-anchored event timings for any niche."""
    explicit = scene.get("storyboard") if isinstance(scene.get("storyboard"), dict) else {}
    narr = scene.get("narration") or ""
    desc = scene.get("visual", {}).get("description") or ""
    heading = scene.get("heading") or scene.get("id", "scene")
    anim = scene.get("animation") or []

    if component == "semantic-artwork":
        artwork = explicit["artwork"]
        resolved = []
        for event in artwork.get("events", []):
            if event.get("anchor"):
                at = resolve_anchor_time(event["anchor"], scene_words, narr, scene_start, scene_dur,
                                         exact=True, occurrence=event.get("occurrence", 1))
                if at is None:
                    raise ValueError(f"{scene['id']}: artwork phrase {event['anchor']!r} has no aligned onset.")
            else:
                at = min(event["at"], scene_dur)
            duration = min(float(event.get("duration", 0.36)), scene_dur - at)
            if duration <= 0:
                raise ValueError(f"{scene['id']}: artwork event {event['action']} falls outside the measured scene.")
            resolved.append({**event, "at": at, "duration": duration})
        return {**explicit, "events": sorted(resolved, key=lambda ev: ev["at"])}

    def anchor(cands, ratio, min_after=0.0):
        return resolve_anchor_time(cands, scene_words, narr, scene_start, scene_dur, fallback_ratio=ratio, after_rel=min_after)

    t_first = round(min(0.28, scene_dur * 0.08), 3)
    t_mid_early = anchor(["github", "version", "control", "feature", "computer", "platform", "team", "commit", "step", "email", "search", "folder", "code"], 0.18, min_after=0.12)
    t_mid = anchor(["connection", "changes", "history", "bigad", "broken", "error", "internet", "offline", "host", "repositories", "review", "local", "hosting", "track", "verify", "score"], 0.44, min_after=max(0.35, t_mid_early + 0.2))
    t_late = anchor(["alag", "wapas", "restore", "undo", "account", "share", "merge", "push", "bhej", "collaboration", "follow", "export", "csv", "dm", "comment"], 0.72, min_after=max(0.65, t_mid + 0.2))

    # Build resolved explicit events first if provided by plan author / ChatGPT
    resolved_events: list[dict] = []
    if explicit.get("events"):
        for ev in explicit["events"]:
            action = ev.get("action", "reveal")
            if action not in SEMANTIC_ACTIONS:
                action = "reveal"
            if "at" in ev and ev["at"] is not None:
                at_s = round(max(0.04, min(scene_dur - 0.15, float(ev["at"]))), 3)
            elif ev.get("anchor"):
                at_s = anchor(ev["anchor"], 0.35)
            else:
                at_s = t_mid
            resolved_events.append({
                "action": action,
                "target": ev.get("target", "primary"),
                "anchor": ev.get("anchor", ""),
                "at": at_s,
                "duration": round(float(ev.get("duration") or 0.42), 3),
                **({"value": ev["value"]} if "value" in ev else {}),
            })
    else:
        # Synthesize narration-driven semantic motion sequence per component type
        comp_events = _default_component_events(component, narr, desc, scene_dur, anchor, t_first, t_mid, t_late)
        resolved_events.extend(comp_events)

    return {
        "purpose": explicit.get("purpose") or f"Illustrate {heading} synchronized with narration",
        "composition": explicit.get("composition") or component,
        "entities": explicit.get("entities") or _extract_entities(heading, desc, brief),
        "elements": explicit.get("elements") or [],
        "events": resolved_events,
        "cues": {
            "t_first": t_first,
            "t_early": t_mid_early,
            "t_mid": t_mid,
            "t_late": t_late,
            "t_exit": round(max(t_late + 0.2, scene_dur - 0.16), 3),
        },
        "transitionIntent": explicit.get("transitionIntent") or scene.get("visual", {}).get("transition", "smooth-editorial"),
        "stylingEmphasis": explicit.get("stylingEmphasis") or (brief.get("style", "editorial-motion") if brief else "editorial-motion"),
        "assignedAssets": explicit.get("assignedAssets") or [layer.get("assetId", "") for layer in (assignment or {}).get("layers", []) if layer.get("assetId")],
    }


def _extract_entities(heading: str, desc: str, brief: dict | None) -> list[str]:
    found = []
    for m in re.finditer(r"\b[A-Z][A-Za-z0-9.+#/-]{1,24}\b", f"{heading} {desc}"):
        w = m.group(0)
        if w not in found and w.lower() not in {"the", "and", "for", "with"}:
            found.append(w)
        if len(found) >= 6:
            break
    if not found and brief and brief.get("topic"):
        found.append(str(brief["topic"])[:40])
    return found


def _default_component_events(component, narr, desc, dur, anchor, t_first, t_mid, t_late):
    if component == "comparison_split":
        t_left = anchor(["git", "first", "local", "old", "before", "others"], 0.04)
        t_right = anchor(["github", "second", "cloud", "new", "after", "platform"], 0.20, min_after=t_left + 0.25)
        t_clash = anchor(["same", "nahi", "not", "different", "alag", "never", "vs"], 0.28, min_after=t_right + 0.25)
        t_conn = anchor(["dono", "connection", "connect", "bridge", "both", "tool", "aur"], 0.48, min_after=t_clash + 0.35)
        t_benefit = anchor(["easy", "simple", "fast", "clear", "benefit", "collaboration", "helpful"], 0.76, min_after=t_conn + 0.35)
        if dur >= 3.0:
            t_benefit = round(min(t_benefit, dur - 1.05), 3)
        return [
            {"action": "reveal", "target": "left-card", "anchor": "primary-subject", "at": t_left, "duration": 0.34},
            {"action": "compare", "target": "right-card", "anchor": "secondary-subject", "at": t_right, "duration": 0.34},
            {"action": "confirm", "target": "verdict-stamp", "anchor": "contrast", "at": t_clash, "duration": 0.36},
            {"action": "connect", "target": "bridge-connector", "anchor": "connection", "at": t_conn, "duration": 0.65},
            {"action": "highlight", "target": "benefit-pill", "anchor": "outcome", "at": t_benefit, "duration": 0.34},
        ]
    if component in ("editor_history", "three_workstation"):
        t_ed = round(min(anchor(["git", "editor", "file", "version"], 0.08), 0.35), 3)
        t_diff = anchor(["code", "changes", "feature", "edit", "track"], 0.36, min_after=t_ed + 0.25)
        t_hist = anchor(["history", "track", "commit", "snapshot", "save"], 0.62, min_after=t_diff + 0.2)
        return [
            {"action": "reveal", "target": "editor-window", "anchor": "editor", "at": t_ed, "duration": 0.38},
            {"action": "type", "target": "code-change", "anchor": "changes", "at": t_diff, "duration": 0.55},
            {"action": "highlight", "target": "history-commits", "anchor": "history", "at": t_hist, "duration": 0.55},
        ]
    if component == "history_snapshots":
        t_feat = anchor(["feature", "banaya", "code", "v2"], 0.14)
        t_err = anchor(["bigad", "broken", "error", "bug", "fail"], 0.46, min_after=t_feat + 0.3)
        t_hist = anchor(["git", "history", "log", "versions"], 0.58, min_after=t_err + 0.25)
        t_pick = anchor(["purane", "version", "v1", "old"], 0.70, min_after=t_hist + 0.25)
        t_rest = anchor(["wapas", "restore", "undo", "recover"], 0.82, min_after=t_pick + 0.25)
        return [
            {"action": "type", "target": "v2-feature", "anchor": "feature", "at": t_feat, "duration": 0.42},
            {"action": "reject", "target": "v3-broken", "anchor": "error", "at": t_err, "duration": 0.40},
            {"action": "reveal", "target": "history-drawer", "anchor": "history", "at": t_hist, "duration": 0.36},
            {"action": "click", "target": "cursor-select-v1", "anchor": "version", "at": t_pick, "duration": 0.42},
            {"action": "restore", "target": "rollback-arc", "anchor": "restore", "at": t_rest, "duration": 0.55},
        ]
    if component in ("offline_workflow", "three_node_offline"):
        t_comp = round(min(anchor(["git", "computer", "laptop", "workstation"], 0.08), 0.35), 3)
        t_off = anchor(["bina internet", "without internet", "internet", "offline", "wifi"], 0.38, min_after=t_comp + 0.2)
        t_work = anchor(["kaam", "works", "work", "save", "commit"], 0.58, min_after=t_off + 0.25)
        t_acct = anchor(["account", "github", "zaroori nahi", "optional"], 0.76, min_after=t_work + 0.25)
        return [
            {"action": "reveal", "target": "local-machine", "anchor": "computer", "at": t_comp, "duration": 0.36},
            {"action": "transform", "target": "wifi-toggle-off", "anchor": "offline", "at": t_off, "duration": 0.34},
            {"action": "confirm", "target": "local-save-ok", "anchor": "commit", "at": t_work, "duration": 0.38},
            {"action": "highlight", "target": "optional-account", "anchor": "account", "at": t_acct, "duration": 0.34},
        ]
    if component == "platform_browser":
        t_open = anchor(["github", "platform", "website", "dashboard", "browser"], 0.10)
        t_host = anchor(["repositories", "host", "files", "code"], 0.48, min_after=t_open + 0.3)
        t_share = anchor(["share", "link", "invite", "public"], 0.75, min_after=t_host + 0.3)
        return [
            {"action": "reveal", "target": "browser-window", "anchor": "platform", "at": t_open, "duration": 0.38},
            {"action": "reveal", "target": "repo-files", "anchor": "host", "at": t_host, "duration": 0.45},
            {"action": "click", "target": "share-button", "anchor": "share", "at": t_share, "duration": 0.45},
        ]
    if component == "collaboration_network":
        t_team = anchor(["team", "developers", "contributors"], 0.08)
        t_rev = anchor(["review", "code review", "changes", "diff"], 0.32, min_after=t_team + 0.25)
        t_merge = anchor(["merge", "pull request", "github", "sync"], 0.68, min_after=t_rev + 0.3)
        return [
            {"action": "reveal", "target": "contributor-nodes", "anchor": "team", "at": t_team, "duration": 0.38},
            {"action": "connect", "target": "branch-review", "anchor": "review", "at": t_rev, "duration": 0.55},
            {"action": "click", "target": "merge-pr-btn", "anchor": "merge", "at": t_merge, "duration": 0.50},
        ]
    if component == "terminal_pipeline":
        t_commit = anchor(["commit", "changes", "add"], 0.22)
        t_local = anchor(["local", "history", "save"], 0.40, min_after=t_commit + 0.2)
        t_push = anchor(["push", "git push"], 0.64, min_after=t_local + 0.25)
        t_xfer = anchor(["commits", "bhej", "remote", "transfer", "upload"], 0.72, min_after=t_push + 0.20)
        t_remote = anchor(["github", "online", "live", "synced"], 0.82, min_after=t_xfer + 0.20)
        return [
            {"action": "type", "target": "cmd-commit", "anchor": "commit", "at": t_commit, "duration": 0.46},
            {"action": "confirm", "target": "local-repo-card", "anchor": "local", "at": t_local, "duration": 0.40},
            {"action": "type", "target": "cmd-push", "anchor": "push", "at": t_push, "duration": 0.46},
            {"action": "connect", "target": "push-transfer-arc", "anchor": "transfer", "at": t_xfer, "duration": 0.70},
            {"action": "confirm", "target": "remote-sync-confirm", "anchor": "github", "at": t_remote, "duration": 0.38},
        ]
    if component == "folder_workspace":
        t_drop = anchor(["folder", "project", "workspace", "drop"], 0.14)
        t_sub = anchor(["footage", "assets", "style", "export", "files", "ready"], 0.48, min_after=t_drop + 0.25)
        t_done = anchor(["organize", "done", "clean", "start"], 0.76, min_after=t_sub + 0.25)
        return [
            {"action": "reveal", "target": "dropzone-card", "anchor": "folder", "at": t_drop, "duration": 0.36},
            {"action": "click", "target": "folder-expand-btn", "anchor": "files", "at": t_sub, "duration": 0.42},
            {"action": "confirm", "target": "workspace-tree", "anchor": "ready", "at": t_done, "duration": 0.38},
        ]
    if component == "search_serp":
        t_q = anchor(["search", "google", "website", "query", "keyword"], 0.14)
        t_rows = anchor(["result", "page", "rank", "top"], 0.44, min_after=t_q + 0.25)
        t_verdict = anchor(["not found", "missing", "rank", "found", "zero"], 0.72, min_after=t_rows + 0.25)
        return [
            {"action": "type", "target": "search-query", "anchor": "search", "at": t_q, "duration": 0.55},
            {"action": "reveal", "target": "serp-results", "anchor": "result", "at": t_rows, "duration": 0.40},
            {"action": "reject" if any(k in f"{narr} {desc}".lower() for k in ("not found", "missing", "hide", "lose")) else "confirm", "target": "serp-stamp", "anchor": "verdict", "at": t_verdict, "duration": 0.36},
        ]
    if component == "metric_showcase":
        t_kpi = anchor(["percent", "%", "score", "rate", "metric", "ratio"], 0.16)
        t_bar = anchor(["target", "goal", "benchmark", "growth", "impact"], 0.48, min_after=t_kpi + 0.25)
        t_check = anchor(["verified", "result", "total", "ready"], 0.74, min_after=t_bar + 0.25)
        return [
            {"action": "count", "target": "metric-kpi-1", "anchor": "metric", "at": t_kpi, "duration": 0.75},
            {"action": "count", "target": "metric-gauge", "anchor": "target", "at": t_bar, "duration": 0.85},
            {"action": "confirm", "target": "metric-stamp", "anchor": "verified", "at": t_check, "duration": 0.34},
        ]
    if component == "creator_cta_outro":
        t_cta = anchor(["follow", "subscribe", "comment", "save", "dm"], 0.65, min_after=0.35)
        return [
            {"action": "reveal", "target": "creator-profile", "anchor": "intro", "at": t_first, "duration": 0.38},
            {"action": "click", "target": "follow-cta", "anchor": "follow", "at": t_cta, "duration": 0.45},
        ]
    return [
        {"action": "reveal", "target": "stage-primary", "anchor": "start", "at": t_first, "duration": 0.38},
        {"action": "highlight", "target": "stage-detail", "anchor": "middle", "at": t_mid, "duration": 0.42},
        {"action": "confirm", "target": "stage-finish", "anchor": "end", "at": t_late, "duration": 0.36},
    ]
