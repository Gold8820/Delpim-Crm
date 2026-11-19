"""Formatos reais de planilha: XML do Excel 2003 (.xls), planilha única, cabeçalho manual e reimportações.

Os dados são sintéticos (CNPJs de teste), mas seguem a estrutura das planilhas reais usadas na validação:
contratos com "Nº", "Documento Cliente" e "Valor Atual"; cadastro com "Documento Cliente" e "Cliente".
"""
import io

from openpyxl import Workbook

from clientes.importer import suggest_mapping

CNPJ_A = "11.222.333/0001-81"
CNPJ_B = "45.997.418/0001-53"
CNPJ_C = "12.345.678/0001-95"
MES = "2026-11"

H_CONTRATOS_REAIS = ["status", "Nº", "Empresa", "Documento Empresa", "Cliente", "Documento Cliente",
                     "Classificação Cliente", "Valor Original", "Valor Atual", "Vigência", "Inicio Contrato",
                     "Termino Contrato", "Encerrado", "Data Encerramento", "Vencimento", "Quantidade Parcelas",
                     "Observacao", "Próx. reajuste", "Pagamento", "Atrasado", "Quitado", "Valor Parcela Atrasado",
                     "Quantidade ParcelaFuturo", "Valor Parcela Futuro", "Periodo Reajuste"]


def xlsx_bytes(rows):
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def spreadsheetml(rows, sheet="ag-grid"):
    """XML do Excel 2003 (SpreadsheetML), como o arquivo .xls exportado pelo sistema de origem."""
    def cell(v, idx=None):
        attr = f' ss:Index="{idx}"' if idx else ""
        if v is None:
            return f"<Cell{attr}/>"
        if isinstance(v, bool):
            return f'<Cell{attr}><Data ss:Type="Boolean">{int(v)}</Data></Cell>'
        if isinstance(v, (int, float)):
            return f'<Cell{attr}><Data ss:Type="Number">{v}</Data></Cell>'
        return f'<Cell{attr}><Data ss:Type="String">{v}</Data></Cell>'
    body = []
    for r_i, row in enumerate(rows, start=1):
        cells = "".join(cell(v) for v in row)
        body.append(f"<Row>{cells}</Row>")
    return (
        '<?xml version="1.0"?>\r\n<?mso-application progid="Excel.Sheet"?>\r\n'
        '<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" '
        'xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">'
        f'<Worksheet ss:Name="{sheet}"><Table>{"".join(body)}</Table></Worksheet></Workbook>'
    ).encode("utf-8")


def upload(client, content, name, papel, linha=None):
    form = {"arquivo": (io.BytesIO(content), name), "papel": papel}
    if linha is not None:
        form["linha_cabecalho"] = str(linha)
    return client.post("/api/importacao/arquivo", data=form, content_type="multipart/form-data")


def as_papel(client, j, papel):
    """Mesmo arquivo (mesmo token) lido com a associação de outro papel, como faz a planilha única na tela."""
    res = client.post("/api/importacao/planilha", json={"token": j["token"], "papel": papel,
                                                         "planilha": j.get("planilha"), "manual": False})
    assert res.status_code == 200, res.get_json()
    return {**res.get_json(), "token": j["token"]}


def side(j, linha=None):
    d = {"token": j["token"], "planilha": j.get("planilha"), "mapeamento": j["mapeamento"],
         "linha_cabecalho": j.get("linha_cabecalho")}
    return d


def contratos_csv_rows(contracts):
    """Linhas no formato da planilha real de contratos (só os campos usados)."""
    rows = [H_CONTRATOS_REAIS]
    for numero, doc, valor_atual in contracts:
        row = [None] * len(H_CONTRATOS_REAIS)
        row[0], row[1], row[5], row[8], row[23] = "Vigente", numero, doc, valor_atual, "0"
        rows.append(row)
    return rows


def count(consulta, table):
    return consulta(f"SELECT COUNT(*) FROM {table}")[0][0]


# --------------------------------------------------------------------------- reconhecimento das colunas

def test_colunas_reais_de_contratos_sao_reconhecidas():
    m = suggest_mapping(H_CONTRATOS_REAIS, "contratos")
    assert H_CONTRATOS_REAIS[m["numero_contrato"]] == "Nº"
    assert H_CONTRATOS_REAIS[m["chave_cliente"]] == "Documento Cliente"
    assert H_CONTRATOS_REAIS[m["valor_mensal"]] == "Valor Atual"        # sugestão inicial, configurável
    assert H_CONTRATOS_REAIS[m["valor_parcela_futuro"]] == "Valor Parcela Futuro"
    assert H_CONTRATOS_REAIS[m["situacao"]] == "status"


def test_valor_parcela_futuro_e_valor_contrato_nunca_viram_valor_mensal():
    assert suggest_mapping(["Documento Cliente", "Nº", "Valor Parcela Futuro"], "contratos")["valor_mensal"] is None
    assert suggest_mapping(["Documento Cliente", "Nº", "Valor Contrato"], "contratos")["valor_mensal"] is None


def test_numero_de_endereco_nao_e_confundido_com_numero_de_contrato():
    m = suggest_mapping(["Documento Cliente", "Cliente", "Numero", "Logradouro"], "contratos")
    assert m["numero_contrato"] is None


def test_documento_cliente_reconhecido_como_cnpj_do_cadastro():
    m = suggest_mapping(["Cliente", "Documento Cliente", "Telefone"], "clientes")
    assert m["cnpj"] == 1 and m["razao_social"] == 0


# --------------------------------------------------------------------------- .xls (XML do Excel 2003)

def test_xls_xml_e_lido_e_gera_a_mesma_previa_do_csv(client):
    rows = contratos_csv_rows([("2482", CNPJ_A, 170), ("2481", CNPJ_B, 500)])
    res = upload(client, spreadsheetml(rows), "contratos.xls", "contratos")
    assert res.status_code == 200, res.get_json()
    j = res.get_json()
    assert j["total_linhas"] == 2
    assert j["colunas"][1]["nome"] == "Nº"
    assert j["mapeamento"]["numero_contrato"] == 1
    assert j["mapeamento"]["valor_mensal"] == 8

    res_csv = upload(client, "\n".join(";".join(str(c) if c is not None else "" for c in r) for r in rows).encode(),
                     "contratos.csv", "contratos")
    assert res_csv.get_json()["mapeamento"] == j["mapeamento"]


def test_xls_xml_aceita_celulas_com_indice_e_numeros(client):
    xml = ('<?xml version="1.0"?><Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" '
           'xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet"><Worksheet ss:Name="Dados"><Table>'
           '<Row><Cell><Data ss:Type="String">Documento Cliente</Data></Cell>'
           '<Cell ss:Index="3"><Data ss:Type="String">Nº</Data></Cell></Row>'
           f'<Row ss:Index="3"><Cell><Data ss:Type="String">{CNPJ_A}</Data></Cell>'
           '<Cell ss:Index="3"><Data ss:Type="Number">2500</Data></Cell></Row>'
           '</Table></Worksheet></Workbook>').encode()
    res = upload(client, xml, "x.xls", "contratos")
    j = res.get_json()
    assert res.status_code == 200
    assert [c["nome"] for c in j["colunas"]] == ["Documento Cliente", "Coluna 2", "Nº"]
    assert j["total_linhas"] == 1                      # linha 2 vazia é ignorada
    assert j["mapeamento"]["numero_contrato"] == 2


def test_xls_binario_antigo_e_recusado_com_mensagem_clara(client):
    res = upload(client, b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 200, "antigo.xls", "contratos")
    assert res.status_code == 400
    assert "binário" in res.get_json()["erro"]


def test_xls_que_nao_e_planilha_xml_e_recusado(client):
    res = upload(client, b"<html><body>nada</body></html>", "pagina.xls", "contratos")
    assert res.status_code == 400


# --------------------------------------------------------------------------- cabeçalho

def test_cabecalho_automatico_ignora_titulo_de_uma_celula(client):
    content = "Relatório de contratos\n\n;;\nDocumento Cliente;Nº;Valor Atual\n{}\n".format(f"{CNPJ_A};1;100").encode()
    res = upload(client, content, "com-titulo.csv", "contratos")
    j = res.get_json()
    assert res.status_code == 200
    assert j["colunas"][0]["nome"] == "Documento Cliente"
    assert j["total_linhas"] == 1


def test_linha_do_cabecalho_escolhida_manualmente(client):
    content = f"Título\nOutro título\nDocumento Cliente;Nº;Valor Atual\n{CNPJ_A};1;100\n".encode()
    res = upload(client, content, "titulos.csv", "contratos", linha=3)
    j = res.get_json()
    assert res.status_code == 200
    assert [c["nome"] for c in j["colunas"]] == ["Documento Cliente", "Nº", "Valor Atual"]
    assert j["linha_cabecalho"] == 3


def test_linha_do_cabecalho_inexistente_ou_vazia_gera_erro(client):
    content = f"Documento Cliente;Nº\n\n{CNPJ_A};1\n".encode()
    res = upload(client, content, "a.csv", "contratos", linha=99)
    assert res.status_code == 400 and "99" in res.get_json()["erro"]
    res = upload(client, content, "a.csv", "contratos", linha=2)
    assert res.status_code == 400 and "linha 2" in res.get_json()["erro"]


# --------------------------------------------------------------------------- planilha única

def test_planilha_unica_com_numero_de_contrato_cria_clientes_e_contratos(app, client, consulta):
    rows = [
        ["Documento Cliente", "Cliente", "Telefone", "Nº", "Valor Atual", "status"],
        [CNPJ_A, "Alfa LTDA", "(84) 3333-4444", "1001", 600, "Vigente"],
        [CNPJ_A, "Alfa LTDA", "(84) 3333-4444", "1002", 450, "Vigente"],   # 2º contrato do mesmo cliente
        [CNPJ_B, "Beta ME", None, "1003", 150, "Vigente"],
    ]
    j = upload(client, xlsx_bytes(rows), "unica.xlsx", "clientes").get_json()
    jc = as_papel(client, j, "contratos")
    body = {"clientes": side(j), "contratos": side(jc), "chave": "cnpj", "mes_padrao": MES,
            "arquivo_clientes": "unica.xlsx", "arquivo_contratos": "unica.xlsx"}
    prev = client.post("/api/importacao/previa", json=body).get_json()
    assert prev["resumo"]["tem_contratos"] is True
    assert prev["resumo"]["clientes_novos"] == 2            # o cliente repetido não é duplicado
    assert prev["resumo"]["duplicados"] == 0
    assert prev["resumo"]["contratos_relacionados"] == 3
    assert client.post("/api/importacao/confirmar", json=body).status_code == 200
    assert count(consulta, "clientes") == 2
    assert count(consulta, "contratos") == 3
    alfa = client.get("/api/clientes", query_string={"mes": MES}).get_json()["clientes"]
    valor = {c["cnpj"]: c["valor_centavos"] for c in alfa}
    assert valor["11.222.333/0001-81"] == 105000            # soma dos contratos do cliente no mês


def test_planilha_unica_sem_numero_importa_so_os_clientes(app, client, consulta):
    rows = [
        ["Documento Cliente", "Cliente", "Valor Contrato"],
        [CNPJ_A, "Alfa LTDA", 600],
        [CNPJ_B, "Beta ME", 150],
    ]
    j = upload(client, xlsx_bytes(rows), "sem-numero.xlsx", "clientes").get_json()
    body = {"clientes": side(j), "contratos": side(as_papel(client, j, "contratos")), "chave": "cnpj", "mes_padrao": MES}
    prev = client.post("/api/importacao/previa", json=body).get_json()
    assert prev["resumo"]["tem_contratos"] is False
    assert prev["resumo"]["clientes_novos"] == 2
    assert prev["avisos"] and "contratos não" in prev["avisos"][0]
    # “Valor Contrato” não é sugerido como valor mensal pago
    assert j["mapeamento"]["valor"] is None
    client.post("/api/importacao/confirmar", json=body)
    assert count(consulta, "clientes") == 2
    assert count(consulta, "contratos") == 0


# --------------------------------------------------------------------------- relacionamento e reimportação

def test_cliente_novo_sem_codigo_usa_o_cnpj_como_codigo(app, client, consulta):
    j = upload(client, xlsx_bytes([["Documento Cliente", "Cliente"], [CNPJ_C, "Gama SA"]]),
               "sem-codigo.xlsx", "clientes").get_json()
    body = {"clientes": side(j), "chave": "cnpj", "mes_padrao": MES}
    prev = client.post("/api/importacao/previa", json=body).get_json()
    assert prev["resumo"]["clientes_novos"] == 1
    assert any("CNPJ foi usado como código" in (r["avisos"][0] if r["avisos"] else "") for r in prev["linhas"])
    client.post("/api/importacao/confirmar", json=body)
    assert consulta("SELECT codigo FROM clientes")[0][0] == "12345678000195"


def test_reimportacao_semanal_nao_duplica_clientes_contratos_nem_valores(app, client, consulta):
    cli = upload(client, xlsx_bytes([["Código", "Documento Cliente", "Cliente"],
                                     ["10", CNPJ_A, "Alfa LTDA"], ["20", CNPJ_B, "Beta ME"]]),
                 "clientes.xlsx", "clientes").get_json()
    con = upload(client, xlsx_bytes(contratos_csv_rows([("C-1", CNPJ_A, 600), ("C-2", CNPJ_A, 450),
                                                        ("C-3", CNPJ_B, 150)])),
                 "contratos.xlsx", "contratos").get_json()
    body = {"clientes": side(cli), "contratos": side(con), "chave": "cnpj", "mes_padrao": MES}
    assert client.post("/api/importacao/confirmar", json=body).status_code == 200
    snapshot = (count(consulta, "clientes"), count(consulta, "contratos"), count(consulta, "contrato_valores"),
                count(consulta, "pagamentos_mensais"))
    assert snapshot == (2, 3, 3, 2)

    # semana seguinte: mesmos contratos, um valor alterado e um contrato novo
    con2 = upload(client, xlsx_bytes(contratos_csv_rows([("C-1", CNPJ_A, 620), ("C-2", CNPJ_A, 450),
                                                         ("C-3", CNPJ_B, 150), ("C-4", CNPJ_B, 80)])),
                  "contratos2.xlsx", "contratos").get_json()
    body2 = {**body, "contratos": side(con2)}
    prev = client.post("/api/importacao/previa", json=body2).get_json()
    assert prev["resumo"]["clientes_novos"] == 0
    assert prev["resumo"]["contratos_novos"] == 1
    assert prev["resumo"]["contratos_atualizados"] == 1
    assert client.post("/api/importacao/confirmar", json=body2).status_code == 200
    assert (count(consulta, "clientes"), count(consulta, "contratos")) == (2, 4)
    # o valor do mês do cliente é a soma atual dos contratos (C-1 = 620 + C-2 = 450)
    alfa = [c for c in client.get("/api/clientes", query_string={"mes": MES}).get_json()["clientes"]
            if "11.222.333/0001-81" in (c["cnpj"] or "")][0]
    assert alfa["valor_centavos"] == 107000

    # terceira importação idêntica: nada muda
    prev3 = client.post("/api/importacao/previa", json=body2).get_json()
    assert prev3["resumo"]["clientes_novos"] == 0 and prev3["resumo"]["contratos_novos"] == 0
    assert prev3["resumo"]["contratos_atualizados"] == 0
    client.post("/api/importacao/confirmar", json=body2)
    assert (count(consulta, "clientes"), count(consulta, "contratos"), count(consulta, "contrato_valores")) == (2, 4, 4)


def test_mapeamento_padrao_e_aplicado_automaticamente_na_proxima_importacao(app, client, consulta):
    con = upload(client, xlsx_bytes(contratos_csv_rows([("C-1", CNPJ_A, 600)])), "contratos.xlsx", "contratos").get_json()
    saved = client.post("/api/importacao/configuracoes/do-arquivo",
                        json={"papel": "contratos", "token": con["token"], "planilha": con["planilha"],
                              "mapeamento": con["mapeamento"], "chave": "cnpj"})
    assert saved.status_code == 200
    # próxima semana: colunas em outra ordem, mesmos nomes
    inversa = list(reversed(H_CONTRATOS_REAIS))
    linha = [None] * len(H_CONTRATOS_REAIS)
    linha[0], linha[1], linha[5], linha[8] = "Vigente", "C-1", CNPJ_A, 700
    trocada = [inversa, [linha[i] for i in reversed(range(len(linha)))]]
    again = upload(client, xlsx_bytes(trocada), "semana2.xlsx", "contratos").get_json()
    assert again["configuracao"] is not None
    assert again["pendencias"] == {}
    idx = {c["nome"]: c["indice"] for c in again["colunas"]}
    assert again["mapeamento"]["numero_contrato"] == idx["Nº"]
    assert again["mapeamento"]["valor_mensal"] == idx["Valor Atual"]


def test_cabecalho_renomeado_destaca_somente_o_campo_afetado(app, client, consulta):
    con = upload(client, xlsx_bytes(contratos_csv_rows([("C-1", CNPJ_A, 600)])), "contratos.xlsx", "contratos").get_json()
    client.post("/api/importacao/configuracoes/do-arquivo",
                json={"papel": "contratos", "token": con["token"], "planilha": con["planilha"],
                      "mapeamento": con["mapeamento"], "chave": "cnpj"})
    linha = [None] * len(H_CONTRATOS_REAIS)
    linha[0], linha[1], linha[5], linha[8] = "Vigente", "C-1", CNPJ_A, 650
    renomeada = [[h.replace("Valor Atual", "Valor Vigente") for h in H_CONTRATOS_REAIS], linha]
    again = upload(client, xlsx_bytes(renomeada), "renomeada.xlsx", "contratos").get_json()
    assert set(again["pendencias"]) == {"valor_mensal"}
    sugestao = again["pendencias"]["valor_mensal"].get("sugestao")
    assert sugestao is None or "parcela" not in again["colunas"][sugestao]["nome"].lower()
    # os demais campos continuam associados (não foram desfeitos)
    assert again["mapeamento"]["numero_contrato"] == 1 and again["mapeamento"]["chave_cliente"] == 5
