#!/usr/bin/env python3
"""
Turn the research log's NEXT / OPEN / STUCK items into content/open-questions.md.

    python3 tools/log_to_open_questions.py path/to/ancestry-research-log.md > content/open-questions.md

ASK and DONE items are left out (ASK items are questions for Chris and usually concern
living people). Ancestry person ids and tree URLs are stripped; line codes are kept.
The site build additionally drops any paragraph that names a living person in full.
"""
import re
import sys

ITEM = re.compile(r"^([A-E]\d+)\s+(NEXT|OPEN|STUCK|ASK|DONE)\b\s*(\([^)]*\)|[^—]*?)?\s*—\s*(.*)$")
PHASE = re.compile(r"^###\s+(Phase\s+[A-E].*)$")
KEEP = {"NEXT", "OPEN", "STUCK"}


def clean(text: str) -> str:
    text = re.sub(r"https://www\.ancestry\.com/family-tree/[^\s)]+", "", text)
    text = re.sub(r"\b\d{12}\b", "", text)                                 # Ancestry person ids
    text = re.sub(r"\bA-?\d{7,9}\b", "[number withheld]", text)            # alien registration numbers
    text = re.sub(r"\s*ASK:.*$", "", text)                                   # trailing questions for Chris
    text = re.sub(r"\(\s*[,;]\s*", "(", text)
    text = re.sub(r"\s*[,;]\s*\)", ")", text)
    text = re.sub(r"\(\s*\)", "", text)
    text = re.sub(r"\s+([,.;:)])", r"\1", text)
    text = re.sub(r"\(\s+", "(", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


def main():
    src = open(sys.argv[1], encoding="utf-8").read().splitlines()
    out = ["*Generated from the research log; NEXT, OPEN and STUCK items only.*", ""]
    phase = None
    pending = []
    for line in src:
        m = PHASE.match(line)
        if m:
            phase = m.group(1).split("—")[0].strip()
            pending.append("")
            pending.append(f"## {phase}")
            continue
        m = ITEM.match(line)
        if not m:
            continue
        code, status, qual, body = m.groups()
        if status not in KEEP:
            continue
        if pending:
            out.extend(pending)
            out.append("")
            pending = []
        label = status.lower()
        if qual and qual.strip():
            label += " " + clean(qual.strip())
        out.append(f"- **{code} · {label}** — {clean(body)}")
    print("\n".join(out))


if __name__ == "__main__":
    main()
