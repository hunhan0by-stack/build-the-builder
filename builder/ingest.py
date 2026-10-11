from __future__ import annotations

import csv
import hashlib
import io
import re
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path

from .models import Document, FoundationError, strict_json

TEXT_FORMATS = {".md", ".txt", ".vtt", ".html", ".htm", ".eml", ".json", ".csv", ".tsv"}
PROTECTED_NAMES = {"env.sh", "env.example", "credentials.json", "secrets.json", "token.json"}
PROTECTED_DIRECTORIES = {".freya-local", ".git", ".aws", ".codex", ".agents"}


def admit_path(path: Path) -> Path:
    # Check both supplied and resolved paths so a symlink cannot admit credentials.
    for candidate in (path.absolute(), path.resolve()):
        name = candidate.name.lower()
        parts = {part.lower() for part in candidate.parts}
        if name.startswith(".env") or name in PROTECTED_NAMES or parts & PROTECTED_DIRECTORIES:
            raise FoundationError("Credential/configuration paths are not admitted as sources")
        if re.search(r"(?:^|[._-])(?:api[-_]?key|secret|credential|token)(?:[._-]|$)", name):
            raise FoundationError("Potential credential source refused before reading")
    resolved = path.resolve()
    if not resolved.is_file():
        raise FoundationError(f"Source is not a file: {path.name}")
    return resolved


class VisibleHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if not self.hidden and tag in {"p", "div", "tr", "li", "h1", "h2", "h3", "br"}:
            self.parts.append("\n")
        if not self.hidden and tag in {"td", "th"}:
            self.parts.append(" | ")

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1
        elif not self.hidden and tag in {"p", "div", "tr", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def html_text(text: str) -> str:
    parser = VisibleHTML()
    parser.feed(text)
    return "".join(parser.parts)


def read_source(path: Path, max_bytes: int = 4_000_000) -> Document:
    path = admit_path(path)
    if path.stat().st_size > max_bytes:
        raise FoundationError(f"Source exceeds configured byte limit: {path.name}")
    data = path.read_bytes()
    if len(data) > max_bytes:
        raise FoundationError("Source changed or exceeded the configured byte limit while reading")
    digest = hashlib.sha256(data).hexdigest()
    source_id = "src_" + digest[:16]
    extension = path.suffix.lower()
    if extension not in TEXT_FORMATS:
        return Document(source_id, str(path), digest, len(data), extension,
                        "", "unsupported_modality", ["Provide an extracted text sidecar and review the original visual/audio source."])
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise FoundationError(f"Source is not UTF-8; convert explicitly: {path.name}") from exc
    limitations = []
    if extension in {".html", ".htm"}:
        text = html_text(text)
        limitations.append("Visible HTML text only; visual layout and hidden metadata require separate review.")
    elif extension == ".eml":
        message = BytesParser(policy=policy.default).parsebytes(data)
        headers = "\n".join(f"{name}: {message.get(name, '')}" for name in ("From", "To", "Date", "Subject", "Message-ID"))
        bodies = []
        for part in message.walk():
            if part.get_content_disposition() == "attachment":
                limitations.append("An email attachment was not parsed; supply it separately.")
            elif part.get_content_type() == "text/plain":
                bodies.append(part.get_content())
        if not bodies:
            body = message.get_body(preferencelist=("html",))
            if body:
                bodies.append(html_text(body.get_content()))
        text = headers + "\n\n" + "\n\n".join(bodies)
    elif extension == ".json":
        strict_json(text)  # Preserve source text and pointer structure; do not execute it.
    elif extension in {".csv", ".tsv"}:
        list(csv.reader(io.StringIO(text), delimiter="\t" if extension == ".tsv" else ",", strict=True))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if "\x00" in text:
        raise FoundationError(f"Unexpected NUL bytes in text source: {path.name}")
    return Document(source_id, str(path), digest, len(data), extension, text,
                    "processed" if text.strip() else "empty", limitations)


def collect_sources(paths: list[Path]) -> list[Document]:
    documents = []
    seen_paths = set()
    for path in paths:
        document = read_source(path)
        if document.path in seen_paths:
            raise FoundationError("The same source path was supplied twice")
        seen_paths.add(document.path)
        documents.append(document)
    # Equal content is deduplicated for extraction, but all paths stay in inventory.
    return documents


def line_spans(document: Document) -> list[dict]:
    records = []
    offset = 0
    for number, line in enumerate(document.text.splitlines(keepends=True), 1):
        if line.strip():
            records.append({"source_id": document.source_id, "line": number,
                            "start": offset, "end": offset + len(line), "text": line.rstrip("\n")})
        offset += len(line)
    return records
