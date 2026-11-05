"""Plain language: posts should read easily for someone who doesn't follow the news.

`plain_notes` finds what makes a post hard to read: newspaper shorthand ("earmarks", "mulls",
"nod"), abbreviations and terms most readers don't know ("SEC", "MPTC", "cusecs") unless they are
explained, legal section numbers, and long sentences. It needs no model; the content creator
sends its notes back to the writer for one rewrite.
"""

from __future__ import annotations

import re

SENTENCE_WORDS_MAX = 22

# Newspaper shorthand -> everyday words.
HEADLINESE = {
    "earmark": "set aside", "earmarks": "sets aside", "earmarked": "set aside", "mulls": "considers",
    "mull": "consider", "slams": "criticises", "slammed": "criticised", "flays": "criticises",
    "raps": "criticises", "eyes": "plans", "nod": "approval", "curbs": "limits", "probe": "investigation",
    "row": "dispute", "amid": "during / as", "sparks": "leads to", "sparked": "led to", "dethroned": "overtook",
    "dethrones": "overtakes", "template": "model", "u-turn": "reversal", "mooted": "proposed", "moots": "proposes",
    "helm": "lead", "spearhead": "lead", "bolster": "strengthen", "bolsters": "strengthens",
    "fillip": "boost", "impetus": "push", "aegis": "support", "inks": "signs", "inked": "signed",
    "dharna": "sit-in protest", "plea": "request to the court", "pleas": "requests to the court",
    "accorded": "given", "apex court": "Supreme Court", "the centre": "the central government",
    "centre's": "the central government's", "acquits": "finds not guilty", "acquitted": "found not guilty",
    "extradite": "bring back to face trial", "extradition": "return to face trial", "stayed": "paused",
}
# Abbreviations and terms readers may not know -> what to say instead (or explain).
JARGON = {
    "SC": "Supreme Court", "HC": "High Court", "SEC": "State Election Commission",
    "EC": "Election Commission", "ECs": "election commissioners", "CEC": "Chief Election Commissioner",
    "MPTC": "local council member", "MPTCs": "local council seats", "ZPTC": "district council member",
    "ZPTCs": "district council seats", "ONOE": "One Nation, One Election", "SACU": "Southern African Customs Union",
    "GRAP": "Delhi's emergency anti-pollution plan", "POCSO": "child sexual abuse law", "Pocso": "child sexual abuse law",
    "UAPA": "anti-terror law", "FIR": "police complaint", "ED": "Enforcement Directorate",
    "CBI": "Central Bureau of Investigation", "RBI": "Reserve Bank of India", "SEBI": "the market regulator",
    "IRDAI": "the insurance regulator", "MeitY": "the IT ministry", "NDA": "BJP-led alliance",
    "TGSRTC": "Telangana's state bus service", "APSRTC": "Andhra Pradesh's state bus service",
    "KSRTC": "the state bus service", "cusecs": "cubic feet of water per second", "Agniveers": "soldiers hired "
    "under the four-year Agnipath scheme", "Agniveer": "soldier hired under the four-year Agnipath scheme",
    "PIL": "public interest petition", "GDP": "size of the economy", "CAG": "the government's auditor",
    "MSP": "minimum support price for crops", "DA": "dearness allowance (cost-of-living pay)",
}
_HEADLINESE_RE = re.compile(r"(?<![\w'])(" + "|".join(sorted(map(re.escape, HEADLINESE), key=len, reverse=True)) + r")(?![\w'])", re.I)
_JARGON_RE = re.compile(r"(?<![\w-])(" + "|".join(sorted(map(re.escape, JARGON), key=len, reverse=True)) + r")(?![\w-])")
_CREDIT_RE = re.compile(r"[,.]?\s+[A-Z][\w.&' -]{1,40}\s+reports?\.?\s*$")
_SECTION_RE = re.compile(r"\b(Section|Article|Clause|Rule)\s+\d+[\w()]*", re.I)


def _explained(text: str, term: str) -> bool:
    """"SEC (State Election Commission)" or the plain words used nearby count as explained."""
    plain = JARGON[term].lower()
    return bool(re.search(re.escape(term) + r"\s*\(", text)) or plain in text.lower()


def plain_notes(text: str, field: str = "") -> list[str]:
    """What makes `text` hard for a general reader; empty when it already reads plainly."""
    where = f"{field}: " if field else ""
    notes = []
    for word in dict.fromkeys(m.group(1).lower() for m in _HEADLINESE_RE.finditer(text)):
        notes.append(f'{where}say "{HEADLINESE[word]}" instead of "{word}"')
    for term in dict.fromkeys(m.group(1) for m in _JARGON_RE.finditer(text)):
        if not _explained(text, term):
            notes.append(f'{where}"{term}" is unclear to most readers: write "{JARGON[term]}" or explain it')
    if _SECTION_RE.search(text):
        notes.append(f"{where}drop legal section numbers; say what the law is about")
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        words = _CREDIT_RE.sub("", sentence).split()  # ", The Hindu reports." is a fixed credit, not reading load
        if len(words) > SENTENCE_WORDS_MAX:
            notes.append(f"{where}split the sentence starting \"{' '.join(words[:5])}…\" "
                         f"({len(words)} words) into shorter ones")
    return notes


def summary(notes: list[str]) -> str:
    """One line per issue, naming the fields it appears in: 'in headline, x_post: say "..."'."""
    fields: dict[str, list[str]] = {}
    for note in notes:
        field, _, issue = note.partition(": ")
        fields.setdefault(issue or field, []).append(field if issue else "")
    return "; ".join(f"in {', '.join(f for f in fs if f)}: {issue}" if any(fs) else issue
                     for issue, fs in fields.items())


def draft_notes(draft: dict) -> list[str]:
    """Plain-language notes for every text a post shows."""
    notes = []
    for field in ("headline", "image_text", "caption", "ig_caption", "x_post"):
        notes += plain_notes(draft.get(field) or "", field)
    return notes
