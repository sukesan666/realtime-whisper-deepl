"""Pure transcript text helpers: cleaning, prompt building and history merging.

Everything here is side-effect free and unit tested, independent of Tkinter,
audio or Whisper backends.
"""

from __future__ import annotations

import re

_MUSIC_NOTE_RE = re.compile(r"[♨♩♪♫♬♭♮♯]+")
_BRACKET_RE = re.compile(r"\s*[\[({][^\[\](){}]*[\])}]\s*")
_ASTERISK_RE = re.compile(r"\*[^*]*\*")
_WS_RE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    return " ".join(text.lower().strip().split())


def _strip_bracketed(text: str) -> str:
    # Iteratively remove innermost bracketed / parenthesised groups so that
    # nested annotations like "[(applause)]" are fully removed.
    prev: str | None = None
    cur = text
    while prev != cur:
        prev = cur
        cur = _BRACKET_RE.sub(" ", cur)
    return cur


def clean_transcript_text(text: str) -> str:
    """Remove non-speech annotations and hallucinated asides from a transcript.

    Whisper and whisper.cpp routinely insert bracketed or parenthesised markers
    such as ``[BLANK_AUDIO]``, ``[Music]``, ``(applause)``, ``*laughs*`` or runs
    of musical notes. These are never real speech; if kept they poison the next
    prompt and fragment the on-screen transcript, so we strip them wherever they
    appear (not only at the end of the line).
    """
    if not text:
        return ""
    cleaned = _MUSIC_NOTE_RE.sub(" ", text)
    cleaned = _strip_bracketed(cleaned)
    cleaned = _ASTERISK_RE.sub(" ", cleaned)
    cleaned = _WS_RE.sub(" ", cleaned).strip()
    cleaned = cleaned.strip(" -–—•·")
    if not re.search(r"[0-9A-Za-zÀ-ɏ]", cleaned):
        # Nothing but leftover punctuation -> treat as empty.
        return ""
    return cleaned


def build_initial_prompt(text: str) -> str | None:
    cleaned = clean_transcript_text(text)
    if not cleaned:
        return None
    parts = re.split(r"(?<=[.!?])\s+", cleaned)
    tail = parts[-1].strip() if parts else cleaned
    if len(tail) < 24 and len(cleaned) > len(tail):
        tail = cleaned[-80:].strip()
    tail = tail[-120:].strip()
    return tail or None


def _merge_on_overlap(a: str, b: str, min_tokens: int = 3) -> str | None:
    """Splice *a* and *b* into one line when the end of *a* overlaps *b*'s start.

    Consecutive sliding-window transcriptions share most of their audio, so a
    long utterance shows up as ``"...brown fox"`` followed by ``"brown fox
    jumped..."``. Stitching them on the shared token run keeps the sentence
    intact instead of cutting it at the window boundary.
    """
    a_tokens = normalize_text(a).split()
    b_tokens = normalize_text(b).split()
    b_words = b.split()
    if len(b_words) != len(b_tokens):
        return None
    max_k = min(len(a_tokens), len(b_tokens))
    for k in range(max_k, min_tokens - 1, -1):
        if a_tokens[-k:] == b_tokens[:k]:
            tail = " ".join(b_words[k:]).strip()
            return a.rstrip() if not tail else f"{a.rstrip()} {tail}"
    return None


def _is_restatement(a: str, b: str, threshold: float = 0.8) -> bool:
    """True when *a* and *b* are two ASR variants of essentially the same text."""
    a_set = set(normalize_text(a).split())
    b_set = set(normalize_text(b).split())
    if not a_set or not b_set:
        return False
    shorter = min(len(a_set), len(b_set))
    if shorter < 5:
        return False
    return len(a_set & b_set) / shorter >= threshold


def _append_or_split(history: list[str], line: str, max_len: int = 500) -> None:
    if len(line) <= max_len:
        history[-1] = line
        return
    idx = max(line.rfind(". "), line.rfind("! "), line.rfind("? "))
    if idx > max_len // 2:
        history[-1] = line[: idx + 1].strip()
        history.append(line[idx + 2 :].strip())
    else:
        history[-1] = line[-max_len:].strip()


def integrate_transcript(history: list[str], text: str, keep_last: int = 30) -> list[str]:
    """Merge a freshly transcribed line into the running transcript history.

    Covers the failure modes of the sliding-window pipeline: exact duplicates,
    prefix extensions of the current line, near-identical ASR restatements, and
    window boundaries where one sentence must be stitched across two windows.
    """
    text = text.strip()
    if not text:
        return history
    if not history:
        history.append(text)
        return history[-keep_last:]
    last = history[-1]
    last_norm = normalize_text(last)
    text_norm = normalize_text(text)
    if text_norm == last_norm:
        pass
    elif text_norm.startswith(last_norm):
        history[-1] = text
    elif last_norm.startswith(text_norm):
        pass
    else:
        merged = _merge_on_overlap(last, text)
        if merged is not None:
            _append_or_split(history, merged)
        elif _is_restatement(last, text):
            if len(text) >= len(last):
                history[-1] = text
        else:
            history.append(text)
    return history[-keep_last:]
