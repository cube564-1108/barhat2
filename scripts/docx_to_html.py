"""Конвертер .docx -> HTML для модуля «Регламенты» в Пульсе.

Зачем: поле «Регламенты» принимает HTML-код, а HTML картинки не хранит — он на
них только ссылается. Отсюда два режима:

    inline  — фото вшиты в сам HTML (data:image/jpeg;base64,...).
              Регламент самодостаточен: скопировал код в поле — фото на месте.
              Платим размером: байты картинки в base64 занимают на треть больше.

    files   — фото выгружаются файлами в отдельную папку, в HTML идут ссылки
              на --base-url. Код лёгкий, фото можно менять не трогая регламент,
              но файлы надо где-то разместить (и не на эфемерном /app).

Фото из Word — это, как правило, снимок с телефона на 3-5 МБ и 4000x3000. Если
установлен Pillow, картинка уменьшается до --max-width по длинной стороне и
жмётся в JPEG: иначе один снимок превращается в 5,5 МБ base64, и поле его не
примет. Ориентация берётся из EXIF — без этого вертикальный кадр ляжет набок, и
видно это будет только после вставки в Пульс. Без Pillow картинки идут как есть,
и скрипт об этом предупреждает.

Использование:
    python scripts/docx_to_html.py "путь/к/инструкции.docx"
    python scripts/docx_to_html.py "файл.docx" --mode files \
        --base-url https://barhat2-cube564.amvera.io/static/reglament/courier
"""

from __future__ import annotations

import argparse
import base64
import html
import io
import os
import re
import sys

try:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph
except ImportError:  # pragma: no cover - подсказка вместо traceback
    sys.exit("Нет библиотеки python-docx. Установите: pip install python-docx")

try:
    from PIL import Image, ImageOps
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
}

HEADING_RE = re.compile(r"^(?:Heading|Заголовок)\s*(\d)", re.IGNORECASE)
LIST_RE = re.compile(r"(List Paragraph|Список|Bullet|Number)", re.IGNORECASE)


def shrink(data: bytes, max_side: int, quality: int, colors: int) -> tuple[bytes, str, str]:
    """Уменьшает и пережимает картинку. При любом сбое возвращает оригинал.

    Считаем ДВА варианта и берём тот, что легче: JPEG и PNG с палитрой. В
    инструкциях почти всегда скриншоты интерфейса, а не снимки с телефона, и
    для них JPEG — плохой выбор: он мылит мелкий текст ровно там, где человек
    ищет нужную кнопку. Палитра на 256 цветов даёт такому скриншоту чёткие
    буквы и файл меньше JPEG. Для настоящего фото всё наоборот, поэтому выбор
    не по типу исходника, а по результату: PNG берётся, если он не тяжелее
    JPEG больше чем на четверть.

    Возврат оригинала обязателен: пустой или битый файл выглядит как успешная
    конвертация, а обнаруживается уже в Пульсе дырой вместо фото.
    """
    if not HAVE_PIL:
        return data, "png", "без сжатия"
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)  # вертикальный кадр иначе ляжет набок
        if max(img.size) > max_side:
            img.thumbnail((max_side, max_side), Image.LANCZOS)
        dims = f"{img.size[0]}x{img.size[1]}"

        # Прозрачность кладём на белое: в регламенте фон белый, а JPEG альфы
        # не умеет вовсе и залил бы её чёрным.
        if img.mode in ("RGBA", "LA", "P"):
            rgba = img.convert("RGBA")
            flat = Image.new("RGB", rgba.size, (255, 255, 255))
            flat.paste(rgba, mask=rgba.split()[3])
            img = flat
        else:
            img = img.convert("RGB")

        jpg = io.BytesIO()
        img.save(jpg, "JPEG", quality=quality, optimize=True)
        png = io.BytesIO()
        img.quantize(colors=colors, method=Image.MEDIANCUT).save(png, "PNG", optimize=True)

        if len(png.getvalue()) <= len(jpg.getvalue()) * 1.25:
            return png.getvalue(), "png", f"{dims} png"
        return jpg.getvalue(), "jpeg", f"{dims} jpeg"
    except Exception as exc:  # noqa: BLE001 — оригинал честнее пустоты
        print(f"  ! картинку не удалось пережать ({exc}), беру как есть")
        return data, "png", "как есть"


def kb(n: int) -> str:
    return f"{n / 1024:.0f} КБ"


TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def make_slug(stem: str) -> str:
    """Имя файла из названия документа. Кириллицу транслитерируем.

    Просто выбросить не-латиницу нельзя: «ТЗ каталог яндекс.еда 1.3» обращается
    в «1-3», и по имени файла уже не понять, к какому регламенту он относится.
    """
    lowered = "".join(TRANSLIT.get(ch, ch) for ch in stem.lower())
    return re.sub(r"[^a-z0-9]+", "-", lowered).strip("-") or "img"


def esc(text: str) -> str:
    return html.escape(text, quote=True)


class Converter:
    def __init__(self, document, args, slug: str, img_dir: str):
        self.doc = document
        self.args = args
        self.slug = slug
        self.img_dir = img_dir
        self.count = 0
        self.src_bytes = 0
        self.out_bytes = 0

    # --- картинки -------------------------------------------------------
    def image_tag(self, rid: str, alt: str) -> str:
        part = self.doc.part.related_parts.get(rid)
        if part is None:
            return ""
        raw = part.blob
        self.count += 1
        self.src_bytes += len(raw)

        ext = os.path.splitext(part.partname)[1].lower().lstrip(".") or "png"
        data, fmt, dims = shrink(raw, self.args.max_width, self.args.quality,
                                 self.args.png_colors)
        if not HAVE_PIL:
            fmt = "jpeg" if ext in ("jpg", "jpeg") else ext
        self.out_bytes += len(data)
        print(f"  фото {self.count}: {kb(len(raw))} -> {kb(len(data))} ({dims})")

        alt = esc(alt or f"Иллюстрация {self.count}")
        style = "max-width:100%;height:auto;border-radius:8px"
        if self.args.mode == "inline":
            b64 = base64.b64encode(data).decode("ascii")
            src = f"data:image/{fmt};base64,{b64}"
        else:
            name = f"{self.slug}-{self.count:02d}.{'jpg' if fmt == 'jpeg' else fmt}"
            with open(os.path.join(self.img_dir, name), "wb") as f:
                f.write(data)
            base = self.args.base_url.rstrip("/")
            src = f"{base}/{name}" if base else name
        return f'<p><img src="{src}" alt="{alt}" loading="lazy" style="{style}"></p>'

    # --- текст ----------------------------------------------------------
    def run_html(self, run) -> str:
        pieces = []
        for blip in run._element.findall(".//a:blip", NS):
            rid = blip.get(f"{{{NS['r']}}}embed")
            if rid:
                alt = ""
                for docpr in run._element.findall(".//wp:docPr", NS):
                    alt = docpr.get("descr") or docpr.get("name") or ""
                pieces.append(self.image_tag(rid, alt))
        text = run.text
        if text:
            frag = esc(text)
            if run.bold:
                frag = f"<strong>{frag}</strong>"
            if run.italic:
                frag = f"<em>{frag}</em>"
            pieces.append(frag)
        return "".join(pieces)

    def paragraph_html(self, para: Paragraph) -> list[str]:
        images, inline = [], []
        for run in para.runs:
            chunk = self.run_html(run)
            (images if chunk.startswith("<p><img") else inline).append(chunk)

        # Гиперссылки: python-docx не кладёт их в runs, добираем из XML.
        for link in para._p.findall(f"{{{NS['w']}}}hyperlink"):
            rid = link.get(f"{{{NS['r']}}}id")
            text = "".join(n.text or "" for n in link.findall(f".//{{{NS['w']}}}t"))
            if not text:
                continue
            rel = self.doc.part.rels.get(rid)
            href = esc(rel.target_ref) if rel is not None else ""
            inline.append(f'<a href="{href}">{esc(text)}</a>' if href else esc(text))

        body = "".join(inline).strip()
        out = list(images)
        if not body:
            return out

        style = para.style.name if para.style is not None else ""
        heading = HEADING_RE.match(style or "")
        if heading:
            level = min(int(heading.group(1)) + 1, 6)  # h1 оставляем заголовку регламента
            out.append(f"<h{level}>{body}</h{level}>")
        elif style and LIST_RE.search(style):
            out.append(f"<li>{body}</li>")
        else:
            out.append(f"<p>{body}</p>")
        return out

    def table_html(self, table: Table) -> list[str]:
        rows = ["<table>"]
        for i, row in enumerate(table.rows):
            cells = []
            for cell in row.cells:
                inner = []
                for para in cell.paragraphs:
                    inner.extend(self.paragraph_html(para))
                text = "".join(inner) or "&nbsp;"
                tag = "th" if i == 0 else "td"
                cells.append(f"<{tag}>{text}</{tag}>")
            rows.append("<tr>" + "".join(cells) + "</tr>")
        rows.append("</table>")
        return rows

    def convert(self) -> str:
        parts: list[str] = []
        body = self.doc.element.body
        for child in body.iterchildren():
            tag = child.tag.split("}")[-1]
            if tag == "p":
                parts.extend(self.paragraph_html(Paragraph(child, self.doc)))
            elif tag == "tbl":
                parts.extend(self.table_html(Table(child, self.doc)))
        return wrap_lists(parts)


def wrap_lists(parts: list[str]) -> str:
    """Оборачивает подряд идущие <li> в <ul> — иначе список развалится."""
    out, open_list = [], False
    for chunk in parts:
        if chunk.startswith("<li>"):
            if not open_list:
                out.append("<ul>")
                open_list = True
        elif open_list:
            out.append("</ul>")
            open_list = False
        out.append(chunk)
    if open_list:
        out.append("</ul>")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="docx -> HTML для «Регламентов» Пульса")
    ap.add_argument("docx", help="исходный файл .docx")
    ap.add_argument("-o", "--out", help="куда положить .html (по умолчанию рядом с docx)")
    ap.add_argument("--mode", choices=("inline", "files"), default="inline",
                    help="inline — фото внутри HTML (по умолчанию); files — фото отдельными файлами")
    ap.add_argument("--img-dir", help="папка для фото в режиме files")
    ap.add_argument("--base-url", default="", help="префикс ссылки на фото в режиме files")
    ap.add_argument("--max-width", type=int, default=1600, help="длинная сторона фото, px")
    ap.add_argument("--quality", type=int, default=82, help="качество JPEG")
    ap.add_argument("--png-colors", type=int, default=256,
                    help="цветов в палитре PNG для скриншотов")
    args = ap.parse_args()

    if not os.path.exists(args.docx):
        print(f"Файл не найден: {args.docx}")
        return 1

    stem = os.path.splitext(os.path.basename(args.docx))[0]
    out_path = args.out or os.path.join(os.path.dirname(args.docx) or ".", stem + ".html")
    img_dir = args.img_dir or os.path.join(os.path.dirname(out_path) or ".", stem + "-img")
    if args.mode == "files":
        os.makedirs(img_dir, exist_ok=True)

    slug = make_slug(stem)

    print(f"Читаю {args.docx}")
    if not HAVE_PIL:
        print("  ! Pillow не установлен — фото идут без сжатия (pip install Pillow)")

    conv = Converter(docx.Document(args.docx), args, slug, img_dir)
    result = conv.convert()

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(result)

    size = len(result.encode("utf-8"))
    print()
    print(f"HTML: {out_path} — {kb(size)}")
    print(f"Фото: {conv.count} шт, {kb(conv.src_bytes)} -> {kb(conv.out_bytes)}")
    if args.mode == "files":
        print(f"Файлы фото: {img_dir}")
        if not args.base_url:
            print("  ! --base-url не задан: в ссылках только имена файлов")
    elif size > 1_000_000:
        print("  ! HTML больше 1 МБ — поле в Пульсе может не принять.")
        print("    Поставьте Pillow и уменьшите --max-width/--quality,")
        print("    либо соберите с --mode files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
