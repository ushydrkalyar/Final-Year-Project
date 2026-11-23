"""
comment_parser.py — Caption Correction Engine (Step 6: Feedback & Refinement)

Turns a one-line comment such as:
    fix spelling of 'recieve' to 'receive'
    change caption 3 to Welcome back to the channel
    caption 5: This is the new line
    delete caption 7
into a precise edit of a single caption — using regex pattern-matching and
fuzzy text comparison only (Python's own re/difflib). No external service,
model download or API call is involved anywhere in this module.

Only the ONE caption the comment refers to is ever touched; every other
caption in the list is returned completely unchanged, byte for byte.
"""
import re
import difflib

_ORDINALS = {
    'first': 1, 'second': 2, 'third': 3, 'fourth': 4, 'fifth': 5,
    'sixth': 6, 'seventh': 7, 'eighth': 8, 'ninth': 9, 'tenth': 10,
}
_QUOTE = r'["“”‘’\']'
_QUOTED = _QUOTE + r'([^"“”‘’\']+)' + _QUOTE


def _strip_quotes(s):
    s = (s or '').strip().rstrip('.').strip()
    if len(s) >= 2 and s[0] in '"\'“‘' and s[-1] in '"\'”’':
        return s[1:-1].strip()
    return s


def _n_str(n):
    return f"{'is' if n == 1 else 'are'} only {n} caption{'s' if n != 1 else ''}"


def _parse_time(text):
    """Extracts a single timestamp (in seconds) mentioned in the comment, e.g.
    "at 22 sec", "at 22s", "at 1:22" — or None if no time is mentioned."""
    m = (re.search(r'\bat\s+(\d{1,3}):(\d{2}(?:\.\d+)?)\b', text, re.IGNORECASE) or
         re.search(r'\b(\d{1,3}):(\d{2}(?:\.\d+)?)\b', text))
    if m:
        return int(m.group(1)) * 60 + float(m.group(2))
    m = re.search(r'\bat\s+(\d+(?:\.\d+)?)\s*(?:sec|secs|second|seconds|s)\b', text, re.IGNORECASE)
    if m:
        return float(m.group(1))
    return None


def _find_by_time(t, captions):
    """Which caption is playing at time t? Falls back to the nearest caption
    edge within 3s; returns -1 if nothing is close enough."""
    for i, c in enumerate(captions):
        if c['s'] - 0.05 <= t <= c['e'] + 0.05:
            return i
    best_i, best_d = None, None
    for i, c in enumerate(captions):
        d = min(abs(t - c['s']), abs(t - c['e']))
        if best_d is None or d < best_d:
            best_d, best_i = d, i
    return best_i if best_d is not None and best_d <= 3.0 else -1


def _index_error_message(text, n):
    ts = _parse_time(text)
    if ts is not None:
        return f"I couldn't find a caption near {ts:g}s — check the time and try again."
    return f"There {_n_str(n)}."


def _find_explicit_index(text, captions):
    """Returns a 0-based caption index if the comment names one explicitly —
    by number, position (first/last/2nd-to-last/ordinal), or timestamp.
    None = no explicit reference found; -1 = referenced but out of range."""
    n = len(captions)
    t = text.lower()
    m = re.search(r'\b(?:caption|line|number|no\.?)\s*#?\s*(\d+)\b', t)
    if not m:
        m = re.search(r'\b(\d+)(?:st|nd|rd|th)\s*(?:caption|line)\b', t)
    if m:
        i = int(m.group(1)) - 1
        return i if 0 <= i < n else -1
    if re.search(r'\bsecond[\s-]to[\s-]last\b|\bsecond\s+last\b', t):
        return n - 2 if n >= 2 else -1
    if re.search(r'\b(last|final)\b', t):
        return n - 1
    if re.search(r'\bfirst\b', t):
        return 0
    for word, num in _ORDINALS.items():
        if re.search(rf'\b{word}\s*(caption|line)\b', t):
            return (num - 1) if (num - 1) < n else -1
    ts = _parse_time(text)
    if ts is not None:
        return _find_by_time(ts, captions)
    return None


def _find_by_content(snippet, captions):
    """Which caption(s) contain this text? Returns matching indices, best first."""
    snip = (snippet or '').lower().strip()
    if not snip:
        return []
    exact = [i for i, c in enumerate(captions) if snip in c['t'].lower()]
    if exact:
        return exact
    scored = []
    for i, c in enumerate(captions):
        ratio = difflib.SequenceMatcher(None, snip, c['t'].lower()).ratio()
        if ratio > 0.45:
            scored.append((ratio, i))
    scored.sort(reverse=True)
    return [i for _, i in scored]


def _word_replace(text, old, new):
    """Case-insensitive literal replace of the first match, keeping capitalisation."""
    pat = re.compile(re.escape(old), re.IGNORECASE)
    def repl(m):
        matched = m.group(0)
        if matched.isupper() and len(matched) > 1:
            return new.upper()
        if matched[:1].isupper():
            return new[:1].upper() + new[1:]
        return new
    return pat.sub(repl, text, count=1)


def _edit_result(captions, idx, after):
    before = captions[idx]['t']
    new_caps = [dict(c) for c in captions]
    new_caps[idx]['t'] = after
    return {"ok": True, "action": "edit", "index": idx, "before": before, "after": after,
            "captions": new_caps,
            "message": f"Updated caption {idx + 1}: “{before}” → “{after}”"}


def _apply_word_fix(original_comment, captions, old, new, n):
    if not old or new is None:
        return {"ok": False, "message": "I need both the word to fix and what to change it to.", "candidates": []}
    idx = _find_explicit_index(original_comment, captions)
    if idx == -1:
        return {"ok": False, "message": _index_error_message(original_comment, n), "candidates": []}
    if idx is None:
        cands = _find_by_content(old, captions)
        if not cands:
            return {"ok": False, "message": f"I couldn't find “{old}” in any caption.", "candidates": []}
        if len(cands) > 1:
            return {"ok": False,
                    "message": f"“{old}” appears in {len(cands)} captions — pick one, or say the caption number.",
                    "candidates": [{"index": i, "text": captions[i]['t']} for i in cands[:6]]}
        idx = cands[0]
    before = captions[idx]['t']
    after = _word_replace(before, old, new)
    after = re.sub(r'\s{2,}', ' ', after).strip()
    if after == before:
        return {"ok": False,
                "message": f"Caption {idx + 1} doesn't contain “{old}” exactly — "
                            f"check it matches: “{before}”",
                "candidates": []}
    return _edit_result(captions, idx, after)


def parse_command(comment, captions):
    """
    captions: list of {"s": float, "e": float, "t": str}, in on-screen order.

    Returns either:
      {"ok": True,  "action": "edit"|"delete", "index": i, "before": str, "after": str|None,
       "captions": <new list — only index i differs from the input>, "message": str}
      {"ok": False, "message": str, "candidates": [{"index": i, "text": str}, ...]}
    """
    n = len(captions)
    text = (comment or '').strip()
    if not text:
        return {"ok": False, "message": "Write what you'd like changed — e.g. "
                                          "“fix spelling of 'recieve' to 'receive'”.", "candidates": []}
    if n == 0:
        return {"ok": False, "message": "There are no captions yet.", "candidates": []}
    low = text.lower()

    # 1. delete a whole caption ------------------------------------------------
    if re.search(r'\b(?:delete|remove|drop)\s+(?:this\s+|the\s+)?(?:caption|line)\b', low):
        idx = _find_explicit_index(text, captions)
        if idx is None:
            quotes = re.findall(_QUOTED, text)
            snippet = quotes[0] if quotes else None
            if snippet is None:
                sm = re.search(r'\b(?:that\s+)?(?:says?|saying|about|containing|with)\s+(.+)$', text, re.IGNORECASE)
                if sm:
                    snippet = sm.group(1)
            cands = _find_by_content(snippet, captions) if snippet else []
            if len(cands) == 1:
                idx = cands[0]
            elif len(cands) > 1:
                return {"ok": False, "message": "That text matches more than one caption — say the number instead.",
                        "candidates": [{"index": i, "text": captions[i]['t']} for i in cands[:6]]}
            else:
                return {"ok": False,
                        "message": "Which caption should I delete? e.g. “delete caption 3”.",
                        "candidates": []}
        if idx == -1:
            return {"ok": False, "message": _index_error_message(text, n), "candidates": []}
        removed = captions[idx]['t']
        new_caps = captions[:idx] + captions[idx + 1:]
        return {"ok": True, "action": "delete", "index": idx, "before": removed, "after": None,
                "captions": new_caps, "message": f"Removed caption {idx + 1}: “{removed}”"}

    # 2. remove one word/phrase from a caption
    #    ("remove 'um' from caption 4", "remove 'the' at 22 sec") ------------
    m = re.search(r'\bremove\s+(?:the\s+word\s+)?' + _QUOTED, text, re.IGNORECASE)
    if m:
        return _apply_word_fix(text, captions, m.group(1), '', n)

    # 3. explicit "change/replace 'X' to/with 'Y'" -----------------------------
    m = re.search(r'\b(?:change|replace)\s+' + _QUOTED + r'\s+(?:to|with)\s+' + _QUOTED, text, re.IGNORECASE)
    if m:
        return _apply_word_fix(text, captions, m.group(1), m.group(2), n)

    # 4. "fix (the) spelling of X to Y" ----------------------------------------
    fix = r"\bfix(?:\s+the)?\s+spelling\s+of\s+"
    m = (re.search(fix + _QUOTED + r"\s+to\s+" + _QUOTED, text, re.IGNORECASE) or  # quoted: any phrase
         re.search(fix + r"([\w’-]+)\s+to\s+([\w’-]+)", text, re.IGNORECASE))
    if m:
        return _apply_word_fix(text, captions, m.group(1), m.group(2), n)

    # 5a. whole-caption rewrite: "change caption 3 to text" ---------------------
    m = re.search(r'\bchange\s+(?:caption|line)\s*#?\s*(\d+)\s+to\s+(.+)$', text, re.IGNORECASE)
    if m:
        idx = int(m.group(1)) - 1
        if not (0 <= idx < n):
            return {"ok": False, "message": f"There {_n_str(n)}.", "candidates": []}
        return _edit_result(captions, idx, _strip_quotes(m.group(2)))

    # 5b. whole-caption rewrite: "caption 3: text" / "caption 3 should say text" -
    m = re.search(r'\b(?:caption|line)\s*#?\s*(\d+)\s*(?::|-|should\s+say|should\s+read|say)\s*(.+)$',
                  text, re.IGNORECASE)
    if m:
        idx = int(m.group(1)) - 1
        if not (0 <= idx < n):
            return {"ok": False, "message": f"There {_n_str(n)}.", "candidates": []}
        return _edit_result(captions, idx, _strip_quotes(m.group(2)))

    # 6. unquoted "change X to Y" / "X should be/say/read Y" / "replace X with Y"
    m = (re.search(r'\bchange\s+(.+?)\s+to\s+(.+)$', text, re.IGNORECASE) or
         re.search(r'^(.+?)\s+should\s+(?:be|say|read)\s+(.+)$', text, re.IGNORECASE) or
         re.search(r'\breplace\s+(.+?)\s+with\s+(.+)$', text, re.IGNORECASE))
    if m:
        old, new = _strip_quotes(m.group(1)), _strip_quotes(m.group(2))
        # strip a trailing "in caption N" clause out of `new` so it isn't typed into the caption
        new = re.sub(r'\s*\b(?:in|for)\s+caption\s*#?\s*\d+\s*$', '', new, flags=re.IGNORECASE)
        return _apply_word_fix(text, captions, old, new, n)

    return {"ok": False,
            "message": "I couldn't understand that. Try: “fix spelling of 'X' to 'Y'”, "
                        "“change caption 3 to ...”, or “delete caption 5”.",
            "candidates": []}


if __name__ == "__main__":
    caps = [
        {"s": 0.0, "e": 1.2, "t": "This way you can focus"},
        {"s": 1.2, "e": 2.4, "t": "on closing deals instead of"},
        {"s": 2.4, "e": 3.6, "t": "worrying about an emty calendar."},
        {"s": 3.6, "e": 4.8, "t": "so you can focus on what matters"},
    ]
    tests = [
        "fix spelling of 'emty' to 'empty'",
        "change caption 2 to on closing more deals",
        "delete caption 1",
        "'worrying' should say 'excited'",
        "caption 3 should say excited about a full calendar.",
        "replace 'emty' with 'empty'",
        "remove 'about' from caption 3",
        "make the last caption bigger",         # unsupported style op -> not understood
        "delete the caption that says closing deals",
        "fix spelling of 'xyz' to 'abc'",       # not found anywhere
        "change caption 9 to hello",            # out of range
        "fix spelling of 'focus' to 'focused'",  # ambiguous: appears in captions 1 and 4
        "remove 'the ' at 1.5 sec",              # word removal by time, mid-caption
        "at 2 sec change 'closing' to 'sealing'",  # word fix by time
        "remove the caption at 3 sec",           # whole-caption delete by time
        "at 40 sec change 'x' to 'y'",           # time far past the video -> not found
    ]
    for t in tests:
        r = parse_command(t, caps)
        extra = f" | candidates={[c['index']+1 for c in r.get('candidates',[])]}" if r.get('candidates') else ""
        print(t, "->", r["message"] + extra)
