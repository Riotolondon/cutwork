"""Find and score clip-worthy moments from a word-level transcript.

The scorer is a free, local rule-based heuristic. It is a first pass, not an
oracle: review the list, and use the "Ask Claude" round trip for sharper picks.
"""
import bisect
import re

HOOK_PHRASES = [
    "here's why", "here is why", "the truth", "nobody", "no one", "the secret",
    "the problem", "the reason", "what if", "imagine", "the biggest", "most people",
    "you need to", "let me tell you", "i was wrong", "turns out", "did you know",
    "this is why", "the real", "the craziest", "i think the", "the mistake",
]
QUESTION_START = {
    "what", "why", "how", "who", "when", "where", "is", "are", "do", "does", "did",
    "can", "could", "would", "should", "have", "has", "will",
}
STRONG = {
    "never", "always", "biggest", "crazy", "insane", "secret", "mistake", "truth",
    "wrong", "worst", "best", "huge", "massive", "dangerous", "impossible",
    "incredible", "shocking", "nobody", "everyone", "everything", "nothing",
    "fastest", "first", "billion", "million", "trillion", "terrifying", "wild",
}
CONTRAST = {"but", "actually", "however", "instead", "except", "although"}
DEPENDENT_START = {
    "and", "but", "so", "because", "which", "or", "then", "also", "yeah", "right",
    "um", "uh", "like", "well", "okay", "ok",
}
DEFAULT_KEYWORDS = (
    "ai agi gpt chatgpt claude gemini openai anthropic nvidia model models agent "
    "agents robot robots startup chip chips gpu code coding software data algorithm "
    "automation jobs future tesla apple google microsoft meta llm neural compute "
    "open-source founder product launch"
)


def _clean(w):
    return re.sub(r"[^a-z0-9'\-]", "", w.lower())


def build_sentences(words):
    sents, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        gap = (nxt["s"] - w["e"]) if nxt else 99
        if w["w"][-1:] in ".?!" or gap > 1.2 or len(cur) >= 40:
            sents.append({
                "s": cur[0]["s"], "e": cur[-1]["e"],
                "text": " ".join(x["w"] for x in cur),
                "n": len(cur), "gap_after": gap,
            })
            cur = []
    return sents


def _score(sents, i, j, keywords):
    first, last = sents[i], sents[j]
    text = " ".join(s["text"] for s in sents[i:j + 1])
    toks = [_clean(t) for t in text.split()]
    toks = [t for t in toks if t]
    dur = last["e"] - first["s"]
    reasons = []

    # --- hook: how the clip opens (0-35)
    f_toks = [_clean(t) for t in first["text"].split()]
    f_low = first["text"].lower()
    hook = 0
    if "?" in first["text"] or (f_toks and f_toks[0] in QUESTION_START):
        hook += 14; reasons.append("Opens with a question")
    if any(p in f_low for p in HOOK_PHRASES):
        hook += 12; reasons.append("Strong hook phrase")
    if {"you", "your", "you're"} & set(f_toks):
        hook += 5
    if re.search(r"\d", first["text"]):
        hook += 5
    if first["n"] <= 14:
        hook += 5
    if f_toks and f_toks[0] in DEPENDENT_START:
        hook -= 12; reasons.append("Starts mid-thought (check the start)")
    hook = max(0, min(35, hook))

    # --- content (0-35)
    kw = sum(1 for t in toks if t in keywords)
    nums = len(re.findall(r"\b\d[\d,.]*%?", text))
    strong = sum(1 for t in toks if t in STRONG)
    contrast = sum(1 for t in toks if t in CONTRAST)
    content = min(15, kw * 3) + min(8, nums * 4) + min(8, strong * 3) + min(4, contrast * 2)
    if kw >= 2: reasons.append("On-niche keywords")
    if nums: reasons.append("Has a number or stat")
    if strong >= 2: reasons.append("Punchy language")

    # --- ending (0-15)
    ending = 0
    if last["text"][-1:] in ".?!": ending += 6
    if last["gap_after"] >= 0.5: ending += 5
    if last["n"] <= 20: ending += 4
    if ending >= 11: reasons.append("Clean ending")

    # --- pace (0-15)
    wps = len(toks) / max(dur, 1)
    pace = 10 if 2.2 <= wps <= 3.8 else max(0, 10 - abs(wps - 3.0) * 6)
    max_gap = max((s["gap_after"] for s in sents[i:j]), default=0)
    if max_gap < 1.5: pace += 5
    else: reasons.append("Has a long pause inside")

    return max(0, min(100, round(hook + content + ending + pace))), reasons, text


def find_moments(words, min_len=20, max_len=45, count=12, keywords=None):
    kws = set((keywords or DEFAULT_KEYWORDS).lower().replace(",", " ").split())
    sents = build_sentences(words)
    cands = []
    for i in range(len(sents)):
        best = None
        for j in range(i, len(sents)):
            dur = sents[j]["e"] - sents[i]["s"]
            if dur > max_len:
                break
            if dur >= min_len:
                sc, reasons, text = _score(sents, i, j, kws)
                if best is None or sc > best[0]:
                    best = (sc, reasons, text, j)
        if best:
            cands.append((best[0], i, best[3], best[1], best[2]))
    cands.sort(key=lambda c: -c[0])
    chosen = []
    for sc, i, j, reasons, text in cands:
        s, e = sents[i]["s"], sents[j]["e"]
        if all(e <= c["start"] + 2 or s >= c["end"] - 2 for c in chosen):
            chosen.append({
                "start": round(max(0, s - 0.15), 2), "end": round(e + 0.25, 2),
                "score": sc, "reasons": reasons, "text": text, "hook": "",
                "source": "auto",
            })
        if len(chosen) >= count:
            break
    for n, m in enumerate(chosen, 1):
        m["id"] = n
    return chosen


def claude_prompt(words, min_len=20, max_len=45, count=10):
    lines = [f"[{s['s']:.1f}] {s['text']}" for s in build_sentences(words)]
    return (
        "You are helping me clip a long video for my Tech & AI short-form page (CUTWORK).\n"
        f"Below is the transcript; each line starts with its time in seconds.\n"
        f"Pick the {count} best moments for Reels/TikTok/Shorts, each {min_len}-{max_len} seconds long.\n"
        "Each must make sense on its own, open with a strong first line, and end on a complete thought.\n"
        "Reply with ONLY a JSON array, no other text, in this exact shape:\n"
        '[{"start": 123.4, "end": 158.0, "hook": "short on-screen title, max 8 words", '
        '"reason": "why this will hold attention"}]\n\n'
        "TRANSCRIPT:\n" + "\n".join(lines)
    )


def import_moments(items, words):
    """Snap pasted moments to real word boundaries."""
    starts = [w["s"] for w in words]
    ends = [w["e"] for w in words]
    out = []
    for n, it in enumerate(items, 1):
        try:
            s, e = float(it["start"]), float(it["end"])
        except (KeyError, TypeError, ValueError):
            continue                      # skip an entry that has no usable times, keep the rest
        if s != s or e != e or s < 0:
            continue
        if e < s:
            s, e = e, s
        if words:
            k = min(bisect.bisect_left(starts, s), len(starts) - 1)
            if k > 0 and abs(starts[k - 1] - s) < abs(starts[k] - s): k -= 1
            if abs(starts[k] - s) <= 1.5: s = max(0, starts[k] - 0.15)
            k = min(bisect.bisect_left(ends, e), len(ends) - 1)
            if k > 0 and abs(ends[k - 1] - e) < abs(ends[k] - e): k -= 1
            if abs(ends[k] - e) <= 1.5: e = ends[k] + 0.25
        if e - s < 3:
            continue
        text = " ".join(w["w"] for w in words if w["s"] >= s - 0.2 and w["e"] <= e + 0.2)
        reason = str(it.get("reason") or "").strip()[:200]
        try:
            score = max(0, min(100, int(float(it.get("score") or 0)))) or None
        except (TypeError, ValueError):
            score = None
        out.append({
            "id": n, "start": round(s, 2), "end": round(e, 2),
            "score": score,
            "reasons": [reason] if reason else [], "text": text,
            "hook": str(it.get("hook") or "").strip()[:70], "source": "claude",
        })
    return out
