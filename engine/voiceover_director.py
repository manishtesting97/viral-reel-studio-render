"""Local ElevenLabs script directions. Alignment always reads clean scene narration."""
import re
import unicodedata

from alignment import lexical, valid_span

CATALOG = {
    "Emotion": ("weak", "angry", "bored", "menacing", "playful", "tired", "excited"),
    "Vocalizations": ("clears throat", "coughing", "chuckling", "sighing", "sniffling", "yawning",
                      "laughing", "sharp exhale", "scoffing", "humming", "panting", "gasping"),
    "Delivery": ("whispering", "singing", "slowly", "softly", "pause", "voice breaking"),
    "Character": ("sleepy drowsy voice", "witch voice", "drunken voice"),
    "Sound effects": ("keyboard typing", "echoing", "knocking on door", "bell ringing", "phone ringing",
                      "dog barking", "crowd cheering", "applause", "rain falling", "drum roll", "heartbeat",
                      "engine starting"),
}
INTENTS = {
    "cheerful": "Smiling and cheerful", "warm": "Friendly and warm", "gentle": "Soft and gentle",
    "polite": "Polite and respectful", "assertive": "Firm and assertive", "confident": "Confident",
    "curious": "Curious", "surprised": "Surprised", "excited": "Excited", "serious": "Serious",
    "playful": "Playful", "suspenseful": "Suspenseful", "calm": "Calm", "deliberate": "Slow and deliberate",
    "punchy": "Fast and punchy", "pause": "Natural pause", "chuckle": "Small chuckle",
    "sigh": "Quiet sigh", "gasp": "Surprised gasp", "aggressive": "Aggressive",
}
# Conservative translations to documented Eleven v3/v4 audio-tag examples.
INTENT_TAGS = {
    "cheerful": "happy", "warm": "content", "gentle": "softly", "polite": "thoughtful",
    "assertive": "powerful", "confident": "proud", "curious": "curious", "surprised": "surprised",
    "excited": "excited", "serious": "thoughtful", "playful": "playful", "suspenseful": "whispers",
    "calm": "peaceful", "deliberate": "slowly", "punchy": "snappy", "pause": "short pause",
    "chuckle": "chuckles", "sigh": "sighs", "gasp": "gasps", "aggressive": "aggressive",
}
PAUSE_TAGS = {"pause", "short pause", "long pause"}
REACTION_TAGS = {"chuckles", "sighs", "gasps", "laughs"}
DOCUMENTED_ADDITIONS = {
    "Emotion": ("happy", "content", "thoughtful", "powerful", "proud", "curious", "surprised", "peaceful", "aggressive"),
    "Delivery": ("whispers", "snappy", "short pause", "long pause"),
    "Vocalizations": tuple(sorted(REACTION_TAGS)),
}
for group, tags in DOCUMENTED_ADDITIONS.items():
    CATALOG[group] += tags
CATEGORY = {tag: group for group, tags in CATALOG.items() for tag in tags}
DOCUMENTED_MODELS = {"eleven_v3", "eleven_v4", "eleven_v4_turbo"}
NO_AUDIO_TAG_MODELS = {"eleven_multilingual_v2", "eleven_flash_v2_5", "eleven_turbo_v2_5"}
DOCUMENTED_TAGS = set(INTENT_TAGS.values()) | PAUSE_TAGS | REACTION_TAGS | {
    "angry", "laughing", "clears throat", "tired", "bored", "dog barking",
}
HEARD_VARIANTS = {"pause": ("पॉज़", "पॉज", "पौज़", "पौज")}
STYLE_INTENTS = {"Warm": "warm", "Confident": "confident", "Calm": "calm", "Punchy": "punchy"}
STYLES = ("Dynamic", "Natural", "Conversational", "Energetic", "Cinematic", "Funny", "Emotional",
          "Professional", "Suspense", *STYLE_INTENTS)
INTENSITIES = {"Subtle": 1, "Balanced": 2, "Dramatic": 3}
TAG = re.compile(r"\[([^\[\]\r\n]+)\]")
DEFAULTS = {
    "mode": "clean", "style": "Dynamic", "intensity": "Balanced", "model": "eleven_v4",
    "automatic": True, "pauses": True, "reactions": True, "effects": False,
    "enabledTags": list(CATEGORY), "sceneOverrides": {}, "customTags": {},
}


def preferences(value):
    if not isinstance(value, dict):
        raise ValueError("Invalid voiceover preferences.")
    result = {**DEFAULTS, **value}
    if (set(result) != set(DEFAULTS) or result["mode"] not in {"clean","expressive"}
            or result["style"] not in STYLES or result["intensity"] not in INTENSITIES
            or not isinstance(result["model"],str) or len(result["model"]) > 80
            or any(type(result[key]) is not bool for key in ("automatic","pauses","reactions","effects"))):
        raise ValueError("Invalid voiceover preferences.")
    tags = result["enabledTags"]
    if not isinstance(tags,list) or any(not isinstance(tag,str) or tag not in CATEGORY for tag in tags):
        raise ValueError("The model tag list contains an unknown tag.")
    overrides, custom = result["sceneOverrides"], result["customTags"]
    if not isinstance(overrides, dict) or any(not isinstance(intent, str) or intent not in INTENTS
                                              for intent in overrides.values()):
        raise ValueError("Unknown scene delivery intent.")
    if not isinstance(custom, dict) or any(
        not isinstance(mapping, dict) or any(
            intent not in INTENTS or not isinstance(tag, str)
            or not re.fullmatch(r"[a-z][a-z0-9 -]{0,47}", tag) or tag != " ".join(tag.split())
            or tag in CATEGORY or tag in {value for key, value in mapping.items() if key != intent and isinstance(value, str)}
            for intent, tag in mapping.items()
        ) for mapping in custom.values()
    ):
        raise ValueError("Custom model tags must be unique, lowercase bracket-free directions for known intents.")
    if "enabledTags" in value and "customTags" not in value:
        tags = tags + [tag for added in DOCUMENTED_ADDITIONS.values() for tag in added if tag not in tags]
    return {**result, "enabledTags": list(dict.fromkeys(tags))}


def clean_narration(plan):
    return "\n\n".join(" ".join(scene["narration"].split()) for scene in plan["scenes"]) + "\n"


def spoken_tokens(text):
    text = unicodedata.normalize("NFKC", text).casefold()
    text = re.sub(r"(?<=\w)['’](?=\w)", "", text)
    return "".join(char if unicodedata.category(char)[0] in "LNM" else " " for char in text).split()


def allowed(tag, prefs):
    model = prefs["model"]
    if model in NO_AUDIO_TAG_MODELS:
        return False
    custom_intent = next((intent for intent, value in prefs["customTags"].get(model, {}).items()
                          if value == tag), None)
    if custom_intent is not None:
        return ((custom_intent != "pause" or prefs["pauses"])
                and (INTENT_TAGS[custom_intent] not in REACTION_TAGS or prefs["reactions"]))
    return (tag in prefs["enabledTags"]
            and (not model or model in DOCUMENTED_MODELS and tag in DOCUMENTED_TAGS)
            and (tag not in PAUSE_TAGS or prefs["pauses"])
            and (CATEGORY[tag] != "Vocalizations" or prefs["reactions"])
            and (CATEGORY[tag] != "Sound effects" or prefs["effects"]))


def intent_tag(intent, prefs):
    return prefs["customTags"].get(prefs["model"], {}).get(intent, INTENT_TAGS[intent])


def expressive_tags(text):
    return [" ".join(match[1].casefold().split()) for match in TAG.finditer(text)]


def validate_expressive(text, clean, prefs):
    if not isinstance(text,str) or len(text) > 80_000:
        raise ValueError("Expressive script is too large or invalid.")
    plain = TAG.sub(" ", text)
    if "[" in plain or "]" in plain:
        raise ValueError("Use complete, non-nested [direction] tags.")
    for tag in expressive_tags(text):
        if tag not in CATEGORY and tag not in prefs["customTags"].get(prefs["model"], {}).values():
            raise ValueError(f"Unknown ElevenLabs tag: [{tag}].")
        if not allowed(tag,prefs):
            raise ValueError(f"Tag [{tag}] is disabled for the selected model or preferences.")
    if spoken_tokens(plain) != spoken_tokens(clean):
        raise ValueError("Expressive script changes the spoken words. Restore the approved wording and edit only tags or punctuation.")
    return text.strip() + "\n"


def validate_scene(scene):
    narration = scene["narration"]
    if "[" in narration or "]" in narration:
        raise ValueError(f'{scene["id"]}: keep clean narration free of bracketed direction tags.')
    count = len(narration.split())
    cues = scene.get("voice_direction", {}).get("cues", [])
    seen = set()
    for cue in cues:
        position = cue["beforeWord"]
        tag, intent = cue.get("tag"), cue.get("intent")
        if (tag is None) == (intent is None) or (tag is not None and tag not in CATEGORY) or (
            intent is not None and intent not in INTENTS
        ):
            raise ValueError(f'{scene["id"]}: use exactly one known tag or delivery intent per cue.')
        if not 0 <= position <= count:
            raise ValueError(f'{scene["id"]}: voice direction lies outside the spoken words.')
        if (position,tag,intent) in seen:
            raise ValueError(f'{scene["id"]}: duplicate voice direction.')
        seen.add((position,tag,intent))


BEATS = (
    ("humor", r"\b(joke|punchline|comic relief|funny)\b"),
    ("tension", r"\b(suspense|cliffhanger|danger|horror)\b"),
    ("emotional", r"\b(grief|heartbreak|goodbye|loss|regret)\b"),
    ("caution", r"\b(warning|caution|be careful|risk|safety|approval|control|lekin|however)\b"),
    ("hook", r"\b(hook|teaser|intro|imagine|picture this|socho)\b"),
    ("peak", r"\b(reveal|payoff|breakthrough|launch|milestone|twist|celebration)\b"),
    ("close", r"\b(outro|cta|follow|subscribe|offer|sponsor)\b"),
    ("example", r"\b(example|demo|case study|walkthrough|step|jaise|misaal)\b"),
    ("sensitive", r"\b(sensitive|recovery|vulnerable|anxiety|rest|breathe)\b"),
    ("problem", r"\b(problem|struggle|fails|failure|stuck|issue|mushkil|dikkat)\b"),
    ("advice", r"\b(advice|recommend|reminder|consider|please|tip|koshish)\b"),
)

# Directions by delivery style and scene beat, with the minimum intensity.
DELIVERY = {
    "Natural": {"caution": ("slowly", 2), "emotional": ("softly", 2)},
    "Conversational": {"hook": ("softly", 1), "caution": ("slowly", 2), "humor": ("playful", 2),
                       "example": ("softly", 3)},
    "Energetic": {"hook": ("excited", 1), "peak": ("excited", 1), "close": ("excited", 2),
                  "caution": ("slowly", 2), "humor": ("playful", 2), "example": ("excited", 3)},
    "Cinematic": {"hook": ("slowly", 1), "peak": ("excited", 2), "tension": ("whispering", 2),
                  "caution": ("softly", 2), "emotional": ("softly", 1), "example": ("slowly", 3)},
    "Funny": {"humor": ("playful", 1), "hook": ("playful", 2)},
    "Emotional": {"emotional": ("softly", 1), "caution": ("softly", 2), "peak": ("excited", 2)},
    "Professional": {"caution": ("slowly", 1), "example": ("slowly", 2)},
    "Suspense": {"tension": ("whispering", 1), "hook": ("softly", 2), "peak": ("slowly", 2)},
}


def natural_pause(words):
    # Prefer a sentence break to a comma and give both sides room to breathe.
    spots = [(2 if word.endswith((".", "?", "!", "।")) else 1, n)
             for n, word in enumerate(words, 1)
             if 4 <= n <= len(words)-4 and word.endswith((".", "?", "!", "।", ",", ";"))]
    return max(spots)[1] if spots else None


def fallback(scene, index, plan, prefs, pause):
    metadata = " ".join((scene["id"], scene["heading"], scene.get("purpose", ""))).replace("-", " ").casefold()
    narration = scene["narration"].casefold()
    beat = next((name for name, pattern in BEATS if re.search(pattern, metadata)), None)
    if beat is None:
        beat = next((name for name, pattern in BEATS if re.search(pattern, narration)),
                    "hook" if index == 0 else "explain")
    style = prefs["style"]
    level = INTENSITIES[prefs["intensity"]]
    audience = f"{plan.get('niche', '')} {plan.get('audience', '')}".casefold()
    formal = bool(re.search(r"\b(professionals?|finance|medical|legal|training|executives?)\b", audience))
    if style == "Dynamic" or style in STYLE_INTENTS:
        intents = {"hook": "curious", "problem": "assertive", "caution": "serious",
                   "humor": "playful", "peak": "surprised", "tension": "suspenseful",
                   "emotional": "gentle", "sensitive": "calm", "advice": "polite",
                   "example": "warm", "explain": "warm", "close": "cheerful"}
        if formal:
            intents.update(example="polite", explain="confident", close="polite")
        if beat == "explain" and level == 1:
            return []
        main = intents[beat]
        if beat not in {"caution", "sensitive", "emotional", "tension"}:
            main = STYLE_INTENTS.get(style, main)
        cues = [(0, intent_tag(main, prefs))]
        if level >= 2:
            words = scene["narration"].split()
            reaction = {"humor": "chuckle", "emotional": "sigh", "tension": "gasp"}.get(beat)
            if reaction and not formal:
                cues.append((pause if pause is not None else len(words), intent_tag(reaction, prefs)))
            else:
                for position, word in enumerate(words):
                    if 3 <= position <= len(words)-3:
                        marker = word.strip(",.!?;:").casefold()
                        if marker in {"but", "lekin", "magar", "however", "par", "isliye", "therefore"}:
                            intent = "assertive" if beat in {"problem", "caution"} else "serious"
                            cues.append((position, intent_tag(intent, prefs)))
                            break
                if level == 3 and len(cues) == 1 and pause is not None:
                    secondary = "deliberate" if beat in {"caution", "advice", "problem"} else "confident"
                    cues.append((pause, intent_tag(secondary, prefs)))
        return cues
    cues = []
    direction = DELIVERY[style].get(beat)
    if direction and level >= direction[1]:
        # Place a caution emphasis on its practical takeaway when it follows a lead-in.
        tag = "whispers" if direction[0] == "whispering" and prefs["model"] else direction[0]
        cues.append((pause if beat == "caution" and pause is not None else 0, tag))
    reactions = {"humor": "chuckle", "emotional": "sigh", "tension": "gasp"}
    if (level >= 2 and beat in reactions and style in {"Conversational", "Energetic", "Cinematic",
                                                       "Funny", "Emotional", "Suspense"}
            and (not formal or beat == "humor")):
        # Let the beat land first. A short scene gets at most one opening cue.
        position = pause if pause is not None else len(scene["narration"].split())
        tag = (intent_tag(reactions[beat], prefs) if prefs["model"] else
               {"humor": "chuckling", "emotional": "sighing", "tension": "gasping"}[beat])
        cues.append((position, tag))
    return cues


def generate(plan, prefs):
    paragraphs, skipped = [], set()
    previous_opening = None
    for index,scene in enumerate(plan["scenes"]):
        words = scene["narration"].split()
        tags = {}
        authored = scene.get("voice_direction", {}).get("cues", [])
        pause = natural_pause(words)
        override = prefs["sceneOverrides"].get(scene["id"])
        if prefs["automatic"] or override:
            proposals = []
            if override:
                proposals.append((0, intent_tag(override, prefs)))
            elif prefs["automatic"]:
                for cue in authored:
                    tag = cue["tag"] if "tag" in cue else intent_tag(cue["intent"], prefs)
                    if allowed(tag, prefs):
                        proposals.append((cue["beforeWord"], tag))
                    else:
                        skipped.add(tag)
                if not any(tag not in PAUSE_TAGS for _, tag in proposals):
                    inferred = fallback(scene, index, plan, prefs, pause)
                    proposals.extend((position, tag) for position, tag in inferred
                                     if prefs["style"] != "Dynamic" or position != 0 or tag != previous_opening)
            used = set()
            for position, tag in proposals:
                if not allowed(tag,prefs):
                    if override:
                        skipped.add(tag)
                    continue
                if position not in tags and tag not in used and len(tags) < INTENSITIES[prefs["intensity"]]:
                    tags[position] = f"[{tag}]"
                    used.add(tag)
        pause_tag = "pause" if not prefs["model"] and prefs["style"] in DELIVERY else intent_tag("pause", prefs)
        if (pause is not None and pause not in tags
                and not used_pause(tags, pause_tag) and allowed(pause_tag, prefs)):
            tags[pause] = f"[{pause_tag}]"
        previous_opening = tags.get(0, "")[1:-1]
        parts = []
        for i,word in enumerate(words):
            if i in tags:
                parts.append(tags[i])
            parts.append(word)
        if len(words) in tags:
            parts.append(tags[len(words)])
        paragraphs.append(" ".join(parts))
    return "\n\n".join(paragraphs) + "\n", sorted(skipped)


def used_pause(tags, pause_tag):
    return any(value[1:-1] in PAUSE_TAGS or value == f"[{pause_tag}]" for value in tags.values())


def director_view(plan, state):
    prefs = preferences(state.get("preferences", {}))
    clean = clean_narration(plan)
    model = prefs["model"]
    warnings = []
    if model not in DOCUMENTED_MODELS:
        warnings.append("This model has no documented audio-tag profile here. Review compatibility before synthesis.")
    if prefs["customTags"].get(model):
        warnings.append("Custom tags are unverified for this model and may be read aloud.")
    edited = state.get("edited")
    if edited:
        try:
            return {"preferences": prefs, "expressive": validate_expressive(edited,clean,prefs),
                    "edited": True, "warnings": warnings}
        except ValueError as error:
            warnings.append(f"Prior edit is not applied: {error}")
    text, skipped = generate(plan,prefs)
    if skipped:
        warnings.append("Disabled scene tags skipped: "+", ".join(f"[{tag}]" for tag in skipped)+".")
    return {"preferences": prefs,"expressive":validate_expressive(text,clean,prefs),
            "edited":False,"warnings":warnings}


def possible_spoken_tags(text, observed, matches, multilingual, duration):
    """Report recognized directions only among unassigned tokens; never time them as captions."""
    assigned = {index for match in matches for index in match["asrIndices"]}
    found = []
    for tag in set(expressive_tags(text)):
        tokens = tag.split()
        for start in range(len(observed)-len(tokens)+1):
            end = start + len(tokens)
            if any(index in assigned for index in range(start,end)):
                continue
            phrase = observed[start:end]
            if valid_span(phrase, duration) and all(
                lexical(expected, w["text"], multilingual)
                or multilingual and w["text"].strip(".,!?।") in HEARD_VARIANTS.get(expected, ())
                for expected, w in zip(tokens, phrase)
            ):
                found.append({"tag":tag,"heard":" ".join(w["text"] for w in phrase),
                              "start":phrase[0]["start"],"end":phrase[-1]["end"]})
    return sorted(found,key=lambda item:(item["start"],item["tag"]))
