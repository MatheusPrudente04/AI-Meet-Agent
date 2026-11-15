"""Documento gerado pelo chat (ferramenta create_document): Markdown → PDF, Word, Excel, CSV,
HTML ou .md.

Entende o Markdown que o modelo escreve: títulos (#, ##, ###), listas (-, *, 1.), parágrafos,
**negrito** e tabelas (| a | b |). Planilhas (xlsx, csv) saem das tabelas; sem tabela, cada
linha do texto vira uma linha da planilha. O HTML é uma página única, sem nada externo, com um
gráfico de barras (SVG) para cada tabela que tenha coluna numérica.
"""
from __future__ import annotations

import csv
import html
import re
from pathlib import Path
from typing import Any, Iterator

FORMATS = ("pdf", "docx", "xlsx", "csv", "html", "md")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_NUMBERED = re.compile(r"^\d+[.)]\s+")
_TABLE_SEP = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$")
# As fontes embutidas do PDF são Latin-1: acentos do português cabem; estes símbolos, não.
_LATIN1 = str.maketrans({"—": "-", "–": "-", "“": '"', "”": '"', "‘": "'", "’": "'", "…": "...",
                         "•": "-", "→": "->", "✅": "[x]", "⬜": "[ ]"})


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _blocks(markdown: str) -> Iterator[tuple[str, Any]]:
    """(tipo, valor) em ordem: h1, h2, h3, bullet, number, text, blank e table (lista de linhas,
    a primeira é o cabeçalho)."""
    lines = markdown.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("|") and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1].strip()):
            rows = [_cells(line)]
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            width = len(rows[0])
            yield "table", [(r + [""] * width)[:width] for r in rows]
            continue
        i += 1
        if not line:
            yield "blank", ""
        elif line.startswith("### "):
            yield "h3", line[4:]
        elif line.startswith("## "):
            yield "h2", line[3:]
        elif line.startswith("# "):
            yield "h1", line[2:]
        elif line[:2] in ("- ", "* "):
            yield "bullet", line[2:]
        elif _NUMBERED.match(line):
            yield "number", _NUMBERED.sub("", line)
        else:
            yield "text", line


def _plain(text: str) -> str:
    return _BOLD.sub(r"\1", text)


def _tables(markdown: str) -> list[tuple[str, list[list[str]]]]:
    """(título da seção, linhas) de cada tabela. Sem tabela: uma coluna com o texto."""
    tables, heading = [], ""
    for kind, value in _blocks(markdown):
        if kind in ("h1", "h2", "h3"):
            heading = _plain(value)
        elif kind == "table":
            tables.append((heading, [[_plain(c) for c in row] for row in value]))
    if not tables:
        tables = [("", _delimited(markdown) or
                   [["Conteúdo"], *([_plain(v)] for k, v in _blocks(markdown) if k not in ("blank", "table"))])]
    return tables


def _delimited(text: str):
    """Texto que já veio como CSV (o modelo às vezes escreve assim em vez de tabela Markdown):
    todas as linhas com o mesmo número de separadores. None se não for."""
    lines = [l for l in text.strip().splitlines() if l.strip() and not l.lstrip().startswith("#")]
    for sep in (";", ",", "\t"):
        counts = {next(csv.reader([l], delimiter=sep)).__len__() for l in lines}
        if len(lines) >= 2 and len(counts) == 1 and counts.pop() >= 2:
            return [[_plain(c.strip()) for c in row] for row in csv.reader(lines, delimiter=sep)]
    return None


def _number(cell: str):
    """'1.234,5' / '12' / '30%' / 'R$ 10' → float; None se não for número."""
    text = cell.replace("R$", "").replace("%", "").strip()
    if not re.fullmatch(r"-?[\d.,]+", text or "x"):
        return None
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


_HTML_TAG = re.compile(r"<(p|h[1-6]|div|table|tr|ul|ol|li|figure|img|br|strong|span)\b", re.IGNORECASE)
_PAIR = re.compile(r"^\s*(?:[-*]|\d+[.)])?\s*\**([^:*|\n]{1,40}?)\**\s*:\s*\**\s*(-?[\d.,]+%?)\s*([^\W\d][\w ]{0,20})?\s*\.?$")


def _normalize(markdown: str) -> str:
    """Conserta o que o modelo às vezes manda no lugar de Markdown:
    - HTML pronto (com <img> de gráfico de mentira): vira texto, mantendo títulos e listas;
    - linhas "Nome: número" seguidas (≥2): viram tabela, que no HTML ganha gráfico."""
    if _HTML_TAG.search(markdown):
        text = re.sub(r"<(script|style)\b.*?</\1>", "", markdown, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<table\b.*?</table>", _html_table, text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<h[1-3][^>]*>", "\n## ", text, flags=re.IGNORECASE)
        text = re.sub(r"<li[^>]*>", "\n- ", text, flags=re.IGNORECASE)
        text = re.sub(r"</?(strong|b)>", "**", text, flags=re.IGNORECASE)
        text = re.sub(r"<(br|/p|/h\d|/li|/tr|/div|/figure|/table)[^>]*>", "\n", text, flags=re.IGNORECASE)
        markdown = html.unescape(re.sub(r"<[^>]+>", "", text))
        markdown = "\n".join(l.strip() for l in markdown.splitlines())
    out, run = [], []

    def flush():
        if len(run) >= 2:
            unit = next((u.strip() for _, _, u in run if u), "")
            out.extend([f"| Item | {unit.capitalize() or 'Valor'} |", "|---|---|",
                        *(f"| {label.strip()} | {value} |" for label, value, _ in run)])
        else:
            out.extend(original for original in run_lines)
        run.clear()
        run_lines.clear()

    run_lines: list[str] = []
    for line in markdown.splitlines():
        m = _PAIR.match(line)
        if m and "|" not in line:
            run.append(m.groups())
            run_lines.append(line)
            continue
        if run and not line.strip():  # linha em branco entre os pares não quebra a sequência
            continue
        flush()
        out.append(line)
    flush()
    return "\n".join(out)


def _html_table(match) -> str:
    """<table> que o modelo escreveu → tabela Markdown (a 1ª linha é o cabeçalho)."""
    rows = []
    for tr in re.findall(r"<tr\b.*?</tr>", match.group(0), flags=re.IGNORECASE | re.DOTALL):
        cells = re.findall(r"<t[hd]\b[^>]*>(.*?)</t[hd]>", tr, flags=re.IGNORECASE | re.DOTALL)
        rows.append([html.unescape(re.sub(r"<[^>]+>", "", c)).strip().replace("|", "/") for c in cells])
    rows = [r for r in rows if r]
    if not rows:
        return "\n"
    width = max(len(r) for r in rows)
    lines = [("| " + " | ".join((r + [""] * width)[:width]) + " |") for r in rows]
    return "\n" + "\n".join([lines[0], "|" + "---|" * width, *lines[1:]]) + "\n"


def write_document(path: Path, fmt: str, title: str, markdown: str,
                   fallback_tables: list[tuple[str, list[list[str]]]] | None = None) -> Path:
    """fallback_tables: (título, linhas) acrescentadas em "Números da reunião" quando o conteúdo
    não tem nenhuma tabela (dashboard em que o modelo escreveu só texto)."""
    if fmt not in FORMATS:
        raise ValueError(f"Formato não suportado: {fmt}. Use um de: {', '.join(FORMATS)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    markdown = _normalize(markdown)
    if fallback_tables and not any(kind == "table" for kind, _ in _blocks(markdown)):
        markdown += "\n\n## Números da reunião\n\n" + "\n\n".join(
            f"### {heading}\n\n| " + " | ".join(rows[0]) + " |\n|" + "---|" * len(rows[0]) + "\n"
            + "\n".join("| " + " | ".join(r) + " |" for r in rows[1:]) for heading, rows in fallback_tables)
    {"pdf": _pdf, "docx": _docx, "xlsx": _xlsx, "csv": _csv, "html": _html, "md": _md}[fmt](path, title, markdown)
    return path


def _md(path: Path, title: str, markdown: str) -> None:
    path.write_text(f"# {title}\n\n{markdown.strip()}\n", encoding="utf-8")


def _csv(path: Path, title: str, markdown: str) -> None:
    # Ponto e vírgula e BOM: é o que o Excel em português abre direto, com acentos certos.
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        for n, (_, rows) in enumerate(_tables(markdown)):
            if n:
                writer.writerow([])
            writer.writerows(rows)


def _xlsx(path: Path, title: str, markdown: str) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    if wb.active is not None:
        wb.remove(wb.active)
    used = set()
    for n, (heading, rows) in enumerate(_tables(markdown), start=1):
        name = re.sub(r"[\[\]:*?/\\]", "", heading or title)[:28] or f"Tabela {n}"
        while name in used:
            name = f"{name[:25]} {n}"
        used.add(name)
        ws = wb.create_sheet(name)
        for r, row in enumerate(rows, start=1):
            for c, cell in enumerate(row, start=1):
                value = _number(cell) if r > 1 else None
                ws.cell(row=r, column=c, value=value if value is not None else cell)
        for cell in ws[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="2F5496")
        for c, column in enumerate(ws.columns, start=1):
            width = max(len(str(cell.value or "")) for cell in column)
            ws.column_dimensions[column[0].column_letter].width = min(max(width + 2, 10), 60)
        ws.freeze_panes = "A2"
    wb.save(str(path))


def _docx(path: Path, title: str, markdown: str) -> None:
    from docx import Document

    doc = Document()
    doc.add_heading(title, level=0)
    for kind, value in _blocks(markdown):
        if kind == "blank":
            continue
        if kind in ("h1", "h2", "h3"):
            doc.add_heading(_plain(value), level=int(kind[1]))
            continue
        if kind == "table":
            table = doc.add_table(rows=0, cols=len(value[0]))
            table.style = "Light Grid Accent 1"
            for r, row in enumerate(value):
                cells = table.add_row().cells
                for c, text in enumerate(row):
                    cells[c].text = _plain(text)
                    if r == 0:
                        for run in cells[c].paragraphs[0].runs:
                            run.bold = True
            continue
        style = {"bullet": "List Bullet", "number": "List Number"}.get(kind)
        paragraph = doc.add_paragraph(style=style)
        for i, part in enumerate(_BOLD.split(value)):  # partes ímpares estavam entre **
            paragraph.add_run(part).bold = i % 2 == 1
    doc.save(str(path))


def _pdf(path: Path, title: str, markdown: str) -> None:
    from fpdf import FPDF

    def latin1(text: str) -> str:
        return text.translate(_LATIN1).encode("latin-1", "replace").decode("latin-1")

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 18)
    pdf.multi_cell(0, 9, latin1(title), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    sizes = {"h1": 15, "h2": 13, "h3": 12}
    counter = 0
    for kind, value in _blocks(markdown):
        counter = counter + 1 if kind == "number" else 0
        if kind == "blank":
            pdf.ln(2)
            continue
        if kind in sizes:
            pdf.ln(2)
            pdf.set_font("Helvetica", "B", sizes[kind])
            pdf.multi_cell(0, 7, latin1(_plain(value)), new_x="LMARGIN", new_y="NEXT")
            continue
        if kind == "table":
            pdf.set_font("Helvetica", "", 10)
            with pdf.table(first_row_as_headings=True, line_height=6) as table:
                for row in value:
                    table.row([latin1(_plain(c)) for c in row])
            pdf.ln(2)
            continue
        prefix = {"bullet": "-  ", "number": f"{counter}.  "}.get(kind, "")
        pdf.set_font("Helvetica", "", 11)
        pdf.multi_cell(0, 6, latin1(prefix + value), markdown=True, new_x="LMARGIN", new_y="NEXT")
    pdf.output(str(path))


# ── HTML: página única com gráficos ───────────────────────────────────

_CSS = """
:root { --fg:#1f2933; --muted:#616e7c; --line:#d9e2ec; --accent:#2f5496; --bg:#ffffff; --card:#f5f7fa; }
@media (prefers-color-scheme: dark) {
  :root { --fg:#e4e7eb; --muted:#9aa5b1; --line:#3e4c59; --accent:#7ea6e0; --bg:#1f2933; --card:#27323d; }
}
body { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; color: var(--fg); background: var(--bg);
       max-width: 960px; margin: 0 auto; padding: 32px 16px; line-height: 1.55; }
h1 { font-size: 1.8rem; margin-bottom: .2rem; } h2 { margin-top: 2rem; border-bottom: 1px solid var(--line); }
.meta { color: var(--muted); font-size: .9rem; }
table { border-collapse: collapse; width: 100%; margin: 1rem 0; font-size: .95rem; }
th, td { border: 1px solid var(--line); padding: 6px 10px; text-align: left; }
th { background: var(--card); } td.num { text-align: right; font-variant-numeric: tabular-nums; }
.chart { background: var(--card); border-radius: 8px; padding: 12px 16px; margin: .5rem 0 1.5rem; }
.chart text { fill: var(--fg); font-size: 12px; } .chart rect { fill: var(--accent); }
.wrap { overflow-x: auto; }
"""


def _html_inline(text: str) -> str:
    return _BOLD.sub(r"<strong>\1</strong>", html.escape(text))


def _bar_chart(rows: list[list[str]]) -> str:
    """Barras horizontais da 1ª coluna numérica, rotuladas pela 1ª coluna de texto."""
    header, body = rows[0], rows[1:]
    numeric = [c for c in range(len(header)) if body and all(_number(r[c]) is not None for r in body)]
    labels = [c for c in range(len(header)) if c not in numeric]
    if not numeric or not labels or len(body) < 2:
        return ""
    vc, lc = numeric[0], labels[0]
    values = [_number(r[vc]) or 0.0 for r in body]  # todas numéricas (checado acima)
    top = max(max(values), 0) or 1
    bar_h, gap, label_w, width = 22, 8, 180, 640
    height = len(body) * (bar_h + gap) + 28
    bars = []
    for i, (row, v) in enumerate(zip(body, values)):
        y = 24 + i * (bar_h + gap)
        w = max(v, 0) / top * (width - label_w - 70)
        label = html.escape(_plain(row[lc])[:28])
        bars.append(f'<text x="{label_w - 8}" y="{y + 15}" text-anchor="end">{label}</text>'
                    f'<rect x="{label_w}" y="{y}" width="{w:.1f}" height="{bar_h}" rx="3"></rect>'
                    f'<text x="{label_w + w + 6:.1f}" y="{y + 15}">{html.escape(row[vc])}</text>')
    caption = html.escape(f"{_plain(header[vc])} por {_plain(header[lc]).lower()}")
    return (f'<div class="chart wrap"><svg viewBox="0 0 {width} {height}" width="100%" role="img" '
            f'aria-label="{caption}"><text x="0" y="14" font-weight="600">{caption}</text>'
            f'{"".join(bars)}</svg></div>')


def _html(path: Path, title: str, markdown: str) -> None:
    out, items, list_tag = [], [], ""

    def close_list():
        nonlocal items, list_tag
        if items:
            out.append(f"<{list_tag}>" + "".join(f"<li>{i}</li>" for i in items) + f"</{list_tag}>")
        items, list_tag = [], ""

    for kind, value in _blocks(markdown):
        if kind in ("bullet", "number"):
            tag = "ul" if kind == "bullet" else "ol"
            if list_tag and list_tag != tag:
                close_list()
            list_tag = tag
            items.append(_html_inline(value))
            continue
        close_list()
        if kind in ("h1", "h2", "h3"):
            level = {"h1": 2, "h2": 2, "h3": 3}[kind]  # h1 da página é o título
            out.append(f"<h{level}>{_html_inline(value)}</h{level}>")
        elif kind == "text":
            out.append(f"<p>{_html_inline(value)}</p>")
        elif kind == "table":
            head = "".join(f"<th>{_html_inline(c)}</th>" for c in value[0])
            body = "".join(
                "<tr>" + "".join(f'<td class="num">{_html_inline(c)}</td>' if _number(c) is not None
                                 else f"<td>{_html_inline(c)}</td>" for c in row) + "</tr>"
                for row in value[1:])
            out.append(f'<div class="wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>')
            out.append(_bar_chart(value))
    close_list()
    page = (f'<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{html.escape(title)}</title><style>{_CSS}</style></head><body>"
            f'<h1>{html.escape(title)}</h1><p class="meta">Gerado pelo Meet Agent a partir da reunião.</p>'
            f'{"".join(out)}</body></html>')
    path.write_text(page, encoding="utf-8")
