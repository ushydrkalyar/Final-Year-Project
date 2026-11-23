"""Filler detection: the one list of what may be cut from speech automatically.

Hesitation sounds are always cut. A phrase ("you know", "I mean") is cut only when the
speaker set it off: punctuation or a boundary on both sides, and it is not a whole
sentence by itself. Ordinary vocabulary ("like", "so", "aur") is never cut
automatically, so a word only belongs here if it means nothing else in that language
("er" and "um" are real words in German, "um" in Portuguese).
"""
import unicodedata

# Unambiguous in any language
_COMMON = {'hm', 'hmm', 'hmmm', 'umm', 'ummm', 'uhh', 'uhhh', 'uhm'}

HESITATIONS_BY_LANGUAGE = {
    'en': {'um', 'uh', 'er', 'err', 'erm', 'ah', 'ahh'},
    # Whisper writes Urdu, Hindi and Arabic in their own script, sometimes romanised
    'ur': {'um', 'uh', 'امم', 'اممم', 'ہمم', 'ہممم'},
    'hi': {'um', 'uh', 'उम्म', 'अम्म', 'हम्म'},
    'ar': {'امم', 'اممم', 'همم', 'هممم', 'اهه', 'ممم'},
    'de': {'äh', 'ähm', 'öhm'},
    'fr': {'euh', 'euhh'},
}

PHRASE_FILLERS_BY_LANGUAGE = {
    'en': {'you know', 'i mean', 'kind of', 'sort of', 'i guess'},
}

MERGE_GAP_S = 0.15      # fillers this close, with no word between, become one cut

_SENTENCE_END = tuple('.!?…؟।۔')
_SET_OFF = _SENTENCE_END + tuple(',;:—–-،؛')
_STRIP = ''.join(_SET_OFF) + '"\'()[]“”‘’«»¿¡ '


def _normal(text: str) -> str:
    return unicodedata.normalize('NFC', text)


def _clean(token: str) -> str:
    return _normal(token).strip(_STRIP).casefold()


def hesitations(language: str) -> set:
    return _COMMON | {_normal(w) for w in HESITATIONS_BY_LANGUAGE.get(language, ())}


def is_hesitation(token: str, language: str) -> bool:
    token = str(token)
    if token.strip(_STRIP) == 'ER':     # the emergency room, not the sound
        return False
    return _clean(token) in hesitations(language)


def _is_set_off(tokens: list, first: int, last: int, language: str) -> bool:
    """Whether tokens[first:last + 1] is an aside rather than part of the sentence."""
    if not tokens[last].endswith(_SET_OFF) or any(tokens[k].endswith(_SET_OFF) for k in range(first, last)):
        return False
    before = tokens[first - 1] if first else None
    starts_sentence = before is None or before.endswith(_SENTENCE_END)
    if starts_sentence:
        return not tokens[last].endswith(_SENTENCE_END)     # "I guess." is an answer
    return before.endswith(_SET_OFF) or is_hesitation(before, language)


def find_fillers(transcript: dict, language: str | None = None) -> list[dict]:
    """Every filler in the transcript: ``{text, start, end, first, last}`` (word indices)."""
    language = language or transcript.get('language') or 'en'
    words = [w for seg in transcript.get('segments', []) for w in seg.get('words', [])]
    tokens = [(w.get('word') or '').strip() for w in words]
    cleaned = [_clean(t) for t in tokens]
    phrases = [p.split() for p in PHRASE_FILLERS_BY_LANGUAGE.get(language, ())]

    found, i = [], 0
    while i < len(words):
        size = next((len(p) for p in phrases
                     if cleaned[i:i + len(p)] == p and _is_set_off(tokens, i, i + len(p) - 1, language)), 0)
        if not size and is_hesitation(tokens[i], language):
            size = 1
        if size:
            start, end = words[i].get('start', 0.0), words[i + size - 1].get('end', 0.0)
            if end > start:     # skip zero-length Whisper quirks
                found.append({'text': ' '.join(cleaned[i:i + size]), 'start': start, 'end': end,
                              'first': i, 'last': i + size - 1})
        i += size or 1
    return found


def detect_fillers(transcript: dict, language: str | None = None) -> list[tuple[float, float]]:
    """The ``(start, end)`` ranges to cut, separate from pauses; merged in get_keep_segments."""
    cuts, previous = [], None
    for f in find_fillers(transcript, language):
        start, end = round(f['start'], 3), round(f['end'], 3)
        if previous and f['first'] == previous['last'] + 1 and start - cuts[-1][1] <= MERGE_GAP_S:
            cuts[-1] = (cuts[-1][0], end)
        else:
            cuts.append((start, end))
        previous = f
    if cuts:
        print(f"   ✅ Filler cuts: {len(cuts)} region(s)")
    return cuts
