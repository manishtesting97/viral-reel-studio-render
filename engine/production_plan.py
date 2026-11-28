"""Production-plan validation, staging and narration alignment."""
import hashlib
import json
import math
import os
import resource
import time
from studio_paths import MODELS
from pathlib import Path
import re
import tempfile

from alignment import DEVANAGARI, MIN_ANCHOR_CONFIDENCE, QUALITY_FLOOR, match_script, normalized, valid_span
from preview_phase2 import BRAND_DIR, CACHE, ENGINE, ROOT, STYLE_LIB, probe_duration, run
from voiceover_director import (allowed, CATALOG, INTENTS, clean_narration, director_view, possible_spoken_tags,
                                preferences, validate_expressive, validate_scene)

SCHEMA = ENGINE / "production.schema.json"
CONFIG = tuple("id topic audience niche language duration brand style".split())
PROJECTS = ROOT / "projects"
TOKEN = re.compile(r"[^\W_][\w\u0900-\u097f'’]*", re.UNICODE)


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
        file.write(text.encode("utf-8"))
    Path(file.name).replace(path)


def save(path, data):
    write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def digest(*inputs):
    key = hashlib.sha256()
    for item in inputs:
        if isinstance(item, Path):
            with item.open("rb") as file:
                data = hashlib.file_digest(file, "sha256").hexdigest()
        else:
            data = json.dumps(item, sort_keys=True, ensure_ascii=False)
        key.update(data.encode("utf-8") + b"\0")
    return key.hexdigest()[:20]


def timing_key(voice, plan):
    # Artwork can be revised independently of approved narration and caption evidence.
    base = {key: value for key, value in plan.items() if key != "badge"}
    base["scenes"] = []
    for scene in plan["scenes"]:
        row = {key: value for key, value in scene.items() if key != "voice_direction"}
        if {"artwork", "model", "limitation"} & row.get("storyboard", {}).keys():
            storyboard = {key: value for key, value in row.pop("storyboard").items()
                          if key not in {"artwork", "model", "limitation"}}
            if storyboard:
                row["storyboard"] = storyboard
        base["scenes"].append(row)
    return digest(voice, base)


def current_alignment_report(voice, plan, key=None):
    path = voice.with_name("alignment-report.json")
    if not voice.is_file() or not path.is_file():
        return {}
    report = load(path)
    if "timingKey" in report:
        return report if report["timingKey"] == (key or timing_key(voice, plan)) else {}
    if report.get("recognitionCache") and report.get("model"):
        filename = f".whisper-{digest(voice, plan['language'], report['model'])}.json"
        return report if report["recognitionCache"] == filename else {}
    return report  # Legacy reports have no input fingerprint.


def timing_is_current(timeline, voice, plan):
    if not timeline.get("key") or not voice.is_file():
        return False
    key = timing_key(voice, plan)
    return (timeline["key"] in {key, digest(voice, plan)}
            and current_alignment_report(voice, plan, key).get("status") not in {"failed", "running"})


def validate(value, rule, path="$", *, schema=None):
    """Evaluate the JSON Schema keywords used in the versioned contract."""
    schema = rule if schema is None else schema
    if "$ref" in rule:
        rule = schema["$defs"][rule["$ref"].removeprefix("#/$defs/")]
    if "anyOf" in rule:
        for option in rule["anyOf"]:
            try:
                validate(value, option, path, schema=schema)
            except ValueError:
                continue
            break
        else:
            raise ValueError(f"{path}: value does not match any permitted shape")
    kind = rule.get("type")
    valid = {
        "object": type(value) is dict,
        "array": type(value) is list,
        "string": type(value) is str,
        "integer": type(value) is int,
        "boolean": type(value) is bool,
        "number": type(value) in (int, float) and abs(value) <= 1e100,
    }
    if (kind and not valid[kind]) or ("const" in rule and value != rule["const"]):
        raise ValueError(f"{path}: expected {rule.get('const', kind)}")
    if "enum" in rule and value not in rule["enum"]:
        raise ValueError(f"{path}: expected one of {rule['enum']}")
    if kind == "object":
        req = set(rule.get("required", ()))
        if not req.issubset(value) or not set(value).issubset(rule["properties"]):
            raise ValueError(f"{path}: missing {sorted(req - value.keys())}, "
                             f"unknown {sorted(value.keys() - set(rule['properties']))}")
        for key, item in value.items():
            validate(item, rule["properties"][key], f"{path}.{key}", schema=schema)
    elif kind == "array":
        if not rule["minItems"] <= len(value) <= rule["maxItems"]:
            raise ValueError(f"{path}: invalid item count")
        for index, item in enumerate(value):
            validate(item, rule["items"], f"{path}[{index}]", schema=schema)
    elif kind == "string":
        if not value.strip() or not rule["minLength"] <= len(value) <= rule["maxLength"]:
            raise ValueError(f"{path}: invalid text length")
        if "pattern" in rule and not re.fullmatch(rule["pattern"], value):
            raise ValueError(f"{path}: invalid identifier or relative asset path")
    elif kind in {"number", "integer"} and not rule.get("minimum", -math.inf) <= value <= rule.get("maximum", math.inf):
        raise ValueError(f"{path}: out of range")


def profiles(config, project=None):
    style_id = config["style"]
    if project is not None:
        vc_path = Path(project) / "visual-config.json"
        if vc_path.is_file():
            try:
                override = load(vc_path).get("style")
                if (isinstance(override, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,47}", override)
                        and (STYLE_LIB / f"{override}.json").is_file()):
                    style_id = override
            except Exception:
                pass
    return load(BRAND_DIR / f"{config['brand']}.json"), load(STYLE_LIB / f"{style_id}.json")


def init_project(project, config):
    schema = load(SCHEMA)
    for key in CONFIG:
        validate(config[key], schema["properties"][key], key)
    if project.exists():
        raise ValueError("Project already exists. Resume using its saved files.")
    profiles(config)
    save(project / "brief.json", config)
    write_prompt(project, config)


def write_prompt(project, config, badge=None):
    write(project / "production-prompt.txt", production_prompt(project, config, badge))


def production_prompt(project, config, badge=None):
    from sponsorship import prompt_context

    brand, style = profiles(config)
    existing_plan = project / "plan.json"
    sponsor_context = prompt_context(project, load(existing_plan) if existing_plan.is_file() else None)
    prefs = preferences(director_state(project).get("preferences", {}))
    brief_id = str(config.get("id") or "reel-plan").strip() or "reel-plan"
    creator_name = str(brand.get("ownerName") or brand.get("brandName") or brand.get("id") or "Creator").strip()
    handle_raw = str(brand.get("instagramUsername") or brand.get("handle") or "").strip().lstrip("@")
    handle = f"@{handle_raw}" if handle_raw else ""
    raw_cta = brand.get("cta")
    brand_cta = str(raw_cta).strip() if isinstance(raw_cta, str) else ""
    cta_enabled = raw_cta is not False and brand_cta.casefold() not in {"none", "off", "disabled"} and bool(brand_cta or handle)
    profile_picture = str(brand.get("profilePicture") or "").strip()
    if cta_enabled:
        profile_title = str(brand.get("profileTitle") or "").strip()
        identity_bits = [part for part in (creator_name, handle, profile_title,
                                           f'brand CTA "{brand_cta}"' if brand_cta else "") if part]
        asset_note = (f'The approved component uses the configured photo "{profile_picture}". ' if profile_picture else "")
        cta_guidance = (
            f"Instagram Follow CTA ({', '.join(identity_bits)}): end the reel with one short, natural, topic-relevant "
            f"spoken follow CTA scene (~3 to 5 seconds) inside total brief.duration (set outro to 0 so no duplicate "
            "spoken or visual outro is added). Adapt the CTA naturally to brief.topic and brief.language instead of "
            "using identical boilerplate; only use this brand profile's creator name, handle "
            + (f"({handle}) " if handle else "")
            + "and CTA setting. Use closing scene id \"creator-cta\" and the approved creator_cta_outro component. "
            "Omit storyboard.artwork and storyboard.model for this scene. "
            f"{asset_note}Use the exact spoken follow phrase in scene.narration as the storyboard.events anchor "
            "for follow-cta. Preserve the configured name, handle and profile title on screen. "
        )
    else:
        cta_guidance = (
            "Respect the selected brand profile CTA setting (disabled): omit any follow CTA scene, set outro to 0, "
            "and never insert another brand's handle. "
        )
    return ("Create an original vertical Instagram Reel production plan conforming to the schema below. "
          "Copy the brief values exactly. Use contiguous estimated scene times from 0 through duration, "
          "and scene-relative animation/audio cue times. "
          "Respect brief.language strictly: do not force Hinglish into Hindi or English projects. "
          "Scroll-stopping hook (first 1 to 3 seconds): open immediately on a strong, topic-specific curiosity gap, "
          "surprising fact, relatable problem, meaningful question or unexpected comparison tailored to brief.topic, "
          "niche, audience, language and duration. Give viewers a genuine reason to stop scrolling and deliver the "
          "answer or payoff promised by the hook. Never use generic openers such as \"Hello guys\", "
          "\"Aaj hum baat karenge\" or empty greetings, and avoid clickbait, fake statistics or exaggerated claims. "
          "Write a short, readable, impactful on-screen hook heading. Make it visible from frame 0 where supported, "
          "make the subject immediately recognizable, and match "
          "the opening visual and animation directly to the spoken hook. Vary hook formulas across reels rather than repeating one template. "
          "Audience retention: build from hook through curiosity, demonstration, explanation and satisfying payoff to the brand-approved CTA. "
          "Every sentence must teach something or advance the story. Cut repetition, filler and long definitions. Use relatable "
          "examples, practical demonstrations and relevant visual changes at meaningful narration beats with "
          "curiosity, contrast, demonstration and clear explanation while preserving factual accuracy. "
          f"{cta_guidance}"
          "\n\nVoiceover: write natural, engaging narration with the pacing of an experienced Indian creator. "
          "For Hinglish projects, use natural Roman-script Hinglish with essential English technical terms and simple surrounding language. "
          "Use the selected language and script for Hindi or English projects. Mentally read the script aloud, including "
          "pauses, to fit the specified duration. Preserve strong hooks, storytelling, "
          "audience retention, essential technical terminology and factual accuracy without sacrificing content quality "
          "or inventing claims, statistics or recognition guarantees. "
          "For Hindi and Hinglish, prefer commonly spoken Indian words, short clear sentences with natural clause boundaries, "
          "simple surrounding explanations around necessary technical terms, and avoid unnecessarily complex English vocabulary, "
          "difficult Sanskrit-based Hindi words or excessive Hindi-English switching within a single sentence. "
          "Put only spoken words in narration: represent spoken initialisms and abbreviations consistently across narration, "
          "captions and word anchors, spell out numbers, percentages, symbols and units as intended to be spoken, and avoid "
          "slash-separated alternatives, ambiguous abbreviations, tongue twisters, excessive slang, awkward pronunciation clusters "
          "or unnecessary special characters. Maintain a moderate, natural speaking pace fitting the requested duration. "
          "Illustrative narration examples only (not fixed substitutions or topic rules): "
          "\"During emergency braking, ABS continuously modulates hydraulic brake pressure.\" -> "
          "\"Jab aap achanak brake lagate hain, tab A B S brake pressure ko control karta hai.\"; "
          "\"The system prevents wheel locking and improves manoeuvrability.\" -> "
          "\"Ye wheel lock hone ka risk kam karta hai, aur gaadi ko steer karna aasaan banata hai.\"; "
          "\"Approximately\" -> \"Lagbhag\"; \"Subsequently\" -> \"Uske baad\". "
          "For each scene, decide whether a change of delivery helps the listener. Tailor delivery to that scene's purpose, actual "
          "narration, niche and audience. For Hinglish, favor natural conversational Indian delivery and "
          "keep the approved code-switching. Use an optional scene note plus sparse cues with one intent "
          "from voiceDirection.intents (or a bare tag from allowedTags) and a zero-based beforeWord "
          "index into the whitespace-separated narration (word count means after the last word). "
          "A cue can start a scene, a sentence or a meaningful clause within a sentence. Use cues and meaningful pauses "
          "only when genuinely useful. Consider curious for the hook, surprised for a real revelation, confident for an explanation, suspenseful for a meaningful build, pause for a natural break, and warm for the CTA. "
          "Do not return pause-only directions for every scene when the narration supports other expressions. "
          "Leave neutral scenes untagged. Add a vocal reaction only when the spoken beat warrants it. "
          "Avoid repetitive emotions, excessive dramatic pauses, whispering, overacting, artificial vocal reactions and irrelevant effects. "
          "Keep all spoken words identical across production JSON narration, voiceover.txt and voiceover-expressive.txt, "
          "never put direction tags in spoken narration or captions, and ensure beforeWord indices and phrase-based animation anchors "
          "match the final spoken wording. "
          "\n\nVisual planning: specify original scene-specific visuals. Use referenceStyle as the authoritative art direction and "
          "apply the selected style across niches. Isometric Tech Grid favors detailed perspective objects, mechanisms, "
          "architecture and network flows. Warm Editorial Motion favors expressive pacing, dynamic typography and relatable "
          "illustrations. Warm Editorial Explainer favors clear diagrams, comparisons and step-by-step demonstrations. Use "
          "the brand profile as identity. Asset entries are local relative filenames, never URLs. "
          "Choose the best visual medium for each explanatory scene. For subjects that benefit from depth, perspective or moving parts, "
          "supply storyboard.model with named box, sphere, cylinder, torus, loft or tube parts and appropriate materials and finishes. "
          "Build recognizable objects with accurate proportions, layered structures and relevant details. Use supported "
          "metalness, roughness and matte, brushed or rubber textures where useful. Use existing lighting to clarify "
          "depth. Keep primitive counts proportionate and promise only the fidelity the renderer and real assets support. "
          "Set a perspective eye and look target. Use groundShadows false for suspended diagrams with no meaningful ground. "
          "Positions use scene units, rotations use degrees. "
          "A loft follows local X and uses sections [x, centerY, centerZ, radiusY, radiusZ] in increasing X order with positive radii; "
          "use size [1,1,1] for a loft. Tube points are 3D coordinates and size[0] is the tube diameter. For torus specify "
          "outer X diameter and tube Z diameter. For other primitives use full dimensions. Reuse relevant project or licensed library media. "
          "Model events use exact spoken anchors. Reveal and hide take value [0,0,0]; move, rotate and camera take destination vectors. Use low emission for status LEDs and packets. Use optional model.labels with text and 3D positions for essential identifiers, and model.entrance for cut, fade or slide. "
          "Use target camera with camera for a straight move or camera-orbit for an arc around the fixed look point. Use assembly to move or rotate the whole model. "
          "Use supported moves and rotations for cutaways, exploded views and changes of state. Use deliberate camera "
          "push-ins or perspective changes to reveal relationships. Vary staging and avoid repeated entrances. "
          "For detailed 2D or original interface scenes supply `storyboard.artwork` with named layers of shapes that actually depict "
          "the objects, interfaces and relationships spoken about. Draw on a 944 by 760 canvas inside the safe zone. "
          "Use accurate interface diagrams, isometric diagrams, scientific cross-sections or process flows as suitable. "
          "Shapes are rect, ellipse, path, text and image. Use box [x,y,width,height] for rect, ellipse and image. "
          "For text use box [x,y,0,0] (top-left), text and size. For paths supply SVG path data d. "
          "Use fill and stroke as hex colors or ink, accent, surface, border or none. Images must reference a real "
          "local raster file listed in the scene's assets. Layer order is back to front. "
          "In artwork.events target a layer ID and quote the exact spoken anchor, with occurrence for repeated phrases. "
          "Use a few meaningful reveals, highlights, exits, typed text, drawn connections or transforms. "
          "A transform's value is the destination layer ID. The destination stays hidden until the swap. "
          "Use highlight for object emphasis or focus for a bounded push-in. Use move with [dx,dy] in illustration pixels, scale with [sx,sy] about the layer center, pulse for a brief scale accent, and sequence with the destination layer ID to send a token to an already visible object. "
          "Use artwork.entrance cut for immediate framing, fade for a gentle reveal, or slide for an explanatory scene opener. Preserve spatial continuity when cutting between views of the same system. Use an explicit scene-relative at only for an unspoken beat. "
          "Compose original diagrams, illustrations or interface details. Draw the mechanism and its state changes "
          "with purposeful labels. Omit decorative metrics, filler cards and unrelated symbols. "
          "If a scene needs true 3D or missing assets and cannot be illustrated faithfully, set storyboard.limitation "
          "to the specific requirement instead of artwork. This stops automatic substitution and permits supplied media "
          "or a reviewed custom scene. "
          "For a visual-only revision of a recorded plan, change storyboard.artwork or storyboard.model only to retain approved timing. "
          "Mobile composition and typography: make the main object large enough to understand immediately, with balanced "
          "top and bottom spacing and useful negative space. Keep headings, important parts and supporting labels clear "
          "of each other, word captions and Instagram interface safe zones. Prefer headings of three to seven words, "
          "strong contrast, accurate technical spelling, and minimal supporting labels. Use the existing word caption "
          "style and configured active-word highlight. "
          "Do not fabricate product screenshots, medical claims, metrics or unsupported facts. "
          "\n\nInstagram post: fill instagram.cover with a short compelling cover title, instagram.post with an engaging "
          "accurate caption, instagram.cta with a natural topic-relevant call to action that respects brand settings, and "
          "instagram.hashtags with a small set of relevant niche-specific tags. Avoid spam, exaggerated promises and "
          "generic engagement bait. "
          "\n\nMandatory JSON self-audit: silently validate the exact brief and schema, language and pronunciation, hook and "
          "payoff, factual claims, spoken duration, contiguous scene cuts, scene-relative motion and audio cues, exact "
          "anchors and supported actions targeting real layers or parts, model geometry and materials, and mobile framing. "
          "Verify the selected brand, the supplied normal, ad or sponsor badge mode and title, the configured creator outro, "
          "local asset paths, and one CTA where enabled. Remove unsupported fields and fix any issues before returning the "
          "JSON. Use normal badge mode when no promotion is configured. Preserve approved sponsorship details. "
          f"Output format & downloadable JSON file: return one valid JSON object matching the schema below with zero "
          f"extra commentary inside the JSON, AND also generate and provide that exact JSON as a downloadable `.json` file "
          f"(`{brief_id}.json`) when file download output is supported in your environment.\n\n"
          + json.dumps({"brief": config, "brand": brand, "referenceStyle": style,
                        **({"badge": badge} if badge else {}),
                        **({"sponsorship": sponsor_context} if sponsor_context else {}),
                        "voiceDirection": {"style": prefs["style"], "intensity": prefs["intensity"],
                                           "model": prefs["model"],
                                           "intents": INTENTS,
                                           "allowedTags": [tag for tags in CATALOG.values()
                                                           for tag in tags if allowed(tag, prefs)]},
                        "schema": load(SCHEMA)}, ensure_ascii=False, indent=2) + "\n")


def director_state(project):
    path = project / "voiceover-director.json"
    return load(path) if path.is_file() else {}


def save_director(project, plan, selected, edited=None):
    prefs = preferences(selected)
    if set(prefs["sceneOverrides"]) - {scene["id"] for scene in plan["scenes"]}:
        raise ValueError("A voice override refers to a scene outside the current plan.")
    state = {"preferences": prefs}
    if edited is not None:
        state["edited"] = validate_expressive(edited, clean_narration(plan), prefs)
    view = director_view(plan, state)
    save(project / "voiceover-director.json", state)
    write(project / "voiceover-expressive.txt", view["expressive"])


def import_plan(project, plan):
    from motion_choreography import validate_artwork, validate_model

    validate(plan, load(SCHEMA))
    brief = load(project / "brief.json")
    if any(plan[key] != brief[key] for key in CONFIG):
        raise ValueError("Plan must match the saved brief.")
    expected = 0
    scene_ids, audio_ids = {"production", "stage"}, set()
    for scene in plan["scenes"]:
        validate_scene(scene)
        validate_artwork(scene)
        validate_model(scene)
        sid = scene["id"]
        if sid in scene_ids or not math.isclose(scene["start"], expected, abs_tol=1e-6):
            raise ValueError(f"{sid}: duplicate ID or discontinuous scene timeline.")
        scene_ids.add(sid)
        expected = scene["end"]
        length = expected - scene["start"]
        if length <= 0 or not TOKEN.search(scene["narration"]):
            raise ValueError(f"{sid}: expected positive duration and spoken words.")
        motion_ids = set()
        for motion in scene["animation"]:
            if motion["id"] in motion_ids or not 0 <= motion["start"] < motion["end"] <= length + 1e-6:
                raise ValueError(f"{sid}: invalid animation interval or duplicate ID.")
            motion_ids.add(motion["id"])
        for cue in scene["audio_cues"]:
            if cue["id"] in audio_ids or not 0 <= cue["at"] < length:
                raise ValueError(f"{sid}: invalid audio cue position or duplicate ID.")
            audio_ids.add(cue["id"])
    if not math.isclose(expected, plan["duration"], abs_tol=1e-6):
        raise ValueError("Final estimated scene must end at the brief duration.")
    extract(project, plan)
    save(project / "plan.json", plan)


def extract(project, plan):
    view = director_view(plan, director_state(project))
    write(project / "voiceover.txt", clean_narration(plan))
    write(project / "voiceover-expressive.txt", view["expressive"])
    save(project / "scenes.json", plan["scenes"])
    save(project / "instagram.json", plan["instagram"])


def local_audio(source, dest):
    if source.suffix.lower() not in {".wav", ".mp3"} or not source.is_file():
        raise ValueError("Select an existing local .wav or .mp3 file.")
    run("ffmpeg", "-nostdin", "-v", "error", "-y", "-protocol_whitelist", "file,pipe",
        "-i", source.resolve(), "-map", "0:a:0", "-c:a", "pcm_s16le", "-ar", "48000", dest)


def draft_voice(project, plan, dest):
    if plan["language"] != "en":
        raise ValueError("Offline Flite draft voice supports English. Supply MP3/WAV for other languages.")
    extract(project, plan)
    run("ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
        "flite=textfile=voiceover.txt:voice=slt", "-ar", "48000", dest, cwd=project)


def alignment_stage(stage, pct):
    print("@vrs-align " + json.dumps({"stage": stage, "pct": pct}), flush=True)


def retry_recognition(observed, retry, cache_name, duration):
    """Splice one cached local ASR retry using only whole measured word spans."""
    if retry["recognitionCache"] != cache_name:
        raise ValueError("The segment retry belongs to another recognition cache.")
    start, end = retry["replace"]
    clip_start, clip_end = retry["clip"]
    if not 0 <= clip_start <= start < end <= clip_end <= duration:
        raise ValueError("Segment retry lies outside the current audio.")
    if any(not valid_span([word], duration) for word in retry["words"]):
        raise ValueError("Segment retry contains an invalid measured span.")
    before = [word for word in observed if word["start"] < start]
    after = [word for word in observed if word["start"] >= start and word["end"] > end]
    left = max(start, before[-1]["end"] if before else start)
    right = min(end, after[0]["start"] if after else end)
    middle = [word for word in retry["words"] if left <= word["start"] < word["end"] <= right]
    if any(a["end"] > b["start"] for a, b in zip(middle, middle[1:])):
        raise ValueError("Segment retry overlaps its own word spans.")
    return before + middle + after if middle else observed


def witness_windows(report, duration):
    """Limit independent ASR to three short recognition gaps and 12 seconds total."""
    windows = set()
    for row in report["unmatched"]:
        span = row.get("reviewWindow") or row.get("candidate")
        if span and 0 < span["end"] - span["start"] <= 3:
            windows.add((max(0, span["start"] - .30), min(duration, span["end"] + .30)))
    merged = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1] and end - merged[-1][0] <= 4:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        elif not merged or start > merged[-1][1]:
            merged.append((start, end))
    return merged[:3]


def whisper_model_name(language, models_dir=MODELS):
    if language == "en":
        return "tiny.en"
    return "tiny" if (models_dir / "models--Systran--faster-whisper-tiny/refs/main").is_file() else "small"


def load_whisper_model(model_name, models_dir=MODELS):
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    os.environ["HF_HUB_OFFLINE"] = "1"
    from faster_whisper import WhisperModel

    return WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=2,
                        download_root=str(models_dir), local_files_only=True)


def run_whisper(model, source, language, *, offset=0.0, **options):
    segments, info = model.transcribe(source if not isinstance(source, Path) else str(source),
                                      language=language, word_timestamps=True, **options)
    words = []
    for segment in segments:
        for word in segment.words:
            text = word.word.strip()
            start, end = round(float(word.start + offset), 6), round(float(word.end + offset), 6)
            if normalized(text) and math.isfinite(start) and math.isfinite(end) and 0 <= start < end:
                if words and start < words[-1]["end"]:
                    continue
                words.append({"text": text, "start": start, "end": end,
                              "probability": float(word.probability)})
    metadata = {"configuredLanguage": language or "auto", "language": info.language,
                "languageProbability": float(info.language_probability)}
    return words, metadata


def unresolved_gaps(scenes, words, duration):
    """Locate contiguous script gaps strictly between reliable measured words."""
    script = [(scene["id"], token) for scene in scenes for token in scene["narration"].split()
              if normalized(token)]
    anchors = {next((w for w in words if w["scene"] == s["id"]), None)["scriptIndex"]
               for s in scenes if next((w for w in words if w["scene"] == s["id"]), None)
               and next((w for w in words if w["scene"] == s["id"]), None)["match"] == "approximate"}
    reliable = {}
    for w in words:
        if w["match"] != "approximate" and w.get("probability", 0) >= 0.25 and w["scriptIndex"] not in anchors:
            for k in range(w["scriptIndex"], w["scriptIndex"] + w["scriptWords"]):
                reliable[k] = w
    gaps = []
    i = 0
    while i < len(script):
        if i in reliable:
            i += 1
            continue
        j = i
        while j < len(script) and j not in reliable:
            j += 1
        t0 = reliable[i - 1]["end"] if i > 0 and (i - 1) in reliable else 0.0
        t1 = reliable[j]["start"] if j < len(script) and j in reliable else duration
        if t1 - t0 > 0.12:
            has_en = any(re.fullmatch(r"[a-z]{3,}", normalized(script[x][1])) for x in range(i, j))
            gaps.append((round(t0, 3), round(t1, 3), has_en))
        i = j
    return gaps


def recovery_windows(scenes, words, duration, *, max_chunk=6.4, max_total=32.0):
    """Pack unresolved intervals with short acoustic padding into bounded <=6.4s recovery windows."""
    gaps = unresolved_gaps(scenes, words, duration)
    merged = []
    for t0, t1, has_en in gaps:
        c0, c1 = max(0.0, t0 - 0.6), min(duration, t1 + 0.6)
        if merged and max(c1, merged[-1][1]) - merged[-1][0] <= max_chunk and c0 <= merged[-1][1] + 0.8:
            merged[-1] = [merged[-1][0], max(c1, merged[-1][1]), merged[-1][2] or has_en]
        else:
            merged.append([c0, c1, has_en])
    windows = []
    total = 0.0
    for c0, c1, has_en in merged:
        pos = c0
        while pos < c1 and total < max_total:
            end_c = min(c1, pos + max_chunk)
            if c1 - end_c < 1.3:
                end_c = c1
            span = round(end_c - pos, 3)
            if span >= 0.4 and total + span <= max_total + 1.5:
                windows.append((round(pos, 2), round(end_c, 2), bool(has_en)))
                total += span
            if end_c >= c1:
                break
            pos = end_c - 0.9
    return windows


def merge_recovered_spans(base_tokens, base_words, recovered_tokens, *, is_witness=False):
    """Merge recovered gap tokens non-destructively while keeping all reliable base intervals untouched."""
    protected = []
    kept = []
    for w in base_words:
        is_reliable_or_opening = w["match"] != "approximate" and w.get("probability", 0) >= 0.25
        if not is_reliable_or_opening:
            # Also protect existing witness scene-opening anchors if witness pass holds them
            if not (is_witness and not w.get("asrIndices")):
                continue
        if is_witness:
            if not w.get("asrIndices"):
                protected.append((round(w["start"], 4), round(w["end"], 4), normalized(w["text"])))
        else:
            for idx in w.get("asrIndices", []):
                if 0 <= idx < len(base_tokens):
                    tok = base_tokens[idx]
                    protected.append((round(tok["start"], 4), round(tok["end"], 4), normalized(tok["text"])))
                    kept.append(tok)
    if is_witness:
        kept = [tok for tok in base_tokens
                if any(min(tok["end"], pe) > max(tok["start"], ps) + 1e-4 for ps, pe, *_ in protected)
                or tok.get("probability", 0) >= 0.70]
    # Preserve exact-English witness spans while allowing recovered Devanagari Hindi words to replace Hinglish witness substitutes.
    blocked_alt = [(round(w["start"], 4), round(w["end"], 4)) for w in base_words
                   if not is_witness and not w.get("asrIndices") and w["match"] != "approximate"
                   and w.get("probability", 0) >= 0.25
                   and normalized(w["text"]) == normalized(w.get("heard", ""))]
    add = []
    for tok in recovered_tokens:
        ts, te = round(tok["start"], 4), round(tok["end"], 4)
        tnorm = normalized(tok["text"])
        for ps, pe, pnorm in protected:
            if te <= ps + 1e-4 or ts >= pe - 1e-4:
                continue
            # Never split an already-protected matching token into a duplicate trailing fragment.
            if pnorm and tnorm == pnorm:
                te = ts
                break
            # Trim small neighbor boundary overlaps without altering any verified protected span.
            if ts < ps <= te and (ps - ts) >= 0.04:
                te = min(te, ps)
            elif ts <= pe < te and (te - pe) >= 0.04:
                ts = max(ts, pe)
            else:
                te = ts
                break
        ts, te = round(ts, 4), round(te, 4)
        if te - ts < 0.04:
            continue
        if any(min(te, pe) > max(ts, ps) + 1e-4 for ps, pe, *_ in protected):
            continue
        if any(min(te, ae) > max(ts, as_) + 1e-4 for as_, ae in blocked_alt):
            continue
        add.append({**tok, "start": ts, "end": te})
    chosen = []
    for tok in sorted(kept, key=lambda x: x["start"]) + sorted(add, key=lambda x: (-x["probability"], x["start"])):
        ts, te = tok["start"], tok["end"]
        if tok in kept or not any(min(te, c["end"]) > max(ts, c["start"]) + 0.02 for c in chosen):
            chosen.append(tok)
    if is_witness:
        for tok in sorted(base_tokens, key=lambda x: (-x["probability"], x["start"])):
            if not any(min(tok["end"], c["end"]) > max(tok["start"], c["start"]) + 1e-4 for c in chosen):
                chosen.append(tok)
    chosen.sort(key=lambda x: x["start"])
    out = []
    for tok in chosen:
        if not out or tok["start"] >= out[-1]["end"] - 1e-4:
            if out and tok["start"] < out[-1]["end"]:
                tok = {**tok, "start": out[-1]["end"]}
            if tok["start"] < tok["end"]:
                out.append(tok)
    return out


def align(project, plan, model_name=None, *, remote_words=None, remote_recognition=None,
          remote_alternate=None, remote_recovery=None):
    for scene in plan["scenes"]:
        validate_scene(scene)
    voice = project / "voice.wav"
    model_name = model_name or whisper_model_name(plan["language"], MODELS)
    key = digest(voice, plan["language"], model_name)
    transcript = project / f".whisper-{key}.json"
    report_path = project / "alignment-report.json"
    identity = {"timingKey": timing_key(voice, plan), "model": model_name,
                "recognitionCache": transcript.name}
    save(report_path, {**identity, "status": "running"})
    if remote_words is not None:
        save(transcript, remote_words)
    model = None

    def recognize(language, *, samples=None, offset=0, override_model=None, **options):
        nonlocal model
        if override_model and override_model != model_name:
            # Free primary model first on memory-constrained CPUs before loading fast witness model.
            model = None
            target = load_whisper_model(override_model, MODELS)
        else:
            if model is None:
                model = load_whisper_model(model_name, MODELS)
            target = model
        return run_whisper(target, voice if samples is None else samples, language, offset=offset, **options)

    t_align_start = time.perf_counter()
    primary_transcribe_s = 0.0
    witness_transcribe_s = 0.0
    info_path = project / f".whisper-{key}-recognition.json"
    if remote_recognition is not None:
        save(info_path, remote_recognition)
    if not transcript.is_file():
        alignment_stage("Transcribing local audio", 10)
        t_p0 = time.perf_counter()
        whisper_lang = None if plan["language"] == "hinglish" else {"en": "en", "hi": "hi"}.get(plan["language"])
        recognition, metadata = recognize(whisper_lang, beam_size=3, condition_on_previous_text=False)
        primary_transcribe_s = round(time.perf_counter() - t_p0, 3)
        save(transcript, recognition)
        save(info_path, metadata)
    if info_path.is_file():
        identity["recognition"] = load(info_path)
    alignment_stage("Matching cached recognition to script", 65)
    observed = load(transcript)
    duration = probe_duration(voice)
    words, report = match_script(plan["scenes"], observed, duration, plan["language"])
    retry_path = project / f".whisper-{key}-retry.json"
    if retry_path.is_file():
        retry = load(retry_path)
        candidate = retry_recognition(observed, retry, transcript.name, duration)
        candidate_words, candidate_report = match_script(plan["scenes"], candidate, duration, plan["language"])
        if (candidate_report["reliableMatches"] > report["reliableMatches"]
                and candidate_report["reliableCoverage"] > report["reliableCoverage"]
                and candidate_report["coverage"] >= report["coverage"]):
            observed, words, report = candidate, candidate_words, candidate_report
            report["segmentRetry"] = {"cache": retry_path.name, "clip": retry["clip"], "replace": retry["replace"],
                                      "rawCache": transcript.name, "method": retry.get("method")}
    has_tiny_en = (MODELS / "models--Systran--faster-whisper-tiny.en/refs/main").is_file()
    if (plan["language"] == "hinglish" and model_name != "tiny.en"
            and (remote_words is None or remote_alternate is not None)
            and duration <= 180 and (report["reliableCoverage"] < QUALITY_FLOOR or report["unmatched"])
            and any(DEVANAGARI.search(w["text"]) for w in observed)):
        use_tiny_en = has_tiny_en or remote_alternate is not None
        windows = [(0, duration)] if (use_tiny_en or duration <= 12) else witness_windows(report, duration)
        suffix = ("tiny-en-witness-v2" if use_tiny_en
                  else "english-witness-v1" if duration <= 12
                  else f"english-windows-v1-{digest(windows)}")
        alternate_path = project / f".whisper-{key}-{suffix}.json"
        try:
            if remote_alternate is not None:
                save(alternate_path, {
                    "words": remote_alternate["words"],
                    "recognition": remote_alternate.get("recognition", {"witnessModel": "tiny.en"}),
                    "windows": windows,
                    "prompted": False,
                })
            elif not alternate_path.is_file():
                alignment_stage("Checking measured code-switch gaps with independent ASR", 72)
                t_w0 = time.perf_counter()
                options = {"beam_size": 3, "condition_on_previous_text": False}
                if use_tiny_en:
                    alternate, metadata = recognize("en", override_model="tiny.en", **options)
                    metadata["witnessModel"] = "tiny.en"
                elif duration <= 12:
                    alternate, metadata = recognize("en", **options)
                else:
                    alternate, metadata = [], {"configuredLanguage": "en"}
                    if windows:
                        from faster_whisper.audio import decode_audio
                        sample_rate = 16000
                        audio = decode_audio(str(voice), sampling_rate=sample_rate)
                        for start, end in windows:
                            left, right = round(start * sample_rate), round(end * sample_rate)
                            if right > left:
                                measured, _ = recognize("en", samples=audio[left:right],
                                                        offset=left / sample_rate, **options)
                                alternate.extend(w for w in measured if left / sample_rate <= w["start"]
                                                 < w["end"] <= right / sample_rate)
                witness_transcribe_s = round(time.perf_counter() - t_w0, 3)
                save(alternate_path, {"words": alternate, "recognition": metadata,
                                      "windows": windows, "prompted": False})
            evidence = load(alternate_path)
            revised, checked = match_script(plan["scenes"], observed, duration, plan["language"],
                                            alternate=evidence["words"] if evidence.get("prompted") is False else [])
        except (ImportError, OSError, RuntimeError, ValueError, KeyError):
            report["independentRecognition"] = {"status": "unavailable"}
        else:
            if (checked["corroboratedMatches"] > report.get("corroboratedMatches", 0)
                    or checked["reliableCoverage"] > report["reliableCoverage"]
                    or checked["coverage"] > report["coverage"]):
                checked.update({k: report[k] for k in ("segmentRetry",) if k in report})
                words, report = revised, checked
            report["independentRecognition"] = {"cache": alternate_path.name,
                                                **evidence.get("recognition", {}),
                                                "windows": evidence.get("windows", windows),
                                                "prompted": evidence.get("prompted")}
    # Conditional targeted recovery of unresolved audio gaps when caption gate or openings stay incomplete.
    alt_words = (load(project / report["independentRecognition"]["cache"]).get("words", [])
                 if isinstance(report.get("independentRecognition"), dict)
                 and report["independentRecognition"].get("cache")
                 and (project / report["independentRecognition"]["cache"]).is_file()
                 else (remote_alternate.get("words", []) if isinstance(remote_alternate, dict) else []))
    anchors_pre = [next((w for w in words if w["scene"] == s["id"]), None) for s in plan["scenes"]]
    needs_recovery = (
        report["reliableCoverage"] < QUALITY_FLOOR
        or bool(report["unmatched"])
        or any(a is None or a["match"] == "approximate" for a in anchors_pre)
    )
    recovery_path = project / f".whisper-{key}-recovery-v1.json"
    if needs_recovery and duration <= 180 and (
        remote_words is None or remote_recovery is not None or recovery_path.is_file()
    ):
        try:
            if remote_recovery is not None:
                save(recovery_path, remote_recovery)
            elif not recovery_path.is_file() and remote_words is None:
                rec_windows = recovery_windows(plan["scenes"], words, duration)
                rec_prim, rec_alt = [], []
                if rec_windows:
                    alignment_stage("Recovering unresolved narration spans", 82)
                    t_r0 = time.perf_counter()
                    from faster_whisper.audio import decode_audio
                    sample_rate = 16000
                    audio = decode_audio(str(voice), sampling_rate=sample_rate)
                    rec_lang = None if plan["language"] == "hinglish" else {"en": "en", "hi": "hi"}.get(plan["language"])
                    for start, end, has_en in rec_windows:
                        left, right = round(start * sample_rate), round(end * sample_rate)
                        if right <= left:
                            continue
                        rw, _ = recognize(rec_lang, samples=audio[left:right], offset=left / sample_rate,
                                          beam_size=3, condition_on_previous_text=False)
                        rec_prim.extend(w for w in rw if left / sample_rate <= w["start"] < w["end"] <= right / sample_rate)
                        if has_en and plan["language"] == "hinglish" and (end - start) <= 6.0:
                            rwe, _ = recognize("en", samples=audio[left:right], offset=left / sample_rate,
                                               beam_size=3, condition_on_previous_text=False)
                            rec_alt.extend(w for w in rwe if left / sample_rate <= w["start"] < w["end"] <= right / sample_rate)
                    witness_transcribe_s = round(witness_transcribe_s + time.perf_counter() - t_r0, 3)
                save(recovery_path, {"windows": rec_windows, "words": rec_prim, "alternate": rec_alt, "model": model_name})
            rec_data = load(recovery_path)
            merged_obs = merge_recovered_spans(observed, words, rec_data.get("words", []), is_witness=False)
            merged_alt = merge_recovered_spans(alt_words, words, rec_data.get("alternate", []), is_witness=True)
            rec_words, rec_report = match_script(plan["scenes"], merged_obs, duration, plan["language"],
                                                 alternate=merged_alt if merged_alt else None)
            rec_anchors = [next((w for w in rec_words if w["scene"] == s["id"]), None) for s in plan["scenes"]]
            old_open_valid = sum(
                1 for a, s in zip(anchors_pre, plan["scenes"])
                if a and a["text"].split()[0] == next(t for t in s["narration"].split() if normalized(t))
            )
            new_open_valid = sum(
                1 for a, s in zip(rec_anchors, plan["scenes"])
                if a and a["text"].split()[0] == next(t for t in s["narration"].split() if normalized(t))
            )
            old_open_ok = sum(1 for a in anchors_pre if a and a["match"] != "approximate")
            new_open_ok = sum(1 for a in rec_anchors if a and a["match"] != "approximate")
            if (new_open_valid >= old_open_valid
                    and rec_report["reliableMatches"] >= report["reliableMatches"]
                    and (rec_report["reliableMatches"] > report["reliableMatches"]
                         or rec_report["coverage"] > report["coverage"]
                         or new_open_ok > old_open_ok)):
                rec_report.update({k: report[k] for k in ("segmentRetry", "independentRecognition") if k in report})
                rec_report["targetedRecovery"] = {
                    "cache": recovery_path.name,
                    "model": rec_data.get("model"),
                    "windows": rec_data.get("windows", []),
                    "recoveredPrimaryTokens": len(rec_data.get("words", [])),
                    "recoveredWitnessTokens": len(rec_data.get("alternate", [])),
                    **({"apexWordCount": len(rec_data["apexWords"])} if isinstance(rec_data.get("apexWords"), list) else {}),
                    **({"apexSeconds": rec_data["apexSeconds"]} if rec_data.get("apexSeconds") is not None else {}),
                    **({"forcedWordCount": len(rec_data["forcedWords"])} if isinstance(rec_data.get("forcedWords"), list) else {}),
                }
                words, report = rec_words, rec_report
        except (ImportError, OSError, RuntimeError, ValueError, KeyError):
            pass
    expressive = director_view(plan, director_state(project))["expressive"]
    report["spokenDirections"] = possible_spoken_tags(
        expressive, observed, words, plan["language"] in {"hi", "hinglish"}, duration)
    total_align_s = round(time.perf_counter() - t_align_start, 3)
    peak_rss_mib = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    report["performance"] = {
        "primaryTranscribeSeconds": primary_transcribe_s,
        "witnessTranscribeSeconds": witness_transcribe_s,
        "alignmentTotalSeconds": total_align_s,
        "peakRssMiB": peak_rss_mib,
        "usedPrimaryCache": primary_transcribe_s == 0.0,
    }
    if len(report["unmatched"]) > 12 or report["reliableCoverage"] < QUALITY_FLOOR:
        report["diagnosticGuidance"] = (
            f"{len(report['unmatched'])} words still need measured timing ({report['reliableCoverage']:.1%} strong evidence vs "
            f"{QUALITY_FLOOR:.0%} gate). Check whether voiceover pronunciation matches the script phrasing (especially mixed "
            "English/Hindi technical words) or review each flagged interval below before retrying alignment."
        )
    elif report["unmatched"]:
        report["diagnosticGuidance"] = (
            f"Gate passed ({report['reliableCoverage']:.1%}). Review the {len(report['unmatched'])} remaining unmatched words "
            "below using Play isolated candidate to accept exact spoken spans before Premium export."
        )
    report.update(identity, voiceDuration=duration)
    anchors = [next((w for w in words if w["scene"] == scene["id"]), None) for scene in plan["scenes"]]
    report["sceneAnchors"] = [
        {"scene": scene["id"], "opening": next(token for token in scene["narration"].split() if normalized(token)),
         **({"expected": word["text"], "heard": word["heard"], "start": word["start"], "end": word["end"],
             "match": word["match"], "confidence": word["confidence"]} if word else {})}
        for scene, word in zip(plan["scenes"], anchors)
    ]
    if report["reliableCoverage"] < QUALITY_FLOOR:
        failure = (f"Strong script coverage {report['reliableCoverage']:.1%} is below {QUALITY_FLOOR:.0%}. "
                   "Review the expected and recognized words, then retry using the same audio.")
    elif None in anchors or any(not any(w["scene"] == scene["id"] and w["match"] != "approximate"
                                       and w["probability"] >= MIN_ANCHOR_CONFIDENCE for w in words)
                                for scene in plan["scenes"]):
        failure = "At least one scene lacks a reliable measured speech anchor. Review the script and recognition."
    elif any(row["expected"].split()[0] != row["opening"] for row in report["sceneAnchors"]):
        failure = "No accepted timing for a scene opening. Review its source audio before continuing."
    else:
        failure = None
    if failure:
        save(report_path, {**report, "status":"failed", "failureReason":failure})
        raise ValueError(failure)
    alignment_stage("Saving measured scene and caption timing", 90)
    timeline = measured_timeline(plan, duration, words, identity["timingKey"])
    save(project / "timeline.json", timeline)
    save(report_path, {**report, "status":"passed"})
    return timeline


def measured_timeline(plan, duration, words, key):
    openings = {}
    for word in words:
        openings.setdefault(word["scene"], word["start"])
    starts = [openings[scene["id"]] for scene in plan["scenes"]]
    starts[0] = 0
    total = duration + plan["outro"]
    scenes = []
    for scene, start, end in zip(plan["scenes"], starts, starts[1:] + [total]):
        # An explicit outro holds the final scene without stretching its motion.
        active_end = min(end, duration)
        scale = (active_end - start) / (scene["end"] - scene["start"])
        scenes.append({"id": scene["id"], "start": start, "end": end,
                       "animation": [{**a, "start": a["start"] * scale, "end": a["end"] * scale}
                                     for a in scene["animation"]],
                       "audio_cues": [{**c, "at": start + c["at"] * scale}
                                      for c in scene["audio_cues"]]})
    return {"version": 1, "key": key, "voiceDuration": duration,
            "duration": total, "scenes": scenes, "words": words}
