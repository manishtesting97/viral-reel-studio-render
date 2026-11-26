"""Monotonic script-to-ASR alignment with measured, unmodified spans."""
from functools import lru_cache
import math
import re
import unicodedata

QUALITY_FLOOR = 0.75
MIN_ANCHOR_CONFIDENCE = 0.25
REVIEW_CONFIDENCE = 0.55
DEVANAGARI = re.compile(r"[\u0900-\u097f]")
CONSONANTS = dict(zip(
    "कखगघङचछजझञटठडढणतथदधनपफबभमयरलवशषसहक़ख़ग़ज़ड़ढ़फ़",
    "k kh g gh ng ch chh j jh ny t th d dh n t th d dh n p ph b bh m y r l v sh sh s h k kh g z r rh f".split(),
))
VOWELS = dict(zip("अआइईउऊऋएऐओऔऑऍ", "a aa i ee u oo ri e ai o au o e".split()))
MARKS = dict(zip("ािीुूृेैोौॉॅ", "aa i ee u oo ri e ai o au o e".split()))
# Common Hindi romanizations and English loan pronunciations. These are word
# equivalences; broad consonant replacement would merge unrelated short words.
VARIANTS = {
    "kehte": ("kahte",), "keh": ("kah",), "nahi": ("nahin",), "zarurat": ("jarurat",),
    "zaroor": ("jarur",), "aur": ("or",), "mein": ("men", "main"),
    "taiyaar": ("tayaar",), "sirf": ("sirph",),
    "goal": ("gol",), "plan": ("plaan",), "permission": ("parmishan", "paramishan"),
    "agent": ("ejent", "ajent", "ajant"), "research": ("risarch", "riserc"),
    "search": ("sarch",), "information": ("informeshan", "inphormeshan"),
    "report": ("riport",), "decisions": ("disizhans", "disijans", "diseejans"),
    "decision": ("disizhan", "disijan"), "human": ("hyuman", "humaan"),
    "approval": ("apruval", "aprooval"), "follow": ("folo", "pholo"),
    "result": ("rijalt", "rizalt"), "tool": ("tul",), "tools": ("tuls", "tulz"),
    "check": ("chek",), "use": ("yuz", "yuj"), "important": ("importent",),
}
# Attested Devanagari spellings; do not equate unrelated Latin spellings.
DEVANAGARI_VARIANTS = {"plan": {"प्लैन"}, "jawab": {"जबाब"}, "waqt": {"वक्त", "वक़्त"}}
# English plurals may have ambiguous ASR boundaries. They are review matches,
# rather than pronunciation equivalents or strong gate evidence.


@lru_cache(maxsize=8192)
def normalized(text):
    return "".join(c for c in unicodedata.normalize("NFKC", text).casefold()
                   if unicodedata.category(c)[0] in "LNM")


def roman_key(text):
    text = re.sub("aa+", "a", text)
    text = re.sub("ee+", "i", text)
    text = re.sub("oo+", "u", text)
    text = re.sub(r"iy(?=[aeou])", "i", text)
    return text.replace("ii", "i").replace("uu", "u").replace("w", "v").replace("ph", "f")


def devanagari_keys(text):
    """Keep optional medial schwas bounded; remove the usual final schwa."""
    parts = []
    i = 0
    while i < len(text):
        ch = text[i]
        i += 1
        if ch in CONSONANTS:
            sound = CONSONANTS[ch]
            if i < len(text) and text[i] == "़":
                sound = {"j": "z", "ph": "f", "d": "r"}.get(sound, sound)
                variants_c = ("r", "d") if ch == "ड" else (sound,)
                i += 1
            elif ch == "ज":
                variants_c = ("j", "z")
            elif ch == "ड़":
                variants_c = ("r", "d")
            else:
                variants_c = (sound,)
            if i < len(text) and (text[i] in MARKS or text[i] == "्"):
                mark = MARKS.get(text[i], "")
                parts.append(tuple(dict.fromkeys(c + mark for c in variants_c)))
                i += 1
            else:
                final = i == len(text)
                if final:
                    parts.append(tuple(dict.fromkeys(x for c in variants_c for x in (c, c + "a"))))
                else:
                    parts.append(tuple(dict.fromkeys(x for c in variants_c for x in (c + "a", c))))
        elif ch in "ंँ":
            final = i == len(text)
            if final:
                parts.append(("n", ""))
            elif i < len(text) and text[i] in "पफबभम":
                parts.append(("n", "m"))
            else:
                parts.append(("n",))
        else:
            if ch == "औ":
                parts.append(("au", "o"))
            else:
                parts.append((VOWELS.get(ch, "h" if ch == "ः" else ch),))
    if not parts:
        return frozenset()
    keys = {""}
    for variants in parts:
        # Long strings usually only occur in compound names. Bound work per word.
        keys = set(sorted({prefix + sound for prefix in keys for sound in variants},
                          key=lambda value: (-len(value), value))[:64])
    return frozenset(roman_key(key) for key in keys if key)


@lru_cache(maxsize=4096)
def english_phonetic_keys(text):
    """General English-orthography to Indian-phonetic keys for cross-script comparison only."""
    token = normalized(text)
    if not re.fullmatch(r"[a-z]{3,}", token):
        return frozenset()

    def base_forms(word):
        # Preserve doubled-consonant simplification, silent-e diphthongs, and common suffixes.
        w = word
        w = re.sub(r"tion\b", "shan", w)
        w = re.sub(r"sion\b", "jan", w)
        w = re.sub(r"ture\b", "char", w)
        w = re.sub(r"([bcdfghjklmnpqrstvwxyz])\1+", r"\1", w)
        # Keep -ange words (change/range/arrange) before generic silent-e normalization.
        if re.search(r"ange\b", w):
            w = re.sub(r"ange\b", "enj", w)
        elif w.endswith("are") and len(w) >= 4:
            w = w[:-3] + "er"
        elif re.search(r"a[bcdfghjklmnpqrstvwxyz]e\b", w):
            w = re.sub(r"a([bcdfghjklmnpqrstvwxyz])e\b", r"e\1", w)
        elif len(w) >= 6 and w.endswith("ice"):
            w = w[:-3] + "is"
        elif re.search(r"i[bcdfghjklmnpqrstvwxyz]e\b", w):
            w = re.sub(r"i([bcdfghjklmnpqrstvwxyz])e\b", r"ai\1", w)
        elif re.search(r"o[bcdfghjklmnpqrstvwxyz]e\b", w):
            w = re.sub(r"o([bcdfghjklmnpqrstvwxyz])e\b", r"o\1", w)
        elif re.search(r"u[bcdfghjklmnpqrstvwxyz]e\b", w):
            w = re.sub(r"u([bcdfghjklmnpqrstvwxyz])e\b", r"yu\1", w)
        elif len(w) >= 4 and w.endswith("es") and w[-3] in "cgjszx":
            if w.endswith("anges"):
                w = w[:-5] + "enjis"
            else:
                w = w[:-2] + "is"
        elif len(w) >= 4 and w.endswith("e") and not w.endswith(("ee", "ie", "ye")):
            w = w[:-1]
        w = w.replace("igh", "ai").replace("qu", "kv").replace("ph", "f").replace("ck", "k")
        w = w.replace("tch", "ch").replace("dge", "j")
        # Keep 'ch' intact before c->k so English 'ch' stays 'ch' (with 'k' branch below for tech/arch).
        w = re.sub(r"c(?=[eiy])", "s", w)
        w = re.sub(r"c(?!h)", "k", w)
        # Keep initial 'gi-' hard (/g/: git, give) while allowing medial/soft g before e/i/y.
        w = re.sub(r"(?<!\A)g(?=[iy])|g(?=e)", "j", w)
        w = re.sub(r"(?<![ck]r)(?:ea|ee|ie|ei)", "i", w)
        w = re.sub(r"(ai|ay)", "e", w)
        w = w.replace("oa", "o").replace("ou", "au").replace("ow", "o")
        w = re.sub(r"x", "ksh", w)
        w = re.sub(r"y\b", "i", w)
        w = re.sub(r"(er|or|ar)\b", "ar", w)
        w = re.sub(r"([a-z])le\b", r"\1al", w)
        forms = {w}
        if "ch" in w:
            forms.add(w.replace("ch", "k"))
        expanded = set()
        for f in forms:
            expanded.add(f)
            # Optional unstressed vowel reductions common in Hindi transliterations (commit->kamit, control->kantrol)
            expanded.add(re.sub(r"^([kptbdgmnrsflvh]+)o", r"\1a", f))
            expanded.add(re.sub(r"o([bcdfghjklmnpqrstvwxyz]{1,2}[aeiou])", r"a\1", f))
            expanded.add(re.sub(r"e([rnml])", r"a\1", f))
            # Short closed-syllable English 'u' (/ʌ/ in hub, run, sub, budget, cut) is spelled 'a' in Hindi.
            if not re.search(r"yu", f):
                expanded.add(re.sub(r"([bcdfghjklmnpqrstvwxyz])u([bcdfghjklmnpqrstvwxyz])", r"\1a\2", f))
            if len(f) >= 6:
                expanded.add(re.sub(r"([bcdfghjklmnpqrstvwxyz])[aeiou]([rl][aeiou])", r"\1\2", f))
                expanded.add(f.replace("pyu", "pu"))
        return expanded

    out = set(base_forms(token))
    if len(token) >= 5 and token.endswith("s") and not token.endswith("ss"):
        for sform in base_forms(token[:-1]):
            out.add(sform + "s")
            out.add(sform + "is")
    return frozenset(roman_key(x) for x in sorted(out)[:24] if x)


@lru_cache(maxsize=4096)
def pronunciations(text):
    token = normalized(text)
    if not token:
        return frozenset()
    if DEVANAGARI.search(token):
        return devanagari_keys(token)
    return frozenset(roman_key(key) for key in (token, *VARIANTS.get(token, ())))


def grouped_keys(tokens, cross_script=False):
    keys = {""}
    for token in tokens:
        values = pronunciations(token)
        if cross_script and not DEVANAGARI.search(normalized(token)):
            values = values | english_phonetic_keys(token)
        keys = set(sorted({a + b for a in keys for b in values}, key=lambda v: (-len(v), v))[:128])
    # A duplicated boundary aspiration or split consonant+aspiration (e.g. रह+खो -> rakho) in split tokens.
    keys |= {key.replace("khh", "kh") for key in keys if "khh" in key}
    keys |= {re.sub(r"h(kh|gh|ch|jh|th|dh|ph|bh)", r"\1", key) for key in keys}
    return keys


def lexical(left, right, multilingual):
    a, b = normalized(left), normalized(right)
    if a == b:
        return "exact"
    if multilingual:
        da, db = bool(DEVANAGARI.search(a)), bool(DEVANAGARI.search(b))
        if b in DEVANAGARI_VARIANTS.get(a, ()):
            return "normalized"
        if not (da and db):
            left_p = pronunciations(left) | (english_phonetic_keys(left) if (da != db and not da) else frozenset())
            right_p = pronunciations(right) | (english_phonetic_keys(right) if (da != db and not db) else frozenset())
            if left_p & right_p:
                return "normalized"
    return None


def valid_span(parts, duration):
    return (all(math.isfinite(w["start"]) and math.isfinite(w["end"]) and
                0 <= w["start"] < w["end"] <= duration for w in parts)
            and all(0 <= b["start"] - a["end"] <= 0.24 for a, b in zip(parts, parts[1:])))


def strong_pairs(script, observed, duration, multilingual, witnesses=None):
    """Global monotonic search with nearby-word evidence for ambiguous anchors."""
    n, m = len(script), len(observed)
    scores = [[float("-inf")] * (m + 1) for _ in range(n + 1)]
    back = [[None] * (m + 1) for _ in range(n + 1)]
    scores[0][0] = 0
    steps = ((1, 1), (1, 2), (1, 3), (2, 1), (3, 1))
    left_groups, right_groups = {}, {}
    left_cross, right_cross = {}, {}
    for size in (1, 2, 3):
        for i in range(n - size + 1):
            if script[i][0] == script[i + size - 1][0]:
                toks = [text for _, text in script[i:i + size]]
                left_groups[i, size] = grouped_keys(toks)
                if multilingual:
                    left_cross[i, size] = grouped_keys(toks, cross_script=True)
        for j in range(m - size + 1):
            if valid_span(observed[j:j + size], duration):
                toks = [w["text"] for w in observed[j:j + size]]
                right_groups[j, size] = grouped_keys(toks)
                if multilingual:
                    right_cross[j, size] = grouped_keys(toks, cross_script=True)

    def move(i, j, di, dj, value, kind=None):
        a, b = i + di, j + dj
        score = scores[i][j] + value
        if score > scores[a][b]:
            scores[a][b], back[a][b] = score, (i, j, kind)

    def neighbor(i, j):
        return 0 <= i < n and 0 <= j < m and lexical(script[i][1], observed[j]["text"], multilingual)

    for i in range(n + 1):
        for j in range(m + 1):
            if i < n:
                move(i, j, 1, 0, -1)
            if j < m:
                move(i, j, 0, 1, -1)
            if i >= n or j >= m:
                continue
            for di, dj in steps:
                if (i, di) not in left_groups or (j, dj) not in right_groups:
                    continue
                if di == dj == 1:
                    kind = lexical(script[i][1], observed[j]["text"], multilingual)
                    if not kind and witnesses and ((i, j, 1) in witnesses or (i, j) in witnesses):
                        kind = "corroborated"
                elif di == 1 and witnesses and (i, j, dj) in witnesses:
                    kind = "corroborated"
                else:
                    a = "".join(normalized(text) for _, text in script[i:i + di])
                    b = "".join(normalized(w["text"]) for w in observed[j:j + dj])
                    if a == b:
                        kind = "normalized"
                    elif multilingual and not (DEVANAGARI.search(a) and DEVANAGARI.search(b)):
                        da, db = bool(DEVANAGARI.search(a)), bool(DEVANAGARI.search(b))
                        lg = left_cross[i, di] if da != db else left_groups[i, di]
                        rg = right_cross[j, dj] if da != db else right_groups[j, dj]
                        kind = "normalized" if (lg & rg) else None
                    else:
                        kind = None
                if kind:
                    context = bool(neighbor(i - 1, j - 1)) + bool(neighbor(i + di, j + dj))
                    probability = min(w["probability"] for w in observed[j:j + dj])
                    move(i, j, di, dj, 3 + 0.35 * context + 0.15 * probability + 0.4 * (kind == "exact"), kind)
    pairs = []
    i, j = n, m
    while i or j:
        a, b, kind = back[i][j]
        if kind:
            pairs.append((a, i, b, j, kind))
        i, j = a, b
    return list(reversed(pairs))


def edit_distance(a, b):
    last = list(range(len(b) + 1))
    for i, char in enumerate(a, 1):
        row = [i]
        for j, other in enumerate(b, 1):
            row.append(min(last[j] + 1, row[-1] + 1, last[j - 1] + (char != other)))
        last = row
    return last[-1]


def _lcs_len(a, b):
    dp = [0] * (len(b) + 1)
    for ca in a:
        prev = 0
        for j, cb in enumerate(b, 1):
            cur = dp[j]
            dp[j] = prev + 1 if ca == cb else max(dp[j], dp[j - 1])
            prev = cur
    return dp[-1]


def near(left, right, multilingual):
    a_raw, b_raw = normalized(left), normalized(right)
    cross = multilingual and (bool(DEVANAGARI.search(a_raw)) != bool(DEVANAGARI.search(b_raw)))
    left_set = pronunciations(left) | (english_phonetic_keys(left) if cross and not DEVANAGARI.search(a_raw) else frozenset()) if multilingual else {a_raw}
    right_set = pronunciations(right) | (english_phonetic_keys(right) if cross and not DEVANAGARI.search(b_raw) else frozenset()) if multilingual else {b_raw}
    for a in left_set:
        for b in right_set:
            if not a or not b:
                continue
            # Short bilabial/labiodental onset swap in Hindi ASR (e.g. Bas -> वस)
            if len(a) == len(b) >= 3 and a[1:] == b[1:] and {a[0], b[0]} in ({"b", "v"}, {"b", "f"}):
                return True
            if a[0] != b[0]:
                continue
            longest, shortest = max(len(a), len(b)), min(len(a), len(b))
            if longest >= 4 and shortest >= 3 and edit_distance(a, b) <= (2 if shortest >= 7 else 1):
                return True
            if len(a) == len(b) == 3:
                edits = [(x, y) for x, y in zip(a, b) if x != y]
                if len(edits) == 1 and (all(c in "aeiou" for c in edits[0])
                                        or (a[:2] == b[:2] and set(edits[0]) in ({"t", "d"}, {"p", "b"}, {"k", "g"}))):
                    return True
    return False


def corroborated_pairs(script, observed, alternate, duration):
    """Use independent English ASR evidence to confirm tightly located cross-script words."""
    anchors = strong_pairs(script, observed, duration, True)
    edges = [(0, 0, 0, 0, None), *anchors,
             (len(script), len(script), len(observed), len(observed), None)]

    def skeleton(token):
        token = re.sub(r"c(?=[eiy])", "s", token).replace("c", "k").replace("ph", "f").replace("q", "k")
        token = re.sub(r"[aeiouy]", "", token).translate(str.maketrans("dbgz", "tpks"))
        return re.sub(r"(.)\1+", r"\1", token)

    def skeleton_grade(expected, primary_tokens):
        p_keys = grouped_keys([w["text"] for w in primary_tokens], cross_script=True)
        e_forms = {expected} | english_phonetic_keys(expected)
        e_skels = {skeleton(k) for k in e_forms if len(skeleton(k)) >= 2}
        if not e_skels:
            return None
        p_skels = {skeleton(k) for k in p_keys if len(skeleton(k)) >= 2}
        if not p_skels:
            return None
        # Exact voiced/voiceless consonant skeleton match
        if e_skels & p_skels:
            return "exact"
        best_dist = min(edit_distance(ps, es) for ps in p_skels for es in e_skels)
        if best_dist <= 1 and min(len(ps) for ps in p_skels) >= 2:
            return "close"
        # Subsequence match only on longer corrupted stems (>= 4 consonants) preserving onset or coda
        for ps in p_skels:
            for es in e_skels:
                longest = max(len(ps), len(es))
                if longest >= 4 and (ps[0] == es[0] or ps[-1] == es[-1]):
                    if _lcs_len(ps, es) >= math.ceil(0.65 * longest):
                        return "subseq"
        return None

    witnesses = {}
    for left, right in zip(edges, edges[1:]):
        if not (left[4] or right[4]):
            continue
        si_lo, si_hi = left[1], right[0]
        oj_lo, oj_hi = left[3], right[2]
        if not (1 <= si_hi - si_lo <= 12 and 1 <= oj_hi - oj_lo <= 16):
            continue
        t_left = observed[oj_lo - 1]["end"] if left[4] else 0.0
        t_right = observed[oj_hi]["start"] if right[4] else duration
        # Build all valid corroborated candidates inside this gap, then select greedily in order
        candidates_in_gap = []
        for i in range(si_lo, si_hi):
            expected = normalized(script[i][1])
            if not re.fullmatch(r"[a-z]{4,}", expected):
                continue
            for j in range(oj_lo, oj_hi):
                for dj in (1, 2):
                    if j + dj > oj_hi:
                        continue
                    prim = observed[j:j + dj]
                    if not (valid_span(prim, duration) and any(DEVANAGARI.search(w["text"]) for w in prim)):
                        continue
                    prim_prob = min(w["probability"] for w in prim)
                    if prim_prob < 0.24:
                        continue
                    p_start, p_end = prim[0]["start"], prim[-1]["end"]
                    if p_start < t_left - 0.02 or p_end > t_right + 0.02:
                        continue
                    grade = skeleton_grade(expected, prim)
                    if not grade:
                        continue
                    # Compare against 1-token and merged 2-token independent English ASR witnesses
                    best_w = None
                    p_dur = max(0.05, p_end - p_start)
                    for k in range(len(alternate)):
                        for dw in (1, 2):
                            if k + dw > len(alternate):
                                continue
                            wslice = alternate[k:k + dw]
                            if not all(math.isfinite(w["start"]) and math.isfinite(w["end"])
                                       and 0 <= w["start"] <= w["end"] <= duration for w in wslice):
                                continue
                            if dw == 2 and not (0 <= wslice[1]["start"] - wslice[0]["end"] <= 0.24):
                                continue
                            w_start, w_end = wslice[0]["start"], wslice[-1]["end"]
                            if w_end < p_start - 0.12 or w_start > p_end + 0.12:
                                continue
                            overlap = min(w_end, p_end) - max(w_start, p_start)
                            w_dur = max(0.05, w_end - w_start)
                            if overlap < 0.45 * min(p_dur, w_dur) and not (p_start - 0.08 <= w_start <= w_end <= p_end + 0.08):
                                continue
                            w_joined = "".join(normalized(w["text"]) for w in wslice)
                            w_prob = min(w["probability"] for w in wslice)
                            if prim_prob < 0.30 and not (w_joined == expected and w_prob >= 0.60):
                                continue
                            exact_en = (w_joined == expected)
                            split_ies = (
                                dw == 2 and expected.endswith("ies") and len(expected) >= 5
                                and normalized(wslice[1]["text"]) in ("is", "es", "s")
                                and normalized(wslice[0]["text"]).endswith("y")
                                and normalized(wslice[0]["text"])[:-1] + "ies" == expected
                            )
                            close_en = (len(expected) >= 5 and grade in ("exact", "close") and edit_distance(w_joined, expected) <= 1 and w_joined.endswith("s") == expected.endswith("s"))
                            if exact_en and grade in ("exact", "close") and w_prob >= 0.35:
                                score = (3, -dw, w_prob, overlap)
                            elif (exact_en or split_ies or close_en) and w_prob >= 0.55:
                                score = (2 if (exact_en or split_ies) else 1, -dw, w_prob, overlap)
                            else:
                                continue
                            if best_w is None or score > best_w[0]:
                                best_w = (score, {
                                    "text": " ".join(w["text"] for w in wslice),
                                    "start": w_start, "end": w_end, "probability": w_prob,
                                })
                    if best_w is not None:
                        candidates_in_gap.append((best_w[0], -dj, i, j, dj, best_w[1]))
        # Greedy monotonic resolution inside the gap; strictly prefer 1-token primary spans over 2-token spans.
        candidates_in_gap.sort(key=lambda item: (item[0][0], item[1], item[0][1], item[0][2], item[0][3]), reverse=True)
        chosen = []
        used_i, used_j = set(), set()
        for _, _, i, j, dj, w_info in candidates_in_gap:
            js = set(range(j, j + dj))
            if i in used_i or js & used_j:
                continue
            # Maintain strict monotonicity against already picked items in gap
            if any((i < ci and j >= cj) or (i > ci and j <= cj) for ci, cj in chosen):
                continue
            used_i.add(i)
            used_j.update(js)
            chosen.append((i, j))
            witnesses[i, j, dj] = w_info
    return witnesses


def match_script(scenes, observed, duration, language, *, alternate=None):
    script = [(scene["id"], token) for scene in scenes for token in scene["narration"].split()
              if normalized(token)]
    multilingual = language in {"hi", "hinglish"}
    witnesses = corroborated_pairs(script, observed, alternate, duration) if multilingual and alternate else {}
    pairs = strong_pairs(script, observed, duration, multilingual, witnesses)
    # Approximation is confined to small, equal-length gaps between strong
    # anchors. A lone edge word needs two consecutive neighboring anchors.
    approximations, candidates, review_windows = [], {}, {}
    edges = [(0,0,0,0,None), *pairs,
             (len(script),len(script),len(observed),len(observed),None)]
    for k, (previous, following) in enumerate(zip(edges, edges[1:])):
        i, end = previous[1], following[0]
        j, stop = previous[3], following[2]
        measured = [{**observed[k], "asrIndex": k} for k in range(j, stop)
                    if valid_span([observed[k]], duration)]
        if measured or previous[4] and following[4]:
            start = observed[previous[3]-1]["end"] if previous[4] else measured[0]["start"]
            stop_time = observed[following[2]]["start"] if following[4] else measured[-1]["end"]
            window = {"start": start, "end": stop_time, "heard": " ".join(w["text"] for w in measured),
                      "recognition": measured}
            for index in range(i, end):
                if stop_time > start:
                    review_windows[index] = window
        centered = previous[4] and following[4]
        if centered and end - i == 1 and stop - j == 2:
            grouped = observed[j:stop]
            joined = "".join(normalized(w["text"]) for w in grouped)
            if (valid_span(grouped, duration) and min(w["probability"] for w in grouped) >= MIN_ANCHOR_CONFIDENCE
                    and near(script[i][1], joined, multilingual)):
                approximations.append((i, i + 1, j, stop, "approximate"))
            continue
        if not 1 <= end - i == stop - j <= 2:
            continue
        leading = (k == 0 and end-i == 1 and len(pairs) >= 2 and
                   pairs[0][1] == pairs[1][0] and pairs[0][3] == pairs[1][2])
        trailing = (k == len(pairs) and end-i == 1 and len(pairs) >= 2 and
                    pairs[-2][1] == pairs[-1][0] and pairs[-2][3] == pairs[-1][2])
        for offset in range(end-i):
            w = observed[j+offset]
            candidates[i+offset] = {**w, "asrIndex": j+offset} if valid_span([w], duration) else None
            if ((centered or leading or trailing) and w["probability"] >= MIN_ANCHOR_CONFIDENCE
                    and valid_span([w],duration) and near(script[i+offset][1],w["text"],multilingual)):
                approximations.append((i+offset,i+offset+1,j+offset,j+offset+1,"approximate"))
    pairs = sorted(pairs+approximations)
    words, review, unmatched, deviations = [], [], [], []
    counts = dict(exact=0, normalized=0, approximate=0, corroborated=0)
    reliable = 0
    used, consumed = set(), set()
    for a,b,c,d,kind in pairs:
        heard = observed[c:d]
        start,end = heard[0]["start"],heard[-1]["end"]
        if words and start < words[-1]["end"]:
            continue
        confidence = min(w["probability"] for w in heard)
        shared = b-a > 1 or d-c > 1
        reasons = []
        if kind == "approximate":
            reasons.append("Contextual phonetic match; check the recognized spelling.")
        if confidence < REVIEW_CONFIDENCE:
            reasons.append("Low recognition confidence.")
        if shared:
            reasons.append("Grouped words share the original measured span.")
        text = " ".join(token for _,token in script[a:b])
        entry = dict(scene=script[a][0],text=text,heard=" ".join(w["text"] for w in heard),
                     scriptIndex=a,
                     start=start,end=end,match=kind,probability=confidence,
                     confidence="review" if reasons else kind,
                     granularity="shared-span" if shared else "word",scriptWords=b-a,
                     asrIndices=list(range(c,d)))
        if kind == "corroborated":
            entry["corroboration"] = witnesses.get((a, c, d - c)) or witnesses[a, c]
        words.append(entry)
        if reasons:
            review.append({**entry,"reason":" ".join(reasons)})
        counts[kind] += b-a
        if kind != "approximate" and confidence >= MIN_ANCHOR_CONFIDENCE:
            reliable += b-a
        used.update(range(a,b))
        consumed.update(range(c,d))
    for i,(scene,text) in enumerate(script):
        if i not in used:
            row = dict(scene=scene,text=text,scriptIndex=i,match="unmatched",
                       reason="No reliable measured match in this part of the sequence.")
            if candidates.get(i):
                row["candidate"] = candidates[i]
                row["heard"] = candidates[i]["text"]
                if candidates[i]["probability"] < MIN_ANCHOR_CONFIDENCE:
                    row["reason"] = "Nearby recognition is below the ASR confidence floor."
                elif not near(text, candidates[i]["text"], multilingual):
                    row["reason"] = "The nearby recognition lacks a supported lexical or phonetic match."
                else:
                    row["reason"] = "Nearby phonetic resemblance alone is insufficient to accept this timing."
            if i in review_windows:
                row["reviewWindow"] = review_windows[i]
            unmatched.append(row)
    for i,w in enumerate(observed):
        if i not in consumed:
            deviations.append(dict(kind="unassigned-recognition",asrIndex=i,**w))
    denominator = max(len(script),len(observed),1)
    return words, dict(scriptWords=len(script),recognizedWords=len(observed),
                       exactMatches=counts["exact"],normalizedMatches=counts["normalized"],
                       corroboratedMatches=counts["corroborated"],
                       approximateMatches=counts["approximate"],reliableMatches=reliable,
                       coverage=len(used)/max(len(script),1),reliableCoverage=reliable/denominator,
                       threshold=QUALITY_FLOOR,reviewWords=review,unmatched=unmatched,deviations=deviations,
                       matches=words)
