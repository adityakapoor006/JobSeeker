"""Turns tailored resume markdown into a clean .docx."""
import re
from pathlib import Path

import docx
from docx.shared import Pt


def markdown_to_docx(text: str, path: Path) -> None:
    document = docx.Document()
    document.styles["Normal"].font.name = "Calibri"
    document.styles["Normal"].font.size = Pt(10.5)
    for line in text.splitlines():
        line = line.strip().replace("**", "")
        if not line:
            continue
        if line.startswith("# "):
            document.add_heading(line[2:], 0)
        elif line.startswith("## "):
            document.add_heading(line[3:], 1)
        elif line.startswith("### "):
            document.add_heading(line[4:], 2)
        elif re.match(r"[-*] ", line):
            document.add_paragraph(line[2:], style="List Bullet")
        else:
            document.add_paragraph(line)
    document.save(path)
