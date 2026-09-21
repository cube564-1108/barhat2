"""
Markdown → PDF для инструкций из docs/.

Pandoc и wkhtmltopdf на рабочей машине нет, зато есть Chrome: он печатает
HTML в PDF в headless-режиме и сам расставляет разрывы страниц. Поэтому путь
такой: md → html (этот файл) → chrome --print-to-pdf.

Разбор markdown здесь СВОЙ и намеренно неполный: инструкции пишутся ограниченным
набором (заголовки, списки, таблицы, жирный, разделитель), и тянуть ради них
зависимость из сети незачем. Если в документе появится что-то ещё — синтаксис
просто уедет в PDF как есть, и это видно глазами при первом же просмотре.

Шрифты — системные. Vollkorn/Inter из DESIGN-SPEC потребовали бы загрузки с
Google Fonts в момент печати: документ должен собираться без сети.

Запуск:
    python scripts/md_to_pdf.py docs/инструкция-курьера.md
    python scripts/md_to_pdf.py docs/инструкция-курьера.md --out путь.pdf
"""

import html
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

# Цвета из DESIGN-SPEC.md (--bx-*), чтобы печатный документ не расходился с
# интерфейсом.
CSS = """
@page { size: A4; margin: 18mm 16mm 16mm; }
body {
    font-family: "Segoe UI", "PT Sans", system-ui, sans-serif;
    font-size: 11pt; line-height: 1.55; color: #2b1e28; margin: 0;
}
h1, h2, h3 { font-family: Georgia, "Vollkorn", serif; color: #411330; line-height: 1.25; }
h1 { font-size: 24pt; margin: 0 0 6pt; }
h2 { font-size: 15pt; margin: 20pt 0 8pt; padding-bottom: 4pt;
     border-bottom: 1px solid #e7d7e2; break-after: avoid; }
h3 { font-size: 12.5pt; margin: 14pt 0 6pt; break-after: avoid; }
p { margin: 0 0 8pt; }
ul, ol { margin: 0 0 8pt; padding-left: 18pt; }
li { margin-bottom: 4pt; }
strong { color: #411330; }
hr { border: 0; border-top: 1px solid #e7d7e2; margin: 16pt 0; }
table { border-collapse: collapse; width: 100%; margin: 0 0 10pt; font-size: 10.5pt; }
th, td { border: 1px solid #e7d7e2; padding: 5pt 8pt; text-align: left; vertical-align: top; }
th { background: #faf4f9; color: #411330; font-weight: 600; }
img { max-width: 100%; }
code { font-family: Consolas, monospace; font-size: 10pt;
       background: #faf4f9; padding: 1pt 3pt; border-radius: 3px; }
/* Разрыв страницы не должен отрывать заголовок от текста или рвать таблицу */
table, li { break-inside: avoid; }
"""


def inline(text):
    """**жирный**, `код`, [текст](ссылка) и экранирование всего остального."""
    out = html.escape(text)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"`(.+?)`", r"<code>\1</code>", out)
    out = re.sub(r"!\[(.*?)\]\((.+?)\)", r'<img src="\2" alt="\1">', out)
    out = re.sub(r"\[(.+?)\]\((.+?)\)", r'<a href="\2">\1</a>', out)
    return out


def split_row(line):
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def convert(md):
    lines = md.splitlines()
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            out.append(f"<h{level}>{inline(stripped[level:].strip())}</h{level}>")
            i += 1
            continue

        if re.fullmatch(r"-{3,}|\*{3,}", stripped):
            out.append("<hr>")
            i += 1
            continue

        # Таблица: строка с | и следующая из дефисов-разделителей
        if stripped.startswith("|") and i + 1 < len(lines) \
                and re.fullmatch(r"[\s|:-]+", lines[i + 1].strip()) \
                and "|" in lines[i + 1]:
            header = split_row(stripped)
            i += 2
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(split_row(lines[i].strip()))
                i += 1
            cells = "".join(f"<th>{inline(c)}</th>" for c in header)
            body = "".join(
                "<tr>" + "".join(f"<td>{inline(c)}</td>" for c in row) + "</tr>"
                for row in rows
            )
            out.append(f"<table><thead><tr>{cells}</tr></thead><tbody>{body}</tbody></table>")
            continue

        # Список: маркированный или нумерованный, до первой пустой строки
        bullet = re.match(r"[-*]\s+(.*)", stripped)
        number = re.match(r"\d+[.)]\s+(.*)", stripped)
        if bullet or number:
            tag = "ul" if bullet else "ol"
            items = []
            pattern = r"[-*]\s+(.*)" if bullet else r"\d+[.)]\s+(.*)"
            while i < len(lines) and lines[i].strip():
                match = re.match(pattern, lines[i].strip())
                if match:
                    items.append(match.group(1))
                elif items:
                    # Продолжение предыдущего пункта на новой строке
                    items[-1] += " " + lines[i].strip()
                else:
                    break
                i += 1
            body = "".join(f"<li>{inline(item)}</li>" for item in items)
            out.append(f"<{tag}>{body}</{tag}>")
            continue

        # Абзац
        chunk = []
        while i < len(lines) and lines[i].strip() \
                and not lines[i].strip().startswith(("#", "|", "-", "*")):
            chunk.append(lines[i].strip())
            i += 1
        if chunk:
            out.append("<p>" + inline(" ".join(chunk)) + "</p>")
        else:
            out.append("<p>" + inline(stripped) + "</p>")
            i += 1

    return "\n".join(out)


def find_chrome():
    for path in CHROME_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


def main():
    args = [a for a in sys.argv[1:]]
    if not args:
        print(__doc__)
        return 1

    source = Path(args[0]).resolve()
    out_pdf = Path(args[args.index("--out") + 1]).resolve() if "--out" in args \
        else source.with_suffix(".pdf")

    chrome = find_chrome()
    if not chrome:
        print("Не нашёл Chrome или Edge — печатать нечем.")
        return 2

    md = source.read_text(encoding="utf-8")
    title = html.escape(source.stem)
    page = (
        '<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">'
        f"<title>{title}</title><style>{CSS}</style></head><body>"
        f"{convert(md)}</body></html>"
    )

    # HTML кладём РЯДОМ с исходником: относительные ссылки на картинки
    # (docs/img/...) должны разрешаться от той же папки, иначе в PDF уедут
    # пустые рамки вместо скриншотов.
    tmp_html = source.with_name(f".{source.stem}.print.html")
    tmp_html.write_text(page, encoding="utf-8")

    profile = tempfile.mkdtemp(prefix="md2pdf-")
    try:
        result = subprocess.run([
            chrome,
            "--headless",
            "--disable-gpu",
            "--no-pdf-header-footer",
            f"--user-data-dir={profile}",
            f"--print-to-pdf={out_pdf}",
            tmp_html.as_uri(),
        ], capture_output=True, text=True, timeout=120)
    finally:
        tmp_html.unlink(missing_ok=True)

    if not out_pdf.exists():
        print("Chrome не создал файл.")
        print(result.stderr[-2000:])
        return 3

    print(f"Готово: {out_pdf} ({out_pdf.stat().st_size // 1024} КБ)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
