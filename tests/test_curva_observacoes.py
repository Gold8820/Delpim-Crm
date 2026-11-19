"""Curva ABC configurável (limites no banco, validação, reclassificação, persistência) e
observações de clientes e contratos (edição, importação, separação e preservação)."""
import io

import pytest
from openpyxl import Workbook

from clientes.validation import merge_note, to_note

H_CLI = ["Código", "Razão Social", "CNPJ", "Observações"]
H_CON = ["Documento Cliente", "Nº Contrato", "Produto", "Valor Mensal", "Observacao"]
CNPJ_A = "11222333000181"
CNPJ_B = "11444777000161"


def xlsx(rows, sheet="Planilha1"):
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def up(client, content, name, papel=None):
    data = {"arquivo": (io.BytesIO(content), name)}
    if papel:
        data["papel"] = papel
    res = client.post("/api/importacao/arquivo", data=data, content_type="multipart/form-data")
    assert res.status_code == 200, res.json
    return res.json


def faixas_body(a_plus=(1000, None), a=(400, 999.99), b=(180, 399.99), c=(0, 179.99)):
    """Monta o corpo do PUT da Curva ABC. Valores em reais, texto no formato brasileiro."""
    def fmt(v):
        return "" if v is None else f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    limites = {"A+": a_plus, "A": a, "B": b, "C": c}
    return {"faixas": [{"classificacao": k, "minimo": fmt(v[0]), "maximo": fmt(v[1])} for k, v in limites.items()]}


def salvar_curva(client, **kw):
    return client.put("/api/configuracoes/curva-abc", json=faixas_body(**kw))


def classe_do_cliente(client, codigo, mes="2026-11"):
    lista = client.get("/api/clientes", query_string={"mes": mes}).json["clientes"]
    return next(c for c in lista if c["codigo"] == codigo)["classificacao"]


def novo_cliente(client, codigo, valor, mes="2026-11", **extra):
    res = client.post("/api/clientes", json={"codigo": codigo, "nome_fantasia": f"Cli {codigo}",
                                              "valor": valor, "mes": mes, **extra})
    assert res.status_code == 201, res.json
    return res.json["id"]


def faixas_no_banco(consulta):
    rows = consulta("SELECT classificacao, minimo_centavos, maximo_centavos FROM curvas_abc "
                    "ORDER BY minimo_centavos DESC")
    return [tuple(r.values()) for r in rows]


# ============================================================================ Curva ABC

def test_valores_iniciais_vem_do_banco(client):
    res = client.get("/api/configuracoes/curva-abc")
    assert res.status_code == 200
    por_classe = {f["classificacao"]: f for f in res.json["faixas"]}
    assert por_classe["A+"]["minimo_centavos"] == 100_000 and por_classe["A+"]["maximo_centavos"] is None
    assert por_classe["A"]["minimo_centavos"] == 40_000 and por_classe["A"]["maximo_centavos"] == 99_999
    assert por_classe["B"]["minimo"] == "180,00" and por_classe["B"]["maximo"] == "399,99"
    assert por_classe["C"]["minimo_centavos"] == 0 and por_classe["C"]["maximo"] == "179,99"


def test_meta_expoe_as_faixas_do_banco(client):
    meta = client.get("/api/meta").json
    assert {f["classificacao"] for f in meta["faixas"]} == {"A+", "A", "B", "C"}


def test_alterar_limites_reclassifica_clientes_ja_cadastrados(client):
    novo_cliente(client, "1", "350,00")   # era B (180 a 399,99)
    novo_cliente(client, "2", "500,00")   # era A
    assert classe_do_cliente(client, "1") == "B"

    res = salvar_curva(client, a_plus=(800, None), a=(300, 799.99), b=(150, 299.99), c=(0, 149.99))
    assert res.status_code == 200, res.json
    assert res.json["reclassificados"] >= 1
    assert classe_do_cliente(client, "1") == "A"      # 350 agora está na faixa A
    assert classe_do_cliente(client, "2") == "A"      # continua A
    assert "reclassificado" in res.json["mensagem"]


def test_nova_faixa_vale_para_pagamento_lancado_em_outros_meses(client):
    novo_cliente(client, "1", "120,00", mes="2026-10")
    assert classe_do_cliente(client, "1", mes="2026-10") == "C"
    salvar_curva(client, b=(100, 399.99), c=(0, 99.99))
    assert classe_do_cliente(client, "1", mes="2026-10") == "B"


def test_pagamento_novo_usa_as_faixas_alteradas(client):
    salvar_curva(client, a_plus=(800, None), a=(300, 799.99), b=(150, 299.99), c=(0, 149.99))
    novo_cliente(client, "1", "320,00")
    assert classe_do_cliente(client, "1") == "A"
    novo_cliente(client, "2", "800,00")
    assert classe_do_cliente(client, "2") == "A+"


def test_sem_mudanca_nao_reclassifica_nada(client):
    novo_cliente(client, "1", "350,00")
    res = salvar_curva(client)  # mesmos valores iniciais
    assert res.status_code == 200 and res.json["reclassificados"] == 0
    assert res.json["mensagem"] == "Nenhum pagamento precisou ser reclassificado."


def test_persistencia_reabrindo_o_banco(client, reabrir):
    salvar_curva(client, a_plus=(900, None), a=(350, 899.99), b=(150, 349.99), c=(0, 149.99))
    reaberto = reabrir().test_client()
    faixas = {f["classificacao"]: f for f in reaberto.get("/api/configuracoes/curva-abc").json["faixas"]}
    assert faixas["A+"]["minimo_centavos"] == 90_000
    assert faixas["A"]["minimo_centavos"] == 35_000 and faixas["A"]["maximo_centavos"] == 89_999
    assert faixas["B"]["maximo_centavos"] == 34_999


def test_reinicializar_o_banco_nao_recria_os_valores_iniciais(client, reabrir, consulta):
    salvar_curva(client, a_plus=(900, None), a=(350, 899.99), b=(150, 349.99), c=(0, 149.99))
    reabrir()   # init_db e migrações rodam de novo
    assert faixas_no_banco(consulta) == [("A+", 90_000, None), ("A", 35_000, 89_999),
                                        ("B", 15_000, 34_999), ("C", 0, 14_999)]


def test_tabela_tem_uma_linha_por_classe(consulta):
    assert len(faixas_no_banco(consulta)) == 4


@pytest.mark.parametrize("kwargs,campo", [
    ({"a": (400, 999.99), "b": (180, 400)}, "B.maximo"),        # sobreposição: B vai até 400 e A começa em 400
    ({"a": (400, 900), "b": (180, 399.99)}, "A.maximo"),        # lacuna entre 900 e 1.000
    ({"a_plus": (1000, 5000)}, "A+.maximo"),                    # A+ não tem teto
    ({"c": (5, 179.99)}, "C.minimo"),                           # C precisa começar em zero
])
def test_limites_invalidos_sao_recusados_e_nada_muda(client, consulta, kwargs, campo):
    antes = faixas_no_banco(consulta)
    res = salvar_curva(client, **kwargs)
    assert res.status_code == 400
    assert campo in res.json["campos"], res.json
    assert faixas_no_banco(consulta) == antes


def test_sobreposicao_e_lacuna_geram_mensagem_clara(client):
    res = salvar_curva(client, a=(400, 999.99), b=(180, 400))
    assert res.status_code == 400
    assert "Sobrepõe a faixa A" in res.json["campos"]["B.maximo"]

    res = salvar_curva(client, a=(400, 900), b=(180, 399.99))
    assert any("sem classificação" in m for m in res.json["campos"].values())


def test_texto_invalido_no_limite_e_recusado(client):
    body = faixas_body()
    body["faixas"][1]["minimo"] = "quatrocentos"
    res = client.put("/api/configuracoes/curva-abc", json=body)
    assert res.status_code == 400 and "A.minimo" in res.json["campos"]


def test_classe_faltando_e_recusada(client, consulta):
    antes = faixas_no_banco(consulta)
    body = faixas_body()
    body["faixas"] = [f for f in body["faixas"] if f["classificacao"] != "C"]
    res = client.put("/api/configuracoes/curva-abc", json=body)
    assert res.status_code == 400
    assert faixas_no_banco(consulta) == antes


def test_classe_desconhecida_e_recusada(client):
    body = faixas_body()
    body["faixas"][0]["classificacao"] = "Z"
    assert client.put("/api/configuracoes/curva-abc", json=body).status_code == 400


def test_corpo_invalido_e_recusado(client):
    assert client.put("/api/configuracoes/curva-abc", json={"x": 1}).status_code == 400
    assert client.put("/api/configuracoes/curva-abc", data="não é json", content_type="text/plain").status_code == 400


def test_limites_de_banda_sao_aceitos_e_contiguos(client):
    res = salvar_curva(client, a_plus=(1000, None), a=(400, 999.99), b=(180, 399.99), c=(0, 179.99))
    assert res.status_code == 200


# ============================================================================ Observações: texto

def test_texto_preserva_acentos_quebras_e_simbolos():
    texto = "Cliente pediu ação\r\nTel.: (41) 9999-0000 — ótimo & “urgente”\n\n  última linha  "
    assert to_note(texto) == "Cliente pediu ação\nTel.: (41) 9999-0000 — ótimo & “urgente”\n\n  última linha"


def test_texto_vazio_vira_none():
    assert to_note("   ") is None and to_note(None) is None


def test_texto_longo_e_recusado(client):
    res = client.post("/api/clientes", json={"codigo": "1", "nome_fantasia": "X", "observacoes": "a" * 4001})
    assert res.status_code == 400 and "observacoes" in res.json["campos"]


def test_observacao_multilinha_com_acentos_round_trip(client):
    texto = "Primeira linha: ação e educação\nSegunda linha — R$ 1.000,00 & 50%\n\nQuarta linha com “aspas”"
    cid = novo_cliente(client, "1", "100", observacoes=texto)
    assert client.get(f"/api/clientes/{cid}").json["observacoes"] == texto


def test_observacao_do_cliente_pode_ser_editada_e_apagada(client):
    cid = novo_cliente(client, "1", "100", observacoes="Antiga")
    res = client.put(f"/api/clientes/{cid}", json={"codigo": "1", "nome_fantasia": "Cli 1", "valor": "100",
                                                   "mes": "2026-11", "observacoes": "Nova\nlinha"})
    assert res.status_code == 200
    assert client.get(f"/api/clientes/{cid}").json["observacoes"] == "Nova\nlinha"
    res = client.put(f"/api/clientes/{cid}", json={"codigo": "1", "nome_fantasia": "Cli 1", "valor": "100",
                                                   "mes": "2026-11", "observacoes": ""})
    assert res.status_code == 200
    assert client.get(f"/api/clientes/{cid}").json["observacoes"] is None


# ============================================================================ Observações: contratos

def contrato_da_api(client, cid, numero):
    return next(c for c in client.get(f"/api/clientes/{cid}").json["contratos"] if c["numero"] == numero)


def importar_clientes_e_contratos(client, clientes_rows, contratos_rows):
    cli = up(client, xlsx([H_CLI, *clientes_rows]), "clientes.xlsx", "clientes")
    con = up(client, xlsx([H_CON, *contratos_rows]), "contratos.xlsx", "contratos")
    body = {"chave": "cnpj", "mes_padrao": "2026-11", "substituir_vazios": False,
            "arquivo_clientes": "clientes.xlsx", "arquivo_contratos": "contratos.xlsx",
            "clientes": {"token": cli["token"], "planilha": cli["planilha"], "mapeamento": cli["mapeamento"]},
            "contratos": {"token": con["token"], "planilha": con["planilha"], "mapeamento": con["mapeamento"]}}
    prev = client.post("/api/importacao/previa", json=body)
    assert prev.status_code == 200, prev.json
    done = client.post("/api/importacao/confirmar", json=body)
    assert done.status_code == 200, done.json
    return prev, done, con


def cliente_id(client, cnpj_digits_codigo):
    return next(c["id"] for c in client.get("/api/clientes").json["clientes"] if c["codigo"] == cnpj_digits_codigo)


def test_editar_observacao_do_contrato_manualmente(client):
    importar_clientes_e_contratos(client, [[1, "Alfa", CNPJ_A, "Geral"]],
                                  [[CNPJ_A, "C-1", "Plano", 600, "Obs original"]])
    cid = cliente_id(client, "1")
    contrato = contrato_da_api(client, cid, "C-1")
    assert contrato["observacoes"] == "Obs original"

    texto = "Renegociado em novembro.\nValor com desconto — ação & 50%."
    res = client.put(f"/api/clientes/{cid}/contratos/{contrato['id']}/observacoes", json={"observacoes": texto})
    assert res.status_code == 200
    assert contrato_da_api(client, cid, "C-1")["observacoes"] == texto


def test_observacao_do_contrato_pode_ser_apagada(client):
    importar_clientes_e_contratos(client, [[1, "Alfa", CNPJ_A, None]], [[CNPJ_A, "C-1", "Plano", 600, "X"]])
    cid = cliente_id(client, "1")
    contrato = contrato_da_api(client, cid, "C-1")
    res = client.put(f"/api/clientes/{cid}/contratos/{contrato['id']}/observacoes", json={"observacoes": "  "})
    assert res.status_code == 200
    assert contrato_da_api(client, cid, "C-1")["observacoes"] is None


def test_observacao_do_contrato_invalida_ou_inexistente(client):
    importar_clientes_e_contratos(client, [[1, "Alfa", CNPJ_A, None]], [[CNPJ_A, "C-1", "Plano", 600, None]])
    cid = cliente_id(client, "1")
    contrato = contrato_da_api(client, cid, "C-1")
    longo = client.put(f"/api/clientes/{cid}/contratos/{contrato['id']}/observacoes", json={"observacoes": "x" * 4001})
    assert longo.status_code == 400
    assert client.put(f"/api/clientes/{cid}/contratos/999/observacoes", json={"observacoes": "x"}).status_code == 404
    assert client.put(f"/api/clientes/999/contratos/{contrato['id']}/observacoes", json={"observacoes": "x"}).status_code == 404
    assert client.put(f"/api/clientes/{cid}/contratos/{contrato['id']}/observacoes", json=[1]).status_code == 400


def test_observacao_do_contrato_nao_pertence_a_outro_cliente(client):
    importar_clientes_e_contratos(client, [[1, "Alfa", CNPJ_A, None], [2, "Beta", CNPJ_B, None]],
                                  [[CNPJ_A, "C-1", "Plano", 600, None]])
    dono = cliente_id(client, "1")
    outro = cliente_id(client, "2")
    contrato = contrato_da_api(client, dono, "C-1")
    res = client.put(f"/api/clientes/{outro}/contratos/{contrato['id']}/observacoes", json={"observacoes": "x"})
    assert res.status_code == 404


# ============================================================================ Observações: importação e separação

def test_sugestao_de_mapeamento_plural_cliente_singular_contrato(client):
    cli = up(client, xlsx([H_CLI, [1, "Alfa", CNPJ_A, "Nota"]]), "c.xlsx", "clientes")
    con = up(client, xlsx([H_CON, [CNPJ_A, "C-1", "P", 600, "96"]]), "k.xlsx", "contratos")
    assert cli["mapeamento"]["observacoes"] is not None
    assert con["mapeamento"]["observacoes"] is not None
    singular_no_cliente = up(client, xlsx([["Código", "Razão Social", "Observacao"], [1, "Alfa", "x"]]),
                             "s.xlsx", "clientes")
    assert singular_no_cliente["mapeamento"]["observacoes"] is None


def test_observacoes_de_clientes_sao_importadas(client):
    importar_clientes_e_contratos(client, [[1, "Alfa", CNPJ_A, "Linha 1\nLinha 2 — ação ✓"]],
                                  [[CNPJ_A, "C-1", "P", 600, None]])
    assert client.get(f"/api/clientes/{cliente_id(client, '1')}").json["observacoes"] == "Linha 1\nLinha 2 — ação ✓"


def test_observacoes_de_contratos_sao_importadas_e_ficam_no_contrato(client):
    importar_clientes_e_contratos(client, [[1, "Alfa", CNPJ_A, "Geral do cliente"]],
                                  [[CNPJ_A, "C-1", "P", 600, "Específica do contrato"]])
    cid = cliente_id(client, "1")
    assert client.get(f"/api/clientes/{cid}").json["observacoes"] == "Geral do cliente"
    assert contrato_da_api(client, cid, "C-1")["observacoes"] == "Específica do contrato"


def test_observacoes_de_cliente_e_contrato_nao_se_misturam(client):
    importar_clientes_e_contratos(client, [[1, "Alfa", CNPJ_A, "Só do cliente"]],
                                  [[CNPJ_A, "C-1", "P", 600, "Só do contrato"],
                                   [CNPJ_A, "C-2", "P", 300, None]])
    cid = cliente_id(client, "1")
    assert client.get(f"/api/clientes/{cid}").json["observacoes"] == "Só do cliente"
    assert contrato_da_api(client, cid, "C-2")["observacoes"] is None
    assert contrato_da_api(client, cid, "C-1")["observacoes"] == "Só do contrato"


def test_editar_observacao_do_contrato_nao_altera_a_do_cliente(client):
    importar_clientes_e_contratos(client, [[1, "Alfa", CNPJ_A, "Geral"]], [[CNPJ_A, "C-1", "P", 600, "Antiga"]])
    cid = cliente_id(client, "1")
    contrato = contrato_da_api(client, cid, "C-1")
    client.put(f"/api/clientes/{cid}/contratos/{contrato['id']}/observacoes", json={"observacoes": "Nova"})
    assert client.get(f"/api/clientes/{cid}").json["observacoes"] == "Geral"


def test_editar_observacao_do_cliente_nao_altera_a_do_contrato(client):
    importar_clientes_e_contratos(client, [[1, "Alfa", CNPJ_A, "Geral"]], [[CNPJ_A, "C-1", "P", 600, "Do contrato"]])
    cid = cliente_id(client, "1")
    client.put(f"/api/clientes/{cid}", json={"codigo": "1", "nome_fantasia": "Alfa", "valor": "600",
                                             "mes": "2026-11", "observacoes": "Geral alterada"})
    assert contrato_da_api(client, cid, "C-1")["observacoes"] == "Do contrato"


def test_reimportacao_com_celula_vazia_preserva_observacao_do_cliente(client):
    importar_clientes_e_contratos(client, [[1, "Alfa", CNPJ_A, "Guardada"]], [[CNPJ_A, "C-1", "P", 600, None]])
    importar_clientes_e_contratos(client, [[1, "Alfa", CNPJ_A, None]], [[CNPJ_A, "C-1", "P", 600, None]])
    assert client.get(f"/api/clientes/{cliente_id(client, '1')}").json["observacoes"] == "Guardada"


def test_reimportacao_com_celula_vazia_preserva_observacao_do_contrato(client):
    importar_clientes_e_contratos(client, [[1, "Alfa", CNPJ_A, None]], [[CNPJ_A, "C-1", "P", 600, "Guardada"]])
    importar_clientes_e_contratos(client, [[1, "Alfa", CNPJ_A, None]], [[CNPJ_A, "C-1", "P", 650, None]])
    cid = cliente_id(client, "1")
    assert contrato_da_api(client, cid, "C-1")["observacoes"] == "Guardada"


def test_reimportacao_com_texto_diferente_mantem_o_cadastrado_e_avisa(client):
    importar_clientes_e_contratos(client, [[1, "Alfa", CNPJ_A, "Cadastrada"]], [[CNPJ_A, "C-1", "P", 600, None]])
    prev, _, _ = importar_clientes_e_contratos(client, [[1, "Alfa", CNPJ_A, "Texto diferente da planilha"]],
                                               [[CNPJ_A, "C-1", "P", 600, None]])
    assert any("Observações" in a for linha in prev.json["linhas"] for a in linha["avisos"])
    assert client.get(f"/api/clientes/{cliente_id(client, '1')}").json["observacoes"] == "Cadastrada"


def test_reimportacao_preenche_observacao_que_estava_vazia(client):
    importar_clientes_e_contratos(client, [[1, "Alfa", CNPJ_A, None]], [[CNPJ_A, "C-1", "P", 600, None]])
    importar_clientes_e_contratos(client, [[1, "Alfa", CNPJ_A, "Chegou agora"]], [[CNPJ_A, "C-1", "P", 600, None]])
    assert client.get(f"/api/clientes/{cliente_id(client, '1')}").json["observacoes"] == "Chegou agora"


def test_substituir_vazios_tambem_nao_apaga_observacoes(client):
    importar_clientes_e_contratos(client, [[1, "Alfa", CNPJ_A, "Cliente"]], [[CNPJ_A, "C-1", "P", 600, "Contrato"]])
    cli = up(client, xlsx([H_CLI, [1, "Alfa", CNPJ_A, None]]), "c.xlsx", "clientes")
    con = up(client, xlsx([H_CON, [CNPJ_A, "C-1", "P", 600, None]]), "k.xlsx", "contratos")
    body = {"chave": "cnpj", "mes_padrao": "2026-11", "substituir_vazios": True,
            "arquivo_clientes": "c.xlsx", "arquivo_contratos": "k.xlsx",
            "clientes": {"token": cli["token"], "planilha": cli["planilha"], "mapeamento": cli["mapeamento"]},
            "contratos": {"token": con["token"], "planilha": con["planilha"], "mapeamento": con["mapeamento"]}}
    assert client.post("/api/importacao/confirmar", json=body).status_code == 200
    cid = cliente_id(client, "1")
    assert client.get(f"/api/clientes/{cid}").json["observacoes"] == "Cliente"
    assert contrato_da_api(client, cid, "C-1")["observacoes"] == "Contrato"


def test_regra_de_mesclagem_de_observacoes():
    assert merge_note("A", None) == ("A", None)
    assert merge_note(None, "B") == ("B", None)
    assert merge_note("A", "A") == ("A", None)
    valor, aviso = merge_note("A", "B")
    assert valor == "A" and aviso


# ============================================================================ Curva + observações

def test_observacao_nao_interfere_na_classificacao(client):
    cid = novo_cliente(client, "1", "350,00", observacoes="Texto longo\ncom quebras")
    assert client.get("/api/clientes", query_string={"mes": "2026-11"}).json["clientes"][0]["classificacao"] == "B"
    assert client.get(f"/api/clientes/{cid}").json["observacoes"] == "Texto longo\ncom quebras"
