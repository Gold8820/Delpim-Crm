"""Leitura de planilhas no navegador (static/planilhas.js) x leitura no servidor (clientes/importer.py).

O JavaScript roda no Node com os mesmos arquivos. O resultado (linhas enviadas em lotes) passa pelo
caminho do servidor para planilhas lidas no navegador e tem de ser IGUAL à leitura direta do arquivo no
servidor: mesmo cabeçalho, mesmas linhas, mesmos valores (texto, números, datas).
Depois, o fluxo completo (lotes -> prévia -> confirmação) tem de dar o mesmo resultado do envio do arquivo.
Sem Node instalado, os testes de equivalência são pulados (o resto da suíte continua).
"""
import io
import json
import shutil
import subprocess
from datetime import datetime, time
from pathlib import Path

import pytest
from openpyxl import Workbook

from clientes import importer
from clientes.importer import ImportFailure

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
precisa_node = pytest.mark.skipif(NODE is None, reason="Node.js não instalado")

RUNNER = r"""
const P = require(process.argv[1]);
const fs = require("fs");
(async () => {
  const bytes = new Uint8Array(fs.readFileSync(process.argv[2]));
  try {
    const lido = await P.ler({ name: process.argv[3], size: bytes.length, bytes });
    process.stdout.write(JSON.stringify(lido));
  } catch (e) {
    process.stdout.write(JSON.stringify({ erro: e.message, tipo: e instanceof P.ErroPlanilha ? "planilha" : "outro" }));
  }
})();
"""


def ler_no_navegador(tmp_path, conteudo: bytes, nome: str) -> dict:
    arquivo = tmp_path / ("entrada" + Path(nome).suffix)
    arquivo.write_bytes(conteudo)
    res = subprocess.run([NODE, "-e", RUNNER, str(ROOT / "static" / "planilhas.js"), str(arquivo), nome],
                         capture_output=True, timeout=60)
    assert res.returncode == 0, res.stderr.decode("utf-8", "replace")
    return json.loads(res.stdout.decode("utf-8"))


def como_lotes(lido: dict) -> bytes:
    """O mesmo formato que /api/importacao/lote grava (1ª linha: metadados; depois um lote por linha)."""
    partes = [json.dumps({"arquivo": lido["arquivo"], "planilhas": lido["planilhas"]})]
    for aba, linhas in enumerate(lido["abas"]):
        partes.append(json.dumps({"s": aba, "rows": linhas}))
    return ("\n".join(partes) + "\n").encode()


def normal(valor):
    if isinstance(valor, time):
        return str(valor)
    if isinstance(valor, float) and valor.is_integer():
        return int(valor)
    return valor


def normal_tabela(t: dict) -> dict:
    def linha(cells):
        cells = [normal(c) for c in cells]
        while cells and cells[-1] in (None, ""):
            cells.pop()
        return cells
    return {"sheets": t["sheets"], "sheet": t["sheet"], "header": t["header"],
            "rows": [(n, linha(c)) for n, c in t["rows"]]}


def comparar(tmp_path, conteudo: bytes, nome: str, abas=(None,), header_rows=(None,)):
    ext = Path(nome).suffix.lower()
    lido = ler_no_navegador(tmp_path, conteudo, nome)
    assert "erro" not in lido, lido
    for aba in abas:
        for hr in header_rows:
            try:
                esperado = normal_tabela(importer.read_table(conteudo, ext, aba, hr))
            except ImportFailure as exc:   # o servidor recusa: o caminho do navegador recusa com a mesma mensagem
                with pytest.raises(ImportFailure) as obtido_exc:
                    importer.read_table(como_lotes(lido), ".json", aba, hr)
                assert str(obtido_exc.value) == str(exc)
                continue
            obtido = normal_tabela(importer.read_table(como_lotes(lido), ".json", aba, hr))
            assert obtido == esperado, f"diferença em {nome} aba={aba} cabeçalho={hr}"
    return lido


def xlsx(fill, **opts) -> bytes:
    wb = Workbook()
    fill(wb)
    if opts.get("epoch1904"):
        wb.epoch = datetime(1904, 1, 1)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ============================================================================ .xlsx

def _planilha_completa(wb):
    ws = wb.active
    ws.title = "Clientes"
    ws.append(["Relatório de clientes"])             # título de uma célula acima da tabela
    ws.append([])
    ws.append(["Código", "Razão Social", "CNPJ", "Valor", "Início", "Hora", "Ativo", "Obs"])
    ws.append([1, "Alfa Ltda", 11222333000181, 1050.5, datetime(2026, 1, 15), time(8, 30), True, "linha 1\nlinha 2"])
    ws.append(["002", "Beta & Cia <ME>", "11.444.777/0001-61", 399.99, datetime(2026, 2, 28, 14, 5, 9), None, False, "  espaços  "])
    ws.append([])
    ws.append([3, "Gama — ç ã é", 45997418000153, 12345678901234567890, datetime(1900, 2, 1), None, None, None])
    ws.append([None, None, None, 1e-7, None, None, None, "=SOMA(A1:A2)"])
    ws["E5"].number_format = "dd/mm/yyyy hh:mm:ss"
    ws["D4"].number_format = '"R$" #,##0.00;[Red]-"R$" #,##0.00'   # aspas e cores: não é data
    ws["D5"].number_format = "[>=100]0;0.00"                        # '>' dentro do atributo
    ws["F4"].number_format = "hh:mm"
    ws.cell(row=10, column=12, value="coluna distante")             # lacuna de colunas
    oculta = wb.create_sheet("Oculta")
    oculta.append(["não", "deve", "aparecer"])
    oculta.sheet_state = "hidden"
    outra = wb.create_sheet("Contratos")
    outra.append(["Nº", "Valor Atual", "Mês"])
    outra.append(["C-1", 600, "11/2026"])
    outra.append(["C-2", 1.5e3, datetime(2026, 11, 1)])


@precisa_node
def test_xlsx_completo_igual_ao_servidor(tmp_path):
    lido = comparar(tmp_path, xlsx(_planilha_completa), "clientes.xlsx",
                    abas=(None, "Clientes", "Contratos"), header_rows=(None, 3, 1))
    assert lido["planilhas"] == ["Clientes", "Contratos"]   # aba oculta fica de fora, como no servidor


@precisa_node
def test_xlsx_com_data_1904(tmp_path):
    def fill(wb):
        ws = wb.active
        ws.append(["Mês", "Valor"])
        ws.append([datetime(2026, 11, 1), 10])
        ws.append([datetime(2025, 1, 31, 23, 59, 59), 20])
    comparar(tmp_path, xlsx(fill, epoch1904=True), "datas.xlsx")


@precisa_node
def test_xlsx_invalido_e_recusado_nos_dois(tmp_path):
    lido = ler_no_navegador(tmp_path, b"isto nao e uma planilha", "c.xlsx")
    assert lido.get("tipo") == "planilha"
    with pytest.raises(ImportFailure):
        importer.read_table(b"isto nao e uma planilha", ".xlsx")


# ============================================================================ .xls (XML do Excel 2003)

XLS = """<?xml version="1.0" encoding="{enc}"?>
<?mso-application progid="Excel.Sheet"?>
<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" xmlns:o="urn:schemas-microsoft-com:office:office"
 xmlns:x="urn:schemas-microsoft-com:office:excel" xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet"
 xmlns:html="http://www.w3.org/TR/REC-html40">
 <!-- comentário -->
 <Worksheet ss:Name="Contratos Ativos">
  <Table ss:ExpandedColumnCount="9">
   <Row><Cell><Data ss:Type="String">Relatório</Data></Cell></Row>
   <Row ss:Index="3">
    <Cell><Data ss:Type="String">Documento Cliente</Data></Cell><Cell><Data ss:Type="String">Nº</Data></Cell>
    <Cell><Data ss:Type="String">Valor Atual</Data></Cell><Cell><Data ss:Type="String">Início</Data></Cell>
    <Cell><Data ss:Type="String">Ativo</Data></Cell><Cell ss:Index="7"><Data ss:Type="String">Obs</Data></Cell>
   </Row>
   <Row>
    <Cell><Data ss:Type="Number">11222333000181</Data></Cell><Cell><Data ss:Type="String">C-100</Data></Cell>
    <Cell><Data ss:Type="Number">600.5</Data></Cell><Cell><Data ss:Type="DateTime">2026-01-15T00:00:00.000</Data></Cell>
    <Cell><Data ss:Type="Boolean">1</Data></Cell><Cell ss:Index="7"><Data ss:Type="String">Ação &amp; &lt;teste&gt; &#233;
segunda linha</Data></Cell>
   </Row>
   <Row>
    <Cell><Data ss:Type="Number">12345678901234567890</Data></Cell><Cell><Data ss:Type="String">  C-101 </Data></Cell>
    <Cell><Data ss:Type="Number">1E3</Data></Cell><Cell><Data ss:Type="DateTime">texto-invalido</Data></Cell>
    <Cell><Data ss:Type="Boolean">0</Data></Cell>
    <Cell ss:Index="7"><ss:Data ss:Type="String" xmlns="http://www.w3.org/TR/REC-html40"><Font>rico</Font></ss:Data></Cell>
   </Row>
  </Table>
 </Worksheet>
 <Worksheet ss:Name="Resumo"><Table><Row><Cell><Data ss:Type="String">Total</Data></Cell><Cell><Data ss:Type="Number">2</Data></Cell></Row></Table></Worksheet>
 <Worksheet><Table/></Worksheet>
</Workbook>
"""


@precisa_node
@pytest.mark.parametrize("enc", ["UTF-8", "ISO-8859-1"])
def test_xls_xml_igual_ao_servidor(tmp_path, enc):
    conteudo = XLS.format(enc=enc).replace("\n", "\r\n").encode("utf-8" if enc == "UTF-8" else "latin-1")
    lido = comparar(tmp_path, conteudo, "contratos.xls", abas=(None, "Resumo"), header_rows=(None, 3))
    assert lido["planilhas"] == ["Contratos Ativos", "Resumo", "Planilha 3"]


@precisa_node
@pytest.mark.parametrize("conteudo", [
    b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100,      # .xls binário antigo (BIFF)
    b"<?xml version='1.0'?><!DOCTYPE a [<!ENTITY x 'y'>]><Workbook xmlns='urn:schemas-microsoft-com:office:spreadsheet'/>",
    b"<Outro/>",
], ids=["biff", "doctype", "nao_e_planilha"])
def test_xls_recusado_nos_dois(tmp_path, conteudo):
    lido = ler_no_navegador(tmp_path, conteudo, "x.xls")
    assert lido.get("tipo") == "planilha", lido
    with pytest.raises(ImportFailure):
        importer.read_table(conteudo, ".xls")


# ============================================================================ .csv

CSVS = {
    "ponto_e_virgula": "Código;Razão Social;Valor\r\n1;Alfa;1.050,50\r\n2;\"Beta; Cia\";399,99\r\n",
    "virgula_com_aspas": 'Nº,Obs,Valor\n"C-1","linha 1\nlinha 2",10\n"C-2","diz ""oi""",20\n\n"C-3",,\n',
    "tab_e_bom": "﻿A\tB\tC\n1\t2\t3\n",
    "linhas_em_branco_e_titulo": "\n\nRelatório\nCódigo;Nome\n1;X;\n   \n2;Y\n",
    "aspas_no_meio": 'a;b\nx"y;"z"w\n',
    "cr_sozinho": "a;b\r1;2\r3;4",
}


@precisa_node
@pytest.mark.parametrize("nome", list(CSVS))
def test_csv_igual_ao_servidor(tmp_path, nome):
    comparar(tmp_path, CSVS[nome].encode("utf-8"), f"{nome}.csv", header_rows=(None, 1))


@precisa_node
def test_csv_em_cp1252_igual_ao_servidor(tmp_path):
    comparar(tmp_path, "Código;Razão;Observação\n1;Ação;Pão\n".encode("cp1252"), "latin.csv")


@precisa_node
@pytest.mark.parametrize("conteudo", ['a;b\n"aberta;2\n', 'a;b\n"aberta;2'], ids=["com_quebra", "sem_quebra"])
def test_csv_com_aspas_sem_fechar_igual_ao_servidor(tmp_path, conteudo):
    """O csv do Python aceita o campo até o fim do arquivo; o navegador faz o mesmo."""
    comparar(tmp_path, conteudo.encode(), "x.csv")


# ============================================================================ fluxo completo: lotes -> prévia -> confirmação

def _enviar_em_lotes(client, lido, papel, tamanho=2):
    """Simula o navegador: várias requisições pequenas para /api/importacao/lote."""
    lotes = [(aba, linhas[i:i + tamanho]) for aba, linhas in enumerate(lido["abas"])
             for i in range(0, max(len(linhas), 1), tamanho)]
    token = None
    for n, (aba, linhas) in enumerate(lotes):
        corpo = {"aba": aba, "linhas": linhas, "final": n == len(lotes) - 1}
        if token:
            corpo["token"] = token
        else:
            corpo.update(arquivo=lido["arquivo"], planilhas=lido["planilhas"])
        if corpo["final"]:
            corpo["papel"] = papel
        res = client.post("/api/importacao/lote", json=corpo)
        assert res.status_code == 200, res.json
        token = res.json["token"]
    return res.json


@precisa_node
def test_importacao_pelo_navegador_da_o_mesmo_resultado_do_arquivo(tmp_path, client, consulta, app, reabrir):
    from test_duas_planilhas import clientes_xlsx, contratos_xlsx, payload, up

    # 1) caminho antigo: arquivo enviado ao servidor (só a prévia, nada gravado)
    cli_a, con_a = up(client, clientes_xlsx(), "clientes.xlsx", "clientes"), up(client, contratos_xlsx(), "contratos.xlsx", "contratos")
    previa_arquivo = client.post("/api/importacao/previa", json=payload(cli_a, con_a)).json

    # 2) caminho novo: navegador lê e envia em lotes de 2 linhas
    cli = _enviar_em_lotes(client, ler_no_navegador(tmp_path, clientes_xlsx(), "clientes.xlsx"), "clientes")
    con = _enviar_em_lotes(client, ler_no_navegador(tmp_path, contratos_xlsx(), "contratos.xlsx"), "contratos")
    assert (cli["colunas"], cli["mapeamento"], cli["total_linhas"]) == (cli_a["colunas"], cli_a["mapeamento"], cli_a["total_linhas"])
    assert (con["colunas"], con["mapeamento"], con["chave"]) == (con_a["colunas"], con_a["mapeamento"], con_a["chave"])
    previa = client.post("/api/importacao/previa", json=payload(cli, con)).json
    assert previa["resumo"] == previa_arquivo["resumo"]
    assert previa["linhas"] == previa_arquivo["linhas"] and previa["inconsistencias"] == previa_arquivo["inconsistencias"]

    # 3) confirmação (usa o plano guardado na prévia) e reimportação sem duplicar
    feito = client.post("/api/importacao/confirmar", json=payload(cli, con))
    assert feito.status_code == 200, feito.json
    contagem = lambda: [consulta(f"SELECT COUNT(*) FROM {t}")[0][0]
                        for t in ("clientes", "contratos", "contrato_valores", "pagamentos_mensais")]
    depois = contagem()
    assert depois[0] == 4 and depois[1] == 4
    cli2 = _enviar_em_lotes(client, ler_no_navegador(tmp_path, clientes_xlsx(), "clientes.xlsx"), "clientes")
    con2 = _enviar_em_lotes(client, ler_no_navegador(tmp_path, contratos_xlsx(), "contratos.xlsx"), "contratos")
    assert client.post("/api/importacao/confirmar", json=payload(cli2, con2)).status_code == 200
    assert contagem() == depois


def test_lote_recusa_conteudo_invalido(client):
    base = {"arquivo": "a.xlsx", "planilhas": ["P"], "aba": 0}
    assert client.post("/api/importacao/lote", json={**base, "linhas": "x"}).status_code == 400
    assert client.post("/api/importacao/lote", json={**base, "linhas": [[1, [{"x": 1}]]]}).status_code == 400
    assert client.post("/api/importacao/lote", json={**base, "linhas": [[1, [[1, 2]]]]}).status_code == 400
    assert client.post("/api/importacao/lote", json={**base, "arquivo": "a.exe", "linhas": []}).status_code == 400
    assert client.post("/api/importacao/lote", json={"token": "../x", "linhas": []}).status_code == 400


def test_lote_respeita_expiracao_e_limite(client, consulta):
    r = client.post("/api/importacao/lote", json={"arquivo": "a.csv", "planilhas": [], "linhas": [[1, ["A", "B"]]]})
    token = r.json["token"]
    consulta("UPDATE arquivos_importacao SET criado_em = '2000-01-01T00:00:00' WHERE token = %s", (token,))
    res = client.post("/api/importacao/lote", json={"token": token, "linhas": [[2, ["1", "2"]]]})
    assert res.status_code == 400 and "expirou" in res.json["erro"]


def test_lote_final_devolve_colunas_e_mapeamento(client):
    linhas = [[1, ["Código", "Razão Social", "CNPJ", "Valor", "Mês"]], [2, [1, "Alfa", "11222333000181", 500, "11/2026"]]]
    res = client.post("/api/importacao/lote", json={"arquivo": "c.csv", "planilhas": [], "linhas": linhas,
                                                    "final": True, "papel": "clientes"})
    assert res.status_code == 200, res.json
    assert res.json["arquivo"] == "c.csv" and res.json["colunas"][0]["nome"] == "Código"
    assert res.json["mapeamento"]["razao_social"] == 1 and res.json["total_linhas"] == 1


def test_confirmacao_refaz_a_analise_se_o_banco_mudou_depois_da_previa(client, consulta):
    """O plano da prévia só é reaproveitado se nada mudou; senão a confirmação analisa de novo."""
    linhas = [[1, ["Código", "Razão Social", "Valor", "Mês"]], [2, ["A1", "Alfa", "500,00", "11/2026"]]]
    up = client.post("/api/importacao/lote", json={"arquivo": "c.csv", "planilhas": [], "linhas": linhas,
                                                   "final": True, "papel": "clientes"}).json
    corpo = {"clientes": {"token": up["token"], "planilha": up["planilha"], "mapeamento": up["mapeamento"]},
             "mes_padrao": "2026-11", "arquivo_clientes": "c.csv"}
    assert client.post("/api/importacao/previa", json=corpo).json["resumo"]["clientes_novos"] == 1
    # outra pessoa cadastra o mesmo código entre a prévia e a confirmação
    assert client.post("/api/clientes", json={"codigo": "A1", "razao_social": "Cadastrado antes"}).status_code == 201
    feito = client.post("/api/importacao/confirmar", json=corpo)
    assert feito.status_code == 200, feito.json
    assert feito.json["clientes_novos"] == 0 and feito.json["clientes_atualizados"] == 1
    assert consulta("SELECT COUNT(*) FROM clientes")[0][0] == 1
