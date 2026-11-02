"""Resume loading. The newest file in data/resume is the source of truth."""
import hashlib
from pathlib import Path

import docx
import pypdf

from .config import DATA_DIR, RESUME_DIR

FACTS_PATH = DATA_DIR / "verified_facts.md"

ALLOWED = {".docx", ".pdf", ".txt", ".md"}


def latest_resume() -> Path:
    files = [p for p in RESUME_DIR.glob("*") if p.suffix.lower() in ALLOWED]
    if not files:
        raise FileNotFoundError(f"No resume found in {RESUME_DIR}")
    return max(files, key=lambda p: p.stat().st_mtime)


def read_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        document = docx.Document(path)
        lines = [p.text for p in document.paragraphs if p.text.strip()]
        for table in document.tables:
            lines += [" | ".join(c.text for c in row.cells) for row in table.rows]
        return "\n".join(lines)
    if suffix == ".pdf":
        return "\n".join(page.extract_text() or "" for page in pypdf.PdfReader(path).pages)
    return path.read_text()


def load_resume() -> tuple[str, str]:
    """Return the resume text and a hash used to detect changes."""
    text = read_text(latest_resume())
    if FACTS_PATH.exists():
        text += "\n\nADDITIONAL VERIFIED DETAIL FROM EARLIER RESUME VERSIONS\n" + FACTS_PATH.read_text()
    return text, hashlib.sha256(text.encode()).hexdigest()[:16]
