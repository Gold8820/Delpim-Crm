"""Testes do sistema de clientes (cadastro, edição, exclusão, importação, duplicidades, classificação)."""
import io

import pytest
from openpyxl import Workbook

from clientes.classification import classify

# Faixas iniciais da Curva ABC (as mesmas gravadas no banco na criação). Os testes desta
# seção conferem as regras de classificação com elas.
FAIXAS_INICIAIS = [
    {"classificacao": "A+", "minimo_centavos": 100_000, "maximo_centavos": None},
    {"classificacao": "A", "minimo_centavos": 40_000, "maximo_centavos": 99_999},
    {"classificacao": "B", "minimo_centavos": 18_000, "maximo_centavos": 39_999},
    {"classificacao": "C", "minimo_centavos": 0, "maximo_centavos": 17_999},
]
from clientes.validation import parse_cnpj, parse_money, parse_month

# CNPJs válidos (dígitos verificadores corretos) usados apenas nos testes.
CNPJ_A = "11222333000181"
CNPJ_B = "11444777000161"
CNPJ_C = "45997418000153"


def novo(client, **kw):
    data = {"codigo": "1", "nome_fantasia": "Cliente Um", **kw}
    return client.post("/api/clientes", json=data)


def xlsx_bytes(rows, sheet="Planilha1"):
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def upload(client, content: bytes, name: str):
    res = client.post("/api/importacao/arquivo", data={"arquivo": (io.BytesIO(content), name)},
                      content_type="multipart/form-data")
    return res


def run_import(client, content, name, mapping=None, mes="2026-11", substituir=False, confirm=True):
    up = upload(client, content, name)
    assert up.status_code == 200, up.json
    info = up.json
    body = {"token": info["token"], "planilha": info["planilha"],
            "mapeamento": mapping or info["mapeamento_sugerido"], "mes_padrao": mes,
            "substituir_vazios": substituir, "arquivo": name}
    prev = client.post("/api/importacao/previa", json=body)
    if not confirm:
        return prev
    assert prev.status_code == 200, prev.json
    done = client.post("/api/importacao/confirmar", json=body)
    return prev, done


def listar(client, **params):
    return client.get("/api/clientes", query_string=params).json


# ----------------------------------------------------------------------------- classificação

@pytest.mark.parametrize("cents,esperado", [
    (None, None), (0, "C"), (17999, "C"), (18000, "B"), (39999, "B"),
    (40000, "A"), (99999, "A"), (100000, "A+"), (5_000_000, "A+"),
])
def test_faixas_de_classificacao(cents, esperado):
    assert classify(cents, FAIXAS_INICIAIS) == esperado


def test_valor_nao_informado_nao_vira_c(client):
    novo(client)  # sem valor
    c = listar(client)["clientes"][0]
    assert c["valor_centavos"] is None and c["classificacao"] is None


@pytest.mark.parametrize("texto,centavos", [
    ("1.234,56", 123456), ("R$ 1.234,56", 123456), ("1234,5", 123450), ("1234.56", 123456),
    ("1.500", 150000), ("179,99", 17999), (400, 40000), (399.99, 39999), ("0", 0),
])
def test_parse_valores(texto, centavos):
    assert parse_money(texto) == (centavos, None)


@pytest.mark.parametrize("texto", ["abc", "-10", "1,2,3"])
def test_parse_valores_invalidos(texto):
    assert parse_money(texto)[1]


@pytest.mark.parametrize("entrada,esperado", [
    ("2026-11", "2026-11"), ("11/2026", "2026-11"), ("nov/2026", "2026-11"), ("Novembro de 2026", "2026-11"),
    ("15/11/2026", "2026-11"), ("202611", "2026-11"), ("mar/26", "2026-03"),
])
def test_parse_mes(entrada, esperado):
    assert parse_month(entrada) == (esperado, None)


def test_cnpj_validacao():
    assert parse_cnpj("11.222.333/0001-81") == (CNPJ_A, None)
    assert parse_cnpj("11222333000182")[1] == "CNPJ inválido."
    assert parse_cnpj("1122233300018")[1]  # 13 dígitos (zeros à esquerda perdidos) continua inválido
    assert parse_cnpj("00000000000000")[1]


def test_cnpj_alfanumerico():
    # CNPJ alfanumérico (padrão a partir de 2026): 12 caracteres + 2 dígitos verificadores
    base = "12ABC34501DE"
    def dv(b, w):
        r = sum((ord(c) - 48) * x for c, x in zip(b, w)) % 11
        return 0 if r < 2 else 11 - r
    w1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    d1 = dv(base, w1); d2 = dv(base + str(d1), [6] + w1)
    assert parse_cnpj(f"{base}{d1}{d2}") == (f"{base}{d1}{d2}", None)


def test_cnpj_perde_zero_a_esquerda_no_excel():
    ok = "00623904000173"  # CNPJ real válido com zeros à esquerda
    assert parse_cnpj(int(ok)) == (ok, None)


# ----------------------------------------------------------------------------- cadastro manual

def test_cadastro_manual_completo_aparece_na_tabela(client):
    res = novo(client, razao_social="Empresa Um LTDA", cnpj="11.222.333/0001-81", telefone="(11) 99999-0000",
               email="Contato@Empresa.com", cep="01310-100", endereco="Av. Paulista", numero="1000",
               complemento="Sala 2", bairro="Bela Vista", cidade="São Paulo", estado="sp", segmento="Varejo",
               vendedor="Maria", valor="1.250,00", mes="2026-11")
    assert res.status_code == 201, res.json
    c = listar(client)["clientes"][0]
    assert (c["codigo"], c["cnpj"], c["cep"], c["estado"], c["email"]) == \
           ("1", "11.222.333/0001-81", "01310-100", "SP", "contato@empresa.com")
    assert c["valor_centavos"] == 125000 and c["classificacao"] == "A+"


def test_validacoes_do_formulario(client):
    res = client.post("/api/clientes", json={"cnpj": "123", "email": "x", "estado": "XX", "valor": "abc", "cep": "12"})
    assert res.status_code == 400
    campos = res.json["campos"]
    assert {"codigo", "razao_social", "cnpj", "email", "estado", "valor", "cep"} <= set(campos)


def test_valor_exige_mes(client):
    res = novo(client, valor="100")
    assert res.status_code == 400 and "mes" in res.json["campos"]


def test_codigo_unico_e_cnpj_unico(client):
    assert novo(client, cnpj=CNPJ_A).status_code == 201
    dup_codigo = novo(client, nome_fantasia="Outro")
    assert dup_codigo.status_code == 400 and "codigo" in dup_codigo.json["campos"]
    dup_cnpj = novo(client, codigo="2", cnpj=CNPJ_A)
    assert dup_cnpj.status_code == 400 and "cnpj" in dup_cnpj.json["campos"]
    assert listar(client)["total"] == 1


def test_codigo_unico_ignora_maiusculas(client):
    novo(client, codigo="ab1")
    assert novo(client, codigo="AB1").status_code == 400


def test_edicao_e_substituicao_do_valor_do_mes(client):
    cid = novo(client, valor="100", mes="2026-11").json["id"]
    res = client.put(f"/api/clientes/{cid}", json={"codigo": "1", "nome_fantasia": "Renomeado", "valor": "450,00", "mes": "2026-11"})
    assert res.status_code == 200
    c = listar(client, mes="2026-11")["clientes"][0]
    assert c["nome"] == "Renomeado" and c["valor_centavos"] == 45000 and c["classificacao"] == "A"
    assert len(res.json["pagamentos"]) == 1  # substituiu, não criou outro


def test_edicao_nao_conflita_com_o_proprio_registro_mas_conflita_com_outro(client):
    a = novo(client, cnpj=CNPJ_A).json["id"]
    novo(client, codigo="2", cnpj=CNPJ_B)
    ok = client.put(f"/api/clientes/{a}", json={"codigo": "1", "nome_fantasia": "X", "cnpj": CNPJ_A})
    assert ok.status_code == 200
    bad = client.put(f"/api/clientes/{a}", json={"codigo": "2", "nome_fantasia": "X"})
    assert bad.status_code == 400 and "codigo" in bad.json["campos"]


def test_historico_mensal_separado(client):
    cid = novo(client, valor="100", mes="2026-10").json["id"]
    client.put(f"/api/clientes/{cid}", json={"codigo": "1", "nome_fantasia": "Cliente Um", "valor": "1500", "mes": "2026-11"})
    assert listar(client, mes="2026-10")["clientes"][0]["classificacao"] == "C"
    assert listar(client, mes="2026-11")["clientes"][0]["classificacao"] == "A+"
    pagamentos = client.get(f"/api/clientes/{cid}").json["pagamentos"]
    assert [p["mes_referencia"] for p in pagamentos] == ["2026-11", "2026-10"]


def test_exclusao_remove_cliente_e_historico(client, consulta):
    cid = novo(client, valor="100", mes="2026-11").json["id"]
    assert client.delete(f"/api/clientes/{cid}").status_code == 200
    assert listar(client)["total_geral"] == 0
    assert consulta("SELECT COUNT(*) FROM pagamentos_mensais")[0][0] == 0
    assert client.delete(f"/api/clientes/{cid}").status_code == 404


def test_dados_persistem_apos_reiniciar(client, reabrir):
    novo(client, valor="500", mes="2026-11")
    c2 = reabrir().test_client()  # "reabre" o sistema
    assert listar(c2)["clientes"][0]["classificacao"] == "A"


# ----------------------------------------------------------------------------- pesquisa, filtros, ordenação

def test_pesquisa_filtros_e_ordenacao(client):
    novo(client, codigo="10", nome_fantasia="Padaria Pão Quente", cnpj=CNPJ_A, segmento="Alimentação", valor="1200", mes="2026-11")
    novo(client, codigo="2", nome_fantasia="Mercado Ágil", cnpj=CNPJ_B, segmento="Varejo", valor="150", mes="2026-11")
    novo(client, codigo="3", nome_fantasia="Açougue Central", segmento="Alimentação", valor="500", mes="2026-11")
    novo(client, codigo="4", nome_fantasia="Sem Valor")
    nomes = lambda **p: [c["nome"] for c in listar(client, **p)["clientes"]]

    assert nomes(q="padaria") == ["Padaria Pão Quente"]
    assert nomes(q="pao") == ["Padaria Pão Quente"]            # sem acento
    assert nomes(q="11.222.333/0001-81") == ["Padaria Pão Quente"]
    assert nomes(q="11222333") == ["Padaria Pão Quente"]
    assert nomes(q="10") == ["Padaria Pão Quente"]            # por código
    assert sorted(nomes(classificacao="A+")) == ["Padaria Pão Quente"]
    assert nomes(classificacao="none") == ["Sem Valor"]
    assert sorted(nomes(segmento="alimentação")) == ["Açougue Central", "Padaria Pão Quente"]
    assert nomes(ordem="codigo") == ["Mercado Ágil", "Açougue Central", "Sem Valor", "Padaria Pão Quente"]  # 2,3,4,10
    assert nomes(ordem="nome") == ["Açougue Central", "Mercado Ágil", "Padaria Pão Quente", "Sem Valor"]
    assert nomes(ordem="valor", direcao="desc")[:3] == ["Padaria Pão Quente", "Açougue Central", "Mercado Ágil"]
    assert listar(client, por_pagina=5, pagina=1)["total"] == 4
    assert "Alimentação" in client.get("/api/meta").json["segmentos"]


# ----------------------------------------------------------------------------- importação

CABECALHO = ["Cód. Cliente", "Razão Social", "Nome Fantasia", "CNPJ", "Cidade", "UF", "Segmento", "Vendedor", "Valor total pago"]


def test_importacao_excel_com_colunas_de_nomes_diferentes(client):
    conteudo = xlsx_bytes([
        CABECALHO,
        [101, "Alfa LTDA", "Alfa", CNPJ_A, "Curitiba", "PR", "Varejo", "João", 1250.5],
        [102, "Beta ME", "Beta", CNPJ_B, "Recife", "Pernambuco", "Indústria", "Ana", "R$ 450,00"],
        [103, "Gama SA", None, None, "Belém", "PA", None, None, 90],
    ])
    prev, done = run_import(client, conteudo, "clientes.xlsx")
    r = prev.json["resumo"]
    assert (r["clientes_novos"], r["clientes_atualizados"], r["linhas_rejeitadas"]) == (3, 0, 0)
    assert done.status_code == 200 and done.json["clientes_novos"] == 3
    por_codigo = {c["codigo"]: c for c in listar(client, mes="2026-11")["clientes"]}
    assert por_codigo["101"]["classificacao"] == "A+" and por_codigo["101"]["valor_centavos"] == 125050
    assert por_codigo["102"]["estado"] == "PE" and por_codigo["102"]["classificacao"] == "A"
    assert por_codigo["103"]["classificacao"] == "C" and por_codigo["103"]["nome"] == "Gama SA"


def test_importacao_csv_ponto_e_virgula_e_cp1252(client):
    texto = "Código;Nome;CNPJ;Cidade;Valor\n201;Café São João;;São Paulo;\"1.000,00\"\n202;Bar do Zé;;Niterói;399,99\n"
    prev, done = run_import(client, texto.encode("cp1252"), "clientes.csv")
    assert prev.json["resumo"]["clientes_novos"] == 2
    por = {c["codigo"]: c for c in listar(client, mes="2026-11")["clientes"]}
    assert por["201"]["nome"] == "Café São João" and por["201"]["classificacao"] == "A+"
    assert por["202"]["classificacao"] == "B" and por["202"]["cidade"] == "Niterói"


def test_importacao_csv_utf8_virgula(client):
    texto = "codigo,razao_social,valor\n301,Empresa X,\"400,00\"\n"
    prev, _ = run_import(client, texto.encode("utf-8-sig"), "a.csv")
    assert prev.json["resumo"]["clientes_novos"] == 1
    assert listar(client, mes="2026-11")["clientes"][0]["classificacao"] == "A"


def test_mapeamento_manual_de_colunas(client):
    conteudo = xlsx_bytes([["X", "Y", "Z"], ["500", "Empresa Y", 700]])
    up = upload(client, conteudo, "m.xlsx").json
    mapa = {k: None for k in ["codigo", "razao_social", "valor"]}
    mapa.update(codigo=0, razao_social=1, valor=2)
    prev = client.post("/api/importacao/previa", json={"token": up["token"], "mapeamento": mapa, "mes_padrao": "2026-11"})
    assert prev.json["resumo"]["clientes_novos"] == 1
    assert prev.json["linhas"][0]["classificacao"] == "A"


def test_previa_nao_grava_e_confirmacao_grava(client):
    conteudo = xlsx_bytes([CABECALHO, [1, "A", "A", None, None, None, None, None, 10]])
    run_import(client, conteudo, "x.xlsx", confirm=False)
    assert listar(client)["total_geral"] == 0
    run_import(client, conteudo, "x.xlsx")
    assert listar(client)["total_geral"] == 1


def test_linhas_invalidas_sao_rejeitadas_com_motivo(client):
    conteudo = xlsx_bytes([
        CABECALHO,
        [1, "Boa", "Boa", CNPJ_A, "X", "SP", None, None, 100],
        [2, "CNPJ ruim", None, "123", None, None, None, None, 100],
        [3, "Valor ruim", None, None, None, None, None, None, "abc"],
        [None, "Sem código", None, None, None, None, None, None, 100],
        [5, "UF ruim", None, None, None, "ZZ", None, None, 100],
        [6, None, None, None, None, None, None, None, 100],  # sem nome
    ])
    prev, done = run_import(client, conteudo, "r.xlsx")
    r = prev.json["resumo"]
    assert (r["clientes_novos"], r["linhas_rejeitadas"]) == (1, 5)
    motivos = {l["linha"]: " ".join(l["erros"]) for l in prev.json["linhas"] if l["status"] == "rejeitado"}
    assert "CNPJ" in motivos[3] and "Valor" in motivos[4] and "código" in motivos[5].lower()
    assert "Estado" in motivos[6] and "nome" in motivos[7].lower()
    assert listar(client)["total_geral"] == 1


def test_csv_de_rejeitadas(client):
    conteudo = xlsx_bytes([CABECALHO, [1, "A", None, "123", None, None, None, None, 1]])
    up = upload(client, conteudo, "r.xlsx").json
    res = client.post("/api/importacao/rejeitadas.csv", json={"token": up["token"], "mapeamento": up["mapeamento_sugerido"], "mes_padrao": "2026-11"})
    assert res.status_code == 200 and "Motivo" in res.get_data(as_text=True) and "CNPJ inválido" in res.get_data(as_text=True)


def test_duplicidade_por_codigo_atualiza_sem_duplicar(client):
    novo(client, codigo="7", razao_social="Antiga", cidade="Natal", valor="100", mes="2026-11")
    conteudo = xlsx_bytes([["Código", "Razão social", "Valor"], [7, "Nova Razão", 250]])
    prev, done = run_import(client, conteudo, "d.xlsx")
    assert prev.json["resumo"]["clientes_novos"] == 0 and prev.json["resumo"]["clientes_atualizados"] == 1
    assert done.json["clientes_atualizados"] == 1
    lista = listar(client, mes="2026-11")
    assert lista["total_geral"] == 1
    c = lista["clientes"][0]
    assert c["razao_social"] == "Nova Razão" and c["valor_centavos"] == 25000
    assert c["cidade"] == "Natal"  # coluna não mapeada: preservada


def test_duplicidade_por_cnpj_com_codigo_diferente_mantem_cadastro(client):
    novo(client, codigo="7", razao_social="Empresa", cnpj=CNPJ_A)
    conteudo = xlsx_bytes([["Código", "CNPJ", "Valor"], ["X99", CNPJ_A, 800]])
    prev, _ = run_import(client, conteudo, "d.xlsx")
    assert prev.json["resumo"]["clientes_atualizados"] == 1 and prev.json["linhas"][0]["avisos"]
    lista = listar(client, mes="2026-11")
    assert lista["total_geral"] == 1 and lista["clientes"][0]["codigo"] == "7"
    assert lista["clientes"][0]["classificacao"] == "A"


def test_conflito_codigo_de_um_cliente_e_cnpj_de_outro(client):
    novo(client, codigo="1", cnpj=CNPJ_A)
    novo(client, codigo="2", cnpj=CNPJ_B)
    conteudo = xlsx_bytes([["Código", "CNPJ"], ["1", CNPJ_B]])
    prev, _ = run_import(client, conteudo, "c.xlsx")
    assert prev.json["resumo"]["linhas_rejeitadas"] == 1


def test_celulas_vazias_preservam_dados_por_padrao(client):
    novo(client, codigo="7", razao_social="Empresa", cidade="Natal", telefone="(84) 3333-4444")
    conteudo = xlsx_bytes([["Código", "Cidade", "Telefone"], [7, None, "(84) 99999-0000"]])
    run_import(client, conteudo, "v.xlsx")
    c = listar(client)["clientes"][0]
    assert c["cidade"] == "Natal" and c["telefone"] == "(84) 99999-0000"


def test_celulas_vazias_substituem_quando_usuario_escolhe(client):
    novo(client, codigo="7", razao_social="Empresa", cidade="Natal")
    conteudo = xlsx_bytes([["Código", "Cidade"], [7, None]])
    run_import(client, conteudo, "v.xlsx", substituir=True)
    assert listar(client)["clientes"][0]["cidade"] is None


def test_planilha_diaria_substitui_total_acumulado_do_mes(client):
    novo(client, codigo="7", razao_social="Empresa")
    for total in (300, 520, 520):  # três dias seguidos: acumulado crescente e repetido
        conteudo = xlsx_bytes([["Código", "Valor total pago no mês"], [7, total]])
        run_import(client, conteudo, "dia.xlsx")
    c = listar(client, mes="2026-11")["clientes"][0]
    assert c["valor_centavos"] == 52000 and c["classificacao"] == "A"  # 520, e não 1.340


def test_importacao_com_coluna_de_mes_mantem_historico(client):
    conteudo = xlsx_bytes([
        ["Código", "Razão social", "Mês de referência", "Valor"],
        [1, "Hist", "10/2026", 100],
        [1, "Hist", "11/2026", 1500],
    ])
    prev, done = run_import(client, conteudo, "h.xlsx")
    assert prev.json["resumo"]["clientes_novos"] == 1 and prev.json["resumo"]["linhas_rejeitadas"] == 0
    assert done.json["pagamentos_gravados"] == 2
    assert listar(client, mes="2026-10")["clientes"][0]["classificacao"] == "C"
    assert listar(client, mes="2026-11")["clientes"][0]["classificacao"] == "A+"
    assert listar(client)["total_geral"] == 1


def test_repeticao_no_mesmo_mes_dentro_da_planilha_e_rejeitada(client):
    conteudo = xlsx_bytes([["Código", "Razão social", "Valor"], [1, "X", 100], [1, "X", 200]])
    prev, _ = run_import(client, conteudo, "r.xlsx")
    assert prev.json["resumo"]["clientes_novos"] == 1 and prev.json["resumo"]["linhas_rejeitadas"] == 1
    assert listar(client, mes="2026-11")["clientes"][0]["valor_centavos"] == 10000  # mantém a primeira


def test_valor_vazio_na_planilha_nao_apaga_nem_classifica(client):
    novo(client, codigo="7", razao_social="E", valor="500", mes="2026-11")
    conteudo = xlsx_bytes([["Código", "Cidade", "Valor"], [7, "Lima", None]])
    run_import(client, conteudo, "e.xlsx")
    c = listar(client, mes="2026-11")["clientes"][0]
    assert c["valor_centavos"] == 50000 and c["cidade"] == "Lima"


def test_sem_alteracao(client):
    novo(client, codigo="7", razao_social="E", valor="500", mes="2026-11")
    conteudo = xlsx_bytes([["Código", "Valor"], [7, 500]])
    prev, _ = run_import(client, conteudo, "s.xlsx")
    assert prev.json["resumo"]["clientes_sem_alteracao"] == 1 and prev.json["resumo"]["clientes_atualizados"] == 0


def test_erros_de_arquivo(client):
    assert upload(client, b"x", "a.txt").status_code == 400
    assert upload(client, b"x", "a.xls").status_code == 400
    assert upload(client, b"nao e xlsx", "a.xlsx").status_code == 400
    assert upload(client, xlsx_bytes([["Código"]]), "a.xlsx").status_code == 400  # só cabeçalho
    assert client.post("/api/importacao/previa", json={"token": "../../etc/passwd", "mapeamento": {}}).status_code == 400


def test_mapeamento_exige_codigo_ou_cnpj(client):
    up = upload(client, xlsx_bytes([["Nome"], ["X"]]), "a.xlsx").json
    res = client.post("/api/importacao/previa", json={"token": up["token"], "mapeamento": {"razao_social": 0}})
    assert res.status_code == 400 and "Código" in res.json["erro"]


def test_valor_sem_mes_padrao_nem_coluna_de_mes(client):
    up = upload(client, xlsx_bytes([["Código", "Valor"], [1, 10]]), "a.xlsx").json
    res = client.post("/api/importacao/previa", json={"token": up["token"], "mapeamento": up["mapeamento_sugerido"]})
    assert res.status_code == 400 and "mês" in res.json["erro"].lower()


def test_modelo_csv_so_tem_titulos(client):
    texto = client.get("/api/importacao/modelo.csv").get_data(as_text=True)
    assert texto.count("\n") == 1 and "Código do cliente" in texto


def test_pagina_principal(client):
    html = client.get("/").get_data(as_text=True)
    assert "Novo cliente" in html and "Importar planilha" in html and "<h1>Clientes</h1>" in html
