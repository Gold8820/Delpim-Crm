"""Importação com duas planilhas (clientes + contratos), mapeamentos padrão e migração do banco."""
import io
from pathlib import Path

import psycopg
from openpyxl import Workbook


CNPJ_A, CNPJ_B, CNPJ_C, CNPJ_D = "11222333000181", "11444777000161", "45997418000153", "00623904000173"
FIXTURE = Path(__file__).parent / "fixtures" / "etapa1.db"

H_CLI = ["Código", "Razão Social", "CNPJ", "Cidade", "UF", "Segmento", "Vendedor"]
H_CON = ["Documento Cliente", "Nº Contrato", "Produto", "Situação", "Início", "Valor Mensal", "Valor Parcela Futuro"]


def xlsx(rows, sheet="Planilha1"):
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def clientes_xlsx(extra=()):
    return xlsx([
        H_CLI,
        [1, "Alfa LTDA", "11.222.333/0001-81", "Curitiba", "PR", "Varejo", "João"],
        [2, "Beta ME", CNPJ_B, "Recife", "PE", None, None],
        [3, "Gama SA", "45.997.418/0001-53", "Belém", "PA", None, None],
        [4, "Sem Contrato SA", CNPJ_D, "Natal", "RN", None, None],
        *extra,
    ])


def contratos_xlsx(extra=(), rows=None):
    base = rows if rows is not None else [
        [CNPJ_A, "C-100", "Plano A", "Ativo", "01/01/2026", 600, 5000],          # sem pontuação
        ["11.222.333/0001-81", "C-101", "Plano B", "Ativo", "15/02/2026", 500, 3000],  # com pontuação
        [CNPJ_B, "C-200", "Plano A", "Ativo", "01/03/2026", 150, 900],
        ["45.997.418/0001-53", "C-300", "Plano C", "Ativo", "01/04/2026", 100, 99999],
        ["99.999.999/0001-99", "C-900", "Plano X", "Ativo", "01/05/2026", 10, 0],  # cliente inexistente
        [CNPJ_A, "C-100", "Plano A", "Ativo", "01/01/2026", 600, 5000],          # repetido
    ]
    return xlsx([H_CON, *base, *extra])


def up(client, content, name, papel):
    res = client.post("/api/importacao/arquivo", data={"arquivo": (io.BytesIO(content), name), "papel": papel},
                      content_type="multipart/form-data")
    assert res.status_code == 200, res.json
    return res.json


def payload(cli=None, con=None, chave="cnpj", mes="2026-11", substituir=False, con_map=None, cli_map=None):
    body = {"chave": chave, "mes_padrao": mes, "substituir_vazios": substituir,
            "arquivo_clientes": "clientes.xlsx", "arquivo_contratos": "contratos.xlsx"}
    if cli:
        body["clientes"] = {"token": cli["token"], "planilha": cli["planilha"], "mapeamento": cli_map or cli["mapeamento"]}
    if con:
        body["contratos"] = {"token": con["token"], "planilha": con["planilha"], "mapeamento": con_map or con["mapeamento"]}
    return body


def importar(client, cli_bytes=None, con_bytes=None, confirm=True, **kw):
    cli = up(client, cli_bytes, "clientes.xlsx", "clientes") if cli_bytes else None
    con = up(client, con_bytes, "contratos.xlsx", "contratos") if con_bytes else None
    body = payload(cli, con, chave=kw.pop("chave", None) or (con or {}).get("chave") or "cnpj", **kw)
    prev = client.post("/api/importacao/previa", json=body)
    if not confirm:
        return prev
    assert prev.status_code == 200, prev.json
    done = client.post("/api/importacao/confirmar", json=body)
    assert done.status_code == 200, done.json
    return prev, done


def lista(client, **params):
    return client.get("/api/clientes", query_string={"mes": "2026-11", **params}).json["clientes"]


def por_codigo(client, **params):
    return {c["codigo"]: c for c in lista(client, **params)}


def contar(consulta, tabela):
    return consulta(f"SELECT COUNT(*) FROM {tabela}")[0][0]


# ----------------------------------------------------------------------------- reconhecimento automático

def test_colunas_dos_dois_arquivos_sao_reconhecidas(client):
    cli = up(client, clientes_xlsx(), "c.xlsx", "clientes")
    con = up(client, contratos_xlsx(), "k.xlsx", "contratos")
    m = con["mapeamento"]
    assert (m["chave_cliente"], m["numero_contrato"], m["valor_mensal"], m["valor_parcela_futuro"], m["data_inicio"]) == (0, 1, 5, 6, 4)
    assert con["chave"] == "cnpj"
    assert cli["mapeamento"]["cnpj"] == 2 and cli["mapeamento"]["codigo"] == 0


def test_parcela_futuro_nunca_e_sugerida_como_valor_mensal(client):
    con = up(client, xlsx([["Documento Cliente", "Contrato", "Valor Parcela Futuro"], [CNPJ_A, "X1", 900]]), "k.xlsx", "contratos")
    assert con["mapeamento"]["valor_mensal"] is None and con["mapeamento"]["valor_parcela_futuro"] == 2


# ----------------------------------------------------------------------------- relacionamento

def test_relaciona_contratos_sem_duplicar_clientes(client, app, consulta):
    prev, done = importar(client, clientes_xlsx(), contratos_xlsx())
    r = prev.json["resumo"]
    assert (r["clientes_lidos"], r["contratos_lidos"]) == (4, 6)
    assert (r["contratos_relacionados"], r["contratos_sem_cliente"], r["clientes_sem_contrato"]) == (4, 1, 1)
    assert (r["duplicados"], r["invalidos"]) == (1, 0)
    assert (r["clientes_novos"], r["contratos_novos"]) == (4, 4)
    assert contar(consulta, "clientes") == 4 and contar(consulta, "contratos") == 4
    c = por_codigo(client)
    assert c["1"]["valor_centavos"] == 110000 and c["1"]["classificacao"] == "A+"   # 600 + 500
    assert c["2"]["classificacao"] == "C" and c["3"]["valor_centavos"] == 10000
    assert c["4"]["classificacao"] is None
    detalhe = client.get(f"/api/clientes/{c['1']['id']}").json
    assert sorted(x["numero"] for x in detalhe["contratos"]) == ["C-100", "C-101"]
    assert detalhe["contratos"][0]["produto"] and detalhe["contratos"][0]["parcela_futuro_centavos"]


def test_valor_parcela_futuro_nao_entra_na_classificacao(client):
    importar(client, clientes_xlsx(), contratos_xlsx())
    assert por_codigo(client)["3"]["classificacao"] == "C"   # parcela futura era R$ 99.999


def test_inconsistencias_listadas_e_exportadas(client):
    prev = importar(client, clientes_xlsx(), contratos_xlsx(), confirm=False)
    tipos = {(i["planilha"], i["tipo"]): i for i in prev.json["inconsistencias"]}
    sem = tipos[("contratos", "sem_cliente")]
    assert sem["identificacao"] == "C-900" and sem["linha"] == 6 and "99.999.999/0001-99" in sem["motivo"]
    assert tipos[("contratos", "duplicado")]["linha"] == 7
    assert tipos[("clientes", "cliente_sem_contrato")]["nome"] == "Sem Contrato SA"
    cli = up(client, clientes_xlsx(), "c.xlsx", "clientes")
    con = up(client, contratos_xlsx(), "k.xlsx", "contratos")
    res = client.post("/api/importacao/inconsistencias.csv", json=payload(cli, con))
    texto = res.get_data(as_text=True)
    assert "C-900" in texto and "Sem Contrato SA" in texto and "Documento Cliente: 99.999.999/0001-99" in texto


def test_relaciona_por_codigo_do_cliente(client):
    con = xlsx([["Código do Cliente", "Número do Contrato", "Valor Mensal"], [1, "A1", 450], ["1", "A2", 100], [99, "A3", 5]])
    prev, _ = importar(client, clientes_xlsx(), con)
    assert prev.json["resumo"]["contratos_relacionados"] == 2 and prev.json["resumo"]["contratos_sem_cliente"] == 1
    assert por_codigo(client)["1"]["valor_centavos"] == 55000


def test_cnpj_do_excel_sem_zero_a_esquerda_ainda_relaciona(client):
    con = xlsx([["Documento Cliente", "Contrato", "Valor Mensal"], [int(CNPJ_D), "Z1", 200]])   # 623904000173
    importar(client, clientes_xlsx(), con)
    assert por_codigo(client)["4"]["valor_centavos"] == 20000


def test_somente_contratos_usa_clientes_ja_cadastrados(client, app, consulta):
    importar(client, clientes_xlsx(), None)
    assert contar(consulta, "contratos") == 0
    prev, _ = importar(client, None, contratos_xlsx())
    assert prev.json["resumo"]["contratos_relacionados"] == 4 and prev.json["resumo"]["clientes_novos"] == 0
    assert contar(consulta, "clientes") == 4 and por_codigo(client)["1"]["classificacao"] == "A+"


def test_cliente_da_planilha_rejeitado_explica_contrato(client):
    cli = xlsx([H_CLI, [1, "Ruim", "123", None, None, None, None]])
    con = xlsx([H_CON, ["123", "K1", None, None, None, 10, None]])
    prev = importar(client, cli, con, confirm=False)
    erros = [i["motivo"] for i in prev.json["inconsistencias"] if i["planilha"] == "contratos"]
    assert erros and "rejeitado na planilha de clientes" in erros[0]


# ----------------------------------------------------------------------------- semanas seguintes

def test_importacao_semanal_nao_duplica_e_substitui_totais(client, app, consulta):
    importar(client, clientes_xlsx(), contratos_xlsx())
    prev, _ = importar(client, clientes_xlsx(), contratos_xlsx())
    r = prev.json["resumo"]
    assert (r["contratos_novos"], r["contratos_atualizados"], r["contratos_sem_alteracao"]) == (0, 0, 4)
    assert (r["clientes_novos"], r["clientes_atualizados"]) == (0, 0)
    assert contar(consulta, "clientes") == 4 and contar(consulta, "contratos") == 4 and contar(consulta, "contrato_valores") == 4

    novo = [[CNPJ_A, "C-100", "Plano A", "Ativo", "01/01/2026", 700, 5000],
            ["11.222.333/0001-81", "C-101", "Plano B", "Ativo", "15/02/2026", 500, 3000]]
    importar(client, clientes_xlsx(), contratos_xlsx(rows=novo))
    assert por_codigo(client)["1"]["valor_centavos"] == 120000   # 700 + 500, e não 1.100 + 1.200
    assert contar(consulta, "contratos") == 4


def test_contrato_novo_soma_aos_existentes_do_cliente(client):
    importar(client, clientes_xlsx(), contratos_xlsx())
    importar(client, None, contratos_xlsx(rows=[[CNPJ_A, "C-102", "Plano C", "Ativo", "01/06/2026", 300, None]]))
    assert por_codigo(client)["1"]["valor_centavos"] == 140000   # C-100 600 + C-101 500 + C-102 300


def test_celulas_vazias_nao_apagam_dados_do_contrato(client):
    importar(client, clientes_xlsx(), contratos_xlsx())
    importar(client, None, contratos_xlsx(rows=[[CNPJ_A, "C-100", None, None, None, 600, None]]))
    detalhe = client.get(f"/api/clientes/{por_codigo(client)['1']['id']}").json
    c100 = next(c for c in detalhe["contratos"] if c["numero"] == "C-100")
    assert c100["produto"] == "Plano A" and c100["situacao"] == "Ativo" and c100["data_inicio"] == "2026-01-01"
    assert c100["parcela_futuro_centavos"] == 500000


def test_substituir_vazios_so_quando_o_usuario_escolhe(client):
    importar(client, clientes_xlsx(), contratos_xlsx())
    importar(client, None, contratos_xlsx(rows=[[CNPJ_A, "C-100", None, None, None, 600, None]]), substituir=True)
    detalhe = client.get(f"/api/clientes/{por_codigo(client)['1']['id']}").json
    assert next(c for c in detalhe["contratos"] if c["numero"] == "C-100")["produto"] is None


def test_historico_por_mes_com_coluna_de_mes(client):
    con = xlsx([["Documento Cliente", "Nº Contrato", "Valor Mensal", "Mês de Referência"],
                [CNPJ_A, "C-100", 100, "10/2026"], [CNPJ_A, "C-100", 700, "11/2026"]])
    prev, _ = importar(client, clientes_xlsx(), con)
    assert prev.json["resumo"]["invalidos"] == 0 and prev.json["resumo"]["contratos_relacionados"] == 2
    assert por_codigo(client, mes="2026-10")["1"]["classificacao"] == "C"
    assert por_codigo(client, mes="2026-11")["1"]["classificacao"] == "B" or por_codigo(client, mes="2026-11")["1"]["valor_centavos"] == 70000


def test_cadastro_existente_e_atualizado_sem_duplicar_e_preserva_dados(client, app, consulta):
    client.post("/api/clientes", json={"codigo": "1", "razao_social": "Alfa Antiga", "cnpj": CNPJ_A,
                                       "telefone": "(41) 3333-4444", "email": "a@a.com"})
    prev, _ = importar(client, clientes_xlsx(), contratos_xlsx())
    assert prev.json["resumo"]["clientes_novos"] == 3 and prev.json["resumo"]["clientes_atualizados"] == 1
    assert contar(consulta, "clientes") == 4
    c = client.get("/api/clientes", query_string={"mes": "2026-11"}).json["clientes"][0]
    assert c["razao_social"] == "Alfa LTDA" and c["telefone"] == "(41) 3333-4444" and c["email"] == "a@a.com"


# ----------------------------------------------------------------------------- valor mensal selecionável

def test_usuario_escolhe_qual_coluna_e_o_valor_mensal(client):
    con_bytes = xlsx([["Documento Cliente", "Contrato", "Valor A", "Valor B"], [CNPJ_A, "K1", 50, 450]])
    con = up(client, con_bytes, "k.xlsx", "contratos")
    assert con["mapeamento"]["valor_mensal"] is None           # nada é assumido
    mapa = {**con["mapeamento"], "valor_mensal": 3}            # usuário escolhe "Valor B"
    cli = up(client, clientes_xlsx(), "c.xlsx", "clientes")
    body = payload(cli, con, con_map=mapa)
    assert client.post("/api/importacao/confirmar", json=body).status_code == 200
    assert por_codigo(client)["1"]["valor_centavos"] == 45000


def test_aviso_quando_valor_mensal_parece_parcela_futura(client):
    con = up(client, contratos_xlsx(), "k.xlsx", "contratos")
    cli = up(client, clientes_xlsx(), "c.xlsx", "clientes")
    prev = client.post("/api/importacao/previa", json=payload(cli, con, con_map={**con["mapeamento"], "valor_mensal": 6}))
    assert any("parcela futura" in a for a in prev.json["avisos"])


def test_sem_valor_mensal_associado_importa_contratos_e_avisa(client, app, consulta):
    con = up(client, contratos_xlsx(), "k.xlsx", "contratos")
    cli = up(client, clientes_xlsx(), "c.xlsx", "clientes")
    body = payload(cli, con, con_map={**con["mapeamento"], "valor_mensal": None})
    prev = client.post("/api/importacao/previa", json=body)
    assert prev.json["avisos"] and client.post("/api/importacao/confirmar", json=body).status_code == 200
    assert contar(consulta, "contratos") == 4 and all(c["classificacao"] is None for c in lista(client))


# ----------------------------------------------------------------------------- invalidos e conflitos

def test_contratos_invalidos_e_conflitos(client):
    con = xlsx([H_CON,
                [CNPJ_A, None, "P", "Ativo", None, 10, None],              # sem número
                [CNPJ_A, "K2", "P", "Ativo", "31/02/2026", 10, None],      # data inexistente
                [CNPJ_A, "K3", "P", "Ativo", None, "abc", None],           # valor inválido
                [CNPJ_A, "K4", "P", "Ativo", None, 10, None],
                [CNPJ_B, "K4", "P", "Ativo", None, 10, None],              # mesmo contrato, outro cliente
                [None, "K5", "P", "Ativo", None, 10, None]])               # sem documento
    prev, _ = importar(client, clientes_xlsx(), con)
    r = prev.json["resumo"]
    assert r["contratos_relacionados"] == 1 and r["invalidos"] == 5
    assert {i["tipo"] for i in prev.json["inconsistencias"] if i["planilha"] == "contratos"} == {"invalido", "conflito"}


def test_contrato_ja_cadastrado_para_outro_cliente_e_rejeitado(client):
    importar(client, clientes_xlsx(), contratos_xlsx())
    prev = importar(client, None, contratos_xlsx(rows=[[CNPJ_B, "C-100", None, None, None, 10, None]]), confirm=False)
    assert prev.json["resumo"]["invalidos"] == 1 and prev.json["resumo"]["contratos_relacionados"] == 0
    assert "outro cliente" in prev.json["inconsistencias"][0]["motivo"]


def test_segmento_e_vendedor_dos_contratos_nao_sobrescrevem_a_planilha_de_clientes(client):
    con = xlsx([["Documento Cliente", "Contrato", "Valor Mensal", "Segmento", "Vendedor"],
                [CNPJ_A, "K1", 10, "Indústria", "Outro Vendedor"],
                [CNPJ_B, "K2", 10, "Serviços", "Ana"]])
    importar(client, clientes_xlsx(), con)
    c = por_codigo(client)
    assert (c["1"]["segmento"], c["1"]["vendedor"]) == ("Varejo", "João")     # veio da planilha de clientes
    assert (c["2"]["segmento"], c["2"]["vendedor"]) == ("Serviços", "Ana")    # só existia nos contratos


def test_valor_da_planilha_de_clientes_e_ignorado_quando_ha_contratos(client):
    cli_bytes = xlsx([H_CLI + ["Valor Pago"], [1, "Alfa", CNPJ_A, None, None, None, None, 99999]])
    cli = up(client, cli_bytes, "c.xlsx", "clientes")
    assert cli["mapeamento"]["valor"] == 7
    con = up(client, xlsx([H_CON, [CNPJ_A, "K1", None, None, None, 150, None]]), "k.xlsx", "contratos")
    client.post("/api/importacao/confirmar", json=payload(cli, con, mes="2026-11"))
    assert por_codigo(client)["1"]["valor_centavos"] == 15000


def test_exclusao_do_cliente_remove_seus_contratos(client, app, consulta):
    importar(client, clientes_xlsx(), contratos_xlsx())
    assert client.delete(f"/api/clientes/{por_codigo(client)['1']['id']}").status_code == 200
    assert contar(consulta, "contratos") == 2 and contar(consulta, "contrato_valores") == 2


def test_confirmacao_e_tudo_ou_nada(client, app, monkeypatch, consulta):
    from clientes import importer
    cli = up(client, clientes_xlsx(), "c.xlsx", "clientes")
    con = up(client, contratos_xlsx(), "k.xlsx", "contratos")
    def boom(*a, **k):
        raise psycopg.OperationalError("falha simulada")
    # Falha no último passo da gravação (valores do mês, gravados em lote): nada pode ficar gravado.
    monkeypatch.setattr(importer, "gravar_pagamentos", boom)
    res = client.post("/api/importacao/confirmar", json=payload(cli, con))
    assert res.status_code == 500
    assert contar(consulta, "clientes") == 0 and contar(consulta, "contratos") == 0


def test_previa_nao_grava_nada(client, app, consulta):
    importar(client, clientes_xlsx(), contratos_xlsx(), confirm=False)
    assert contar(consulta, "clientes") == 0 and contar(consulta, "contratos") == 0


def test_validacoes_do_mapeamento_de_contratos(client):
    cli = up(client, clientes_xlsx(), "c.xlsx", "clientes")
    con = up(client, contratos_xlsx(), "k.xlsx", "contratos")
    sem_numero = client.post("/api/importacao/previa", json=payload(cli, con, con_map={**con["mapeamento"], "numero_contrato": None}))
    assert sem_numero.status_code == 400 and "número do contrato" in sem_numero.json["erro"]
    sem_chave = client.post("/api/importacao/previa", json=payload(cli, con, con_map={**con["mapeamento"], "chave_cliente": None}))
    assert sem_chave.status_code == 400 and "identifica o cliente" in sem_chave.json["erro"]
    cli_sem_cnpj = {**cli["mapeamento"], "cnpj": None}
    r = client.post("/api/importacao/previa", json=payload(cli, con, cli_map=cli_sem_cnpj))
    assert r.status_code == 400 and "CNPJ" in r.json["erro"]


# ----------------------------------------------------------------------------- mapeamento padrão

def definir_padrao(client, papel, info, **extra):
    res = client.post("/api/importacao/configuracoes/do-arquivo", json={
        "papel": papel, "token": info["token"], "planilha": info["planilha"],
        "mapeamento": info["mapeamento"], "chave": info.get("chave"), **extra})
    assert res.status_code == 200, res.json
    return res.json


def test_definir_como_padrao_e_aplicar_automaticamente_na_proxima_vez(client):
    cli = up(client, clientes_xlsx(), "c.xlsx", "clientes")
    con = up(client, contratos_xlsx(), "k.xlsx", "contratos")
    # o usuário corrige manualmente uma associação e salva como padrão
    con["mapeamento"]["produto"] = None
    a = definir_padrao(client, "clientes", cli)
    b = definir_padrao(client, "contratos", {**con, "chave": "cnpj"})
    assert a["padrao"] and b["padrao"] and a["mapeamento"]["cnpj"] == "CNPJ" and b["mapeamento"]["valor_mensal"] == "Valor Mensal"
    assert b["chave"] == "cnpj" and b["mapeamento"]["produto"] is None

    # semanas depois: mesmas planilhas, mas com as COLUNAS EM OUTRA ORDEM
    novo_cli = xlsx([["Vendedor", "UF", "CNPJ", "Razão Social", "Código", "Cidade", "Segmento"],
                     ["João", "PR", CNPJ_A, "Alfa LTDA", 1, "Curitiba", "Varejo"]])
    novo_con = xlsx([["Valor Parcela Futuro", "Valor Mensal", "Nº Contrato", "Documento Cliente", "Produto", "Situação", "Início"],
                     [10, 420, "C-100", CNPJ_A, "Plano Z", "Ativo", "01/01/2026"]])
    cli2 = up(client, novo_cli, "c2.xlsx", "clientes")
    con2 = up(client, novo_con, "k2.xlsx", "contratos")
    assert cli2["pendencias"] == {} and con2["pendencias"] == {}
    assert cli2["configuracao"]["nome"].startswith("Clientes") and con2["configuracao"]["padrao"]
    assert cli2["mapeamento"]["codigo"] == 4 and cli2["mapeamento"]["cnpj"] == 2     # por nome, não por posição
    assert con2["mapeamento"]["valor_mensal"] == 1 and con2["mapeamento"]["numero_contrato"] == 2
    assert con2["mapeamento"]["produto"] is None      # o usuário tinha deixado sem associar; continua assim
    importar_ok = client.post("/api/importacao/confirmar", json=payload(cli2, con2))
    assert importar_ok.status_code == 200 and por_codigo(client)["1"]["valor_centavos"] == 42000


def test_cabecalho_alterado_destaca_somente_o_campo_afetado(client):
    con = up(client, contratos_xlsx(), "k.xlsx", "contratos")
    definir_padrao(client, "contratos", {**con, "chave": "cnpj"})
    renomeado = xlsx([["Documento Cliente", "Nº Contrato", "Produto", "Situação", "Início", "Mensalidade Paga", "Valor Parcela Futuro"],
                      [CNPJ_A, "C-1", "P", "Ativo", "01/01/2026", 10, 1]])
    novo = up(client, renomeado, "k2.xlsx", "contratos")
    assert list(novo["pendencias"]) == ["valor_mensal"]
    assert "Valor Mensal" in novo["pendencias"]["valor_mensal"]["motivo"]
    assert novo["pendencias"]["valor_mensal"]["sugestao"] == 5          # "Mensalidade Paga" é sugerida
    assert novo["mapeamento"]["numero_contrato"] == 1 and novo["mapeamento"]["chave_cliente"] == 0   # o resto preservado


def test_coluna_removida_fica_sem_sugestao(client):
    con = up(client, contratos_xlsx(), "k.xlsx", "contratos")
    definir_padrao(client, "contratos", {**con, "chave": "cnpj"})
    novo = up(client, xlsx([["Documento Cliente", "Nº Contrato", "Valor Mensal"], [CNPJ_A, "C-1", 1]]), "k2.xlsx", "contratos")
    assert set(novo["pendencias"]) == {"produto", "situacao", "data_inicio", "valor_parcela_futuro"}
    assert novo["mapeamento"]["produto"] is None and novo["mapeamento"]["valor_mensal"] == 2


def test_um_padrao_por_tipo_e_gerenciamento(client):
    cli = up(client, clientes_xlsx(), "c.xlsx", "clientes")
    con = up(client, contratos_xlsx(), "k.xlsx", "contratos")
    c1 = definir_padrao(client, "clientes", cli)
    k1 = definir_padrao(client, "contratos", {**con, "chave": "cnpj"})
    c2 = definir_padrao(client, "clientes", cli)                      # novo salvamento sem config_id => nova config
    assert c2["id"] != c1["id"] and c2["padrao"]
    todas = client.get("/api/importacao/configuracoes").json
    assert [c["padrao"] for c in todas["clientes"]] == [True, False] and todas["clientes"][0]["id"] == c2["id"]
    assert len(todas["contratos"]) == 1 and todas["contratos"][0]["padrao"]   # padrões separados por tipo

    assert client.post(f"/api/importacao/configuracoes/{c1['id']}/padrao").json["padrao"]
    todas = client.get("/api/importacao/configuracoes").json
    assert {c["id"]: c["padrao"] for c in todas["clientes"]} == {c1["id"]: True, c2["id"]: False}
    assert todas["contratos"][0]["padrao"]                            # não afetou contratos

    # editar associações e nome
    editado = client.put(f"/api/importacao/configuracoes/{k1['id']}", json={
        "nome": "Contratos semanais", "chave": "codigo",
        "mapeamento": {**k1["mapeamento"], "chave_cliente": "Cód. Cliente", "valor_mensal": "Pago"}}).json
    assert editado["nome"] == "Contratos semanais" and editado["chave"] == "codigo"
    assert editado["mapeamento"]["valor_mensal"] == "Pago" and editado["padrao"]

    # excluir
    assert client.delete(f"/api/importacao/configuracoes/{c2['id']}").status_code == 200
    assert client.delete(f"/api/importacao/configuracoes/{c2['id']}").status_code == 404
    assert len(client.get("/api/importacao/configuracoes").json["clientes"]) == 1


def test_salvar_novamente_com_a_config_em_uso_atualiza_em_vez_de_criar(client):
    cli = up(client, clientes_xlsx(), "c.xlsx", "clientes")
    c1 = definir_padrao(client, "clientes", cli)
    cli2 = up(client, clientes_xlsx(), "c.xlsx", "clientes")
    assert cli2["configuracao"]["id"] == c1["id"]
    cli2["mapeamento"]["segmento"] = None
    c1b = definir_padrao(client, "clientes", cli2, config_id=c1["id"])
    assert c1b["id"] == c1["id"] and c1b["mapeamento"]["segmento"] is None
    assert len(client.get("/api/importacao/configuracoes").json["clientes"]) == 1


def test_escolher_outra_configuracao_ou_modo_manual(client):
    cli = up(client, clientes_xlsx(), "c.xlsx", "clientes")
    a = definir_padrao(client, "clientes", cli)
    cli["mapeamento"]["cidade"] = None
    b = client.post("/api/importacao/configuracoes", json={
        "tipo": "clientes", "nome": "Sem cidade", "mapeamento": {"codigo": "Código", "razao_social": "Razão Social", "cnpj": "CNPJ"}}).json
    r = client.post("/api/importacao/planilha", json={"token": cli["token"], "papel": "clientes", "config_id": b["id"]}).json
    assert r["configuracao"]["nome"] == "Sem cidade" and r["mapeamento"]["cidade"] is None
    manual = client.post("/api/importacao/planilha", json={"token": cli["token"], "papel": "clientes", "manual": True}).json
    assert manual["configuracao"] is None and manual["mapeamento"]["cidade"] == 3
    assert [c["nome"] for c in r["configuracoes"]][0] == a["nome"]


def test_validacoes_das_configuracoes(client):
    base = {"tipo": "clientes", "nome": "X", "mapeamento": {"codigo": "Código"}}
    assert client.post("/api/importacao/configuracoes", json=base).status_code == 200
    assert client.post("/api/importacao/configuracoes", json=base).status_code == 409           # nome repetido
    assert client.post("/api/importacao/configuracoes", json={**base, "nome": ""}).status_code == 400
    assert client.post("/api/importacao/configuracoes", json={**base, "nome": "Y", "mapeamento": {"inexistente": "A"}}).status_code == 400
    assert client.post("/api/importacao/configuracoes", json={**base, "nome": "Z", "mapeamento": {"codigo": None}}).status_code == 400
    assert client.post("/api/importacao/configuracoes", json={"tipo": "contratos", "nome": "K", "mapeamento": {"valor_mensal": "V"}}).status_code == 400
    assert client.post("/api/importacao/configuracoes", json={**base, "tipo": "outro"}).status_code == 400
    assert client.post("/api/importacao/configuracoes/999/padrao").status_code == 404
    assert client.put("/api/importacao/configuracoes/999", json={}).status_code == 404


def test_mapeamento_padrao_persiste_apos_reiniciar(client, reabrir):
    definir_padrao(client, "clientes", up(client, clientes_xlsx(), "c.xlsx", "clientes"))
    c2 = reabrir().test_client()
    assert c2.get("/api/importacao/configuracoes").json["clientes"][0]["padrao"]


# ----------------------------------------------------------------------------- migração e compatibilidade



def test_cabecalho_do_valor_mensal_removido_nao_sugere_parcela_futuro(client):
    con = up(client, contratos_xlsx(), "k.xlsx", "contratos")
    definir_padrao(client, "contratos", {**con, "chave": "cnpj"})
    novo = up(client, xlsx([["Documento Cliente", "Nº Contrato", "Produto", "Situação", "Início", "Valor Parcela Futuro"],
                            [CNPJ_A, "C-1", "P", "Ativo", "01/01/2026", 1]]), "k2.xlsx", "contratos")
    assert list(novo["pendencias"]) == ["valor_mensal"]
    assert novo["pendencias"]["valor_mensal"]["sugestao"] is None and novo["mapeamento"]["valor_mensal"] is None
