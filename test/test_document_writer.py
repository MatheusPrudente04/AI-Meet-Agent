"""Gerador de documentos do chat: tabelas Markdown viram planilha, tabela de Word/PDF e gráfico."""
import csv

from infrastructure.exporters.document_writer import _number, _tables, write_document

MD = """## Tarefas por responsável
| Responsável | Tarefas |
|---|---|
| Carla | 2 |
| Bruno | 1 |

- Fala com <script>alert(1)</script>"""


def test_numero_em_formato_brasileiro():
    assert _number("1.250,50") == 1250.5 and _number("800") == 800 and _number("30%") == 30
    assert _number("sexta") is None and _number("25/11") is None


def test_planilhas_saem_das_tabelas(tmp_path):
    import openpyxl
    ws = openpyxl.load_workbook(write_document(tmp_path / "a.xlsx", "xlsx", "T", MD)).active
    assert ws.title == "Tarefas por responsável"
    assert [[c.value for c in r] for r in ws.iter_rows()] == [["Responsável", "Tarefas"], ["Carla", 2], ["Bruno", 1]]
    with open(write_document(tmp_path / "a.csv", "csv", "T", MD), encoding="utf-8-sig") as f:
        assert list(csv.reader(f, delimiter=";")) == [["Responsável", "Tarefas"], ["Carla", "2"], ["Bruno", "1"]]


def test_sem_tabela_cada_linha_vira_linha_da_planilha():
    assert _tables("## Decisões\n- Entrega na **sexta**") == [("", [["Conteúdo"], ["Decisões"], ["Entrega na sexta"]])]


def test_html_tem_grafico_e_escapa_o_texto(tmp_path):
    page = write_document(tmp_path / "a.html", "html", "T", MD).read_text(encoding="utf-8")
    assert page.count("<svg") == 1 and "Tarefas por responsável" in page
    assert "<script>" not in page and "&lt;script&gt;" in page


def test_word_e_pdf_com_tabela(tmp_path):
    import docx
    doc = docx.Document(write_document(tmp_path / "a.docx", "docx", "T", MD))
    assert [c.text for c in doc.tables[0].rows[1].cells] == ["Carla", "2"]
    assert write_document(tmp_path / "a.pdf", "pdf", "T", MD).stat().st_size > 800


def test_csv_escrito_direto_pelo_modelo_vira_colunas():
    rows = _tables("Tarefa,Responsável,Status\nEnviar calendário,Carla,Confirmado\nRevisar contratos,N/A,Pendente")[0][1]
    assert rows == [["Tarefa", "Responsável", "Status"], ["Enviar calendário", "Carla", "Confirmado"],
                    ["Revisar contratos", "N/A", "Pendente"]]


def test_html_do_modelo_e_pares_nome_numero_viram_tabela_com_grafico(tmp_path):
    from infrastructure.exporters.document_writer import _normalize
    modelo = ('<h1>Dashboard</h1><p><strong>Falas:</strong></p><figure><img src="data:image/png;base64,xx">'
              '</figure><p><strong>Ana:</strong> 6 falas</p><p><strong>Bruno:</strong> 4 falas</p>'
              '<p><strong>Carla:</strong> 3 falas</p>')
    md = _normalize(modelo)
    assert "| Item | Falas |" in md and "| Ana | 6 |" in md and "<img" not in md
    page = write_document(tmp_path / "d.html", "html", "Dashboard", modelo).read_text(encoding="utf-8")
    assert page.count("<svg") == 1 and "&lt;img" not in page


def test_um_par_so_e_texto_normal_continua():
    from infrastructure.exporters.document_writer import _normalize
    assert _normalize("Prazo: 25 de novembro") == "Prazo: 25 de novembro"
    assert _normalize("- Status: aprovado\n- Prazo: sexta") == "- Status: aprovado\n- Prazo: sexta"


def test_tabela_html_e_pares_separados_por_linha_em_branco():
    from infrastructure.exporters.document_writer import _normalize
    tabela = "<table><tr><th>Participante</th><th>Falas</th></tr><tr><td>Ana</td><td>6</td></tr>" \
             "<tr><td>Bruno</td><td>4</td></tr></table>"
    assert _tables(_normalize(tabela))[0][1] == [["Participante", "Falas"], ["Ana", "6"], ["Bruno", "4"]]
    pares = "<p>\n<strong>Ana:</strong> 6 falas</p><p>\n<strong>Bruno:</strong> 4 falas</p>"
    assert "| Ana | 6 |" in _normalize(pares)


def test_dashboard_so_com_texto_ganha_os_numeros_do_sistema(tmp_path):
    tabelas = [("Falas por participante", [["Participante", "Falas"], ["Ana", "6"], ["Bruno", "4"]])]
    prosa = "Ana (6 falas) e Bruno (4 falas) participaram."
    page = write_document(tmp_path / "a.html", "html", "D", prosa, fallback_tables=tabelas).read_text(encoding="utf-8")
    assert "Números da reunião" in page and page.count("<svg") == 1
    com_tabela = write_document(tmp_path / "b.html", "html", "D", MD, fallback_tables=tabelas).read_text(encoding="utf-8")
    assert "Números da reunião" not in com_tabela  # o modelo já trouxe tabela: não duplica
