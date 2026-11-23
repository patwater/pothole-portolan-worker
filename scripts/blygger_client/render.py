"""Turn a queued dossier draft (queue/drafts/<slug>-v<n>.md) into Markdown a Blygger Studio renders.

The Studio renders Markdown with markdown-it, raw HTML off and no footnote plugin (checked in the
blygger-studio source, src/markdown.ts, on 2026-11-19). Tables render; footnotes do not. So:

- The YAML front matter is removed and returned separately.
- Footnote references "[^label]" become "[n]", numbered in order of first reference.
- Footnote definitions become a "## Notes" section at the end, one paragraph per note, each
  starting with its number in bold. Every source URL, retrieval date, archive path, method and
  caveat in the note is kept as written.
- When archive_base is given, each archive path written as `sources/<sha256>.<ext>` becomes a link
  to archive_base + path, so a reader can open the archived copy (CLAUDE.md rule 1).
- A "$" in prose is escaped as "\\$", because the Studio's math plugin reads "$...$" as TeX. Dollars
  inside code spans, autolinks and link destinations (the Socrata query URLs) are left as written.
- A disclaimer (DISCLAIMER below) that the dossier was written with AI automation, is published
  as is and may contain inaccuracies is placed under the title of every dossier.
- The text is put in Unicode Normalization Form C, and control characters other than tab, line
  feed and carriage return are refused, as the protocol requires of content_md (spec 0.4).

A reference without a definition, or a definition never referenced, stops the render, so a number
cannot lose its source on the way to the blyg.
"""
import re
import unicodedata

FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)
DEFINITION = re.compile(r"^\[\^([^\]\s]+)\]:[ \t]?(.*)$")
REFERENCE = re.compile(r"\[\^([^\]\s]+)\](?!:)")
ARCHIVE = re.compile(r"`(sources/[0-9a-f]{64}\.[A-Za-z0-9]+)`")
BAD_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class RenderError(ValueError):
    pass


# Added to every dossier sent to the Studio, right under its title (supervisor's instruction,
# 2026-11-23). It lives here rather than in each dossier so that no published dossier can omit it.
DISCLAIMER = (
    "*This dossier was researched and written with AI automation and is published as is. "
    "It may contain inaccuracies. Every figure cites its source, retrieval date and archived copy "
    "in the Notes, so please check a number against its source before relying on it.*"
)


def add_disclaimer(body):
    """Insert DISCLAIMER as its own paragraph after the first level-one heading, or at the top."""
    lines = body.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("# "):
            return "\n".join(lines[:i + 1] + ["", DISCLAIMER] + lines[i + 1:])
    return DISCLAIMER + "\n\n" + body


# Spans whose dollars must stay as written: code spans, autolinks and link destinations. Any other
# "$" is escaped, because the Studio reads "$...$" as a TeX formula (pandoc tex_math_dollars,
# src/math.ts), and "\$" is markdown's own escape for a literal dollar sign.
_KEEP = re.compile(r"(`+)[^`]*?\1|<[A-Za-z][A-Za-z0-9+.\-]{1,31}:[^<>\s]*>|\]\([^)\s]*\)|\\\$")


def escape_dollars(text):
    out, in_fence = [], False
    for line in text.split("\n"):
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        pieces, pos = [], 0
        for m in _KEEP.finditer(line):
            pieces.append(line[pos:m.start()].replace("$", "\\$"))
            pieces.append(m.group(0))
            pos = m.end()
        pieces.append(line[pos:].replace("$", "\\$"))
        out.append("".join(pieces))
    return "\n".join(out)


def split_front_matter(text):
    """Return (front_matter_text_or_None, body)."""
    text = text.replace("\r\n", "\n")
    m = FRONT_MATTER.match(text)
    if not m:
        return None, text
    return m.group(1), text[m.end():]


def _collect_definitions(body):
    """Return (body_without_definitions, {label: text}, [labels in definition order]).

    A definition may continue on following lines indented by four spaces or a tab."""
    lines = body.split("\n")
    kept, defs, order = [], {}, []
    current = None
    for line in lines:
        m = DEFINITION.match(line)
        if m:
            label = m.group(1)
            if label in defs:
                raise RenderError(f"footnote [^{label}] is defined twice")
            defs[label] = m.group(2).strip()
            order.append(label)
            current = label
            continue
        if current is not None and (line.startswith("    ") or line.startswith("\t")):
            defs[current] = (defs[current] + " " + line.strip()).strip()
            continue
        current = None
        kept.append(line)
    return "\n".join(kept), defs, order


def to_blyg_markdown(text, archive_base=None):
    """Return (front_matter_text_or_None, markdown) for posting to a Studio."""
    front, body = split_front_matter(text)
    body, defs, def_order = _collect_definitions(body)

    numbers = {}

    def number(m):
        label = m.group(1)
        if label not in defs:
            raise RenderError(f"footnote reference [^{label}] has no definition")
        if label not in numbers:
            numbers[label] = len(numbers) + 1
        # Escaped brackets render as "[n]" and can never be read as a link.
        return f"\\[{numbers[label]}\\]"

    body = REFERENCE.sub(number, body)
    unused = [label for label in def_order if label not in numbers]
    if unused:
        raise RenderError("footnotes defined but never referenced: " + ", ".join(unused))

    # Definitions may reference other notes; resolve those too, against the same numbering.
    notes = []
    for label, n in sorted(numbers.items(), key=lambda kv: kv[1]):
        note = REFERENCE.sub(number, defs[label])
        notes.append(f"**\\[{n}\\]** {note}")

    body = body.rstrip("\n")
    if notes:
        body += "\n\n## Notes\n\n" + "\n\n".join(notes)
    body += "\n"

    if archive_base:
        base = archive_base if archive_base.endswith("/") else archive_base + "/"
        body = ARCHIVE.sub(lambda m: f"[`{m.group(1)}`]({base}{m.group(1)})", body)

    body = escape_dollars(body)
    body = add_disclaimer(body)
    body = unicodedata.normalize("NFC", body)
    bad = BAD_CONTROL.search(body)
    if bad:
        raise RenderError(f"control character U+{ord(bad.group()):04X} is not allowed in content_md")
    return front, body
