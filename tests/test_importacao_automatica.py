"""Importação automática em duas etapas (clientes, depois contratos), com os cabeçalhos REAIS das planilhas.

Os dados são fictícios; os nomes das colunas são exatamente os dos arquivos reais fornecidos.
Cada etapa usa o mesmo caminho da tela: linhas em lotes (/api/importacao/lote), prévia e confirmação,
com o mapeamento reconhecido automaticamente (sem associação manual).
"""
import time

import pytest

from clientes.validation import format_cnpj

CAB_CLIENTES = ["Pessoa", "Documento", "Razão Social", "Nome Fantasia", "Endereço Padrão", "Logradouro", "Numero",
                "Bairro", "Complemento", "CEP", "Cidade", "Referência", "Codigo IBGE/Cidade", "UF", "Contato Padrão",
                "Contato Cobrança", "Contato", "Telefone", "E-mail", "Inscricao Estadual", "Estado Inscrição Estadual",
                "Data de Nascimento", "Optante Simples Nacional", "Ramo Atividade", "Dias Antecedência Faturamento",
                "Dia Faturamento", "Configuração Faturamento", "Inscrição Municipal"]
CAB_CONTRATOS = ["status", "Nº", "Empresa", "Documento Empresa", "Cliente", "Documento Cliente", "Classificação Cliente",
                 "Valor Original", "Valor Atual", "Vigência", "Inicio Contrato", "Termino Contrato", "Encerrado",
                 "Data Encerramento", "Vencimento", "Quantidade Parcelas", "Observacao", "Próx. reajuste", "Pagamento",
                 "Atrasado", "Quitado", "Valor Parcela Atrasado", "Quantidade ParcelaFuturo", "Valor Parcela Futuro",
                 "Periodo Reajuste"]
MES = "2026-11"


def cnpj(n: int) -> str:
    base = [int(c) for c in f"{40000000 + n:08d}0001"]
    for pesos in ([5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2], [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]):
        r = sum(a * b for a, b in zip(base, pesos)) % 11
        base.append(0 if r < 2 else 11 - r)
    return "".join(map(str, base))


def cpf(n: int) -> str:
    base = [int(c) for c in f"{300000000 + n:09d}"]
    for tam in (9, 10):
        d = sum(a * b for a, b in zip(base, range(tam + 1, 1, -1))) * 10 % 11 % 10
        base.append(d)
    return "".join(map(str, base))


def linha_cliente(n, documento, pessoa="Jurídica", **mudar):
    d = dict(zip(CAB_CLIENTES, [pessoa, documento, f"Empresa Teste {n} Ltda", f"Fantasia {n}", "SIM", f"Rua {n}", str(n),
                                "Centro", "", "01310-100", "São Paulo", "Perto da praça" if n % 7 == 0 else "", "3550308",
                                "SP", "SIM", "NÃO", f"Contato {n}", "(11) 3333-4444", f"cliente{n}@exemplo.com.br",
                                "", "São Paulo", "", "NÃO", "Comércio", "10", "", "Empresa", ""]))
    d.update(mudar)
    return [d[c] for c in CAB_CLIENTES]


def linha_contrato(numero, documento, valor=300.0, encerrado=" Nao", atrasado=0, obs="", **mudar):
    d = dict(zip(CAB_CONTRATOS, ["Vigente", numero, "EMPRESA CONTRATADA LTDA", "12.995.328/0001-74", "Cliente Teste",
                                 documento, "3 Estrelas", 250, valor, "26/04/2020", "26/04/2020", "", encerrado, "", 20, 48,
                                 obs, "26/04/2027", "Recorrente", atrasado, 10, "0", 37, valor * 37, "Anual"]))
    d.update(mudar)
    return [d[c] for c in CAB_CONTRATOS]


def enviar(client, cabecalho, linhas, papel, nome, linhas_iniciais=(), lote=400):
    """Como o navegador: linhas numeradas (com linhas vazias no início, se houver) em lotes."""
    todas = [[i + 1, list(l)] for i, l in enumerate(list(linhas_iniciais) + [cabecalho] + linhas)]
    token, res = None, None
    for i in range(0, len(todas), lote):
        corpo = {"aba": 0, "linhas": todas[i:i + lote], "final": i + lote >= len(todas)}
        if token:
            corpo["token"] = token
        else:
            corpo.update(arquivo=nome, planilhas=["ag-grid"])
        if corpo["final"]:
            corpo["papel"] = papel
        res = client.post("/api/importacao/lote", json=corpo)
        assert res.status_code == 200, res.json
        token = res.json["token"]
    return res.json


def etapa(client, papel, cabecalho, linhas, nome=None, salvar_padrao=True, **kw):
    """Uma etapa completa da tela: envio, prévia e confirmação (mapeamento automático)."""
    up = enviar(client, cabecalho, linhas, papel, nome or f"{papel}.xls", **kw)
    corpo = {papel: {"token": up["token"], "planilha": up["planilha"], "mapeamento": up["mapeamento"]},
             "mes_padrao": MES, f"arquivo_{papel}": nome or f"{papel}.xls"}
    if papel == "contratos":
        corpo["chave"] = up["chave"]
    previa = client.post("/api/importacao/previa", json=corpo)
    assert previa.status_code == 200, previa.json
    feito = client.post("/api/importacao/confirmar", json=corpo)
    assert feito.status_code == 200, feito.json
    if salvar_padrao and not up["configuracao"]:
        r = client.post("/api/importacao/configuracoes/do-arquivo", json={
            "papel": papel, "token": up["token"], "planilha": up["planilha"], "mapeamento": up["mapeamento"],
            "chave": up["chave"] if papel == "contratos" else None})
        assert r.status_code == 200, r.json
    return up, previa.json, feito.json


def contagem(consulta):
    return {t: consulta(f"SELECT COUNT(*) FROM {t}")[0][0]
            for t in ("clientes", "contratos", "contrato_valores", "pagamentos_mensais")}


# ============================================================================ reconhecimento

def test_cabecalhos_reais_reconhecidos_sem_associacao_manual(client):
    up = enviar(client, CAB_CLIENTES, [linha_cliente(1, cnpj(1))], "clientes", "clientes.xls", linhas_iniciais=[[], []])
    assert up["colunas_desconhecidas"] == []                      # as 28 colunas reais foram reconhecidas
    nomes = {k: up["colunas"][i]["nome"] for k, i in up["mapeamento"].items() if i is not None}
    assert nomes["cnpj"] == "Documento" and nomes["razao_social"] == "Razão Social"
    assert nomes["endereco"] == "Logradouro" and nomes["segmento"] == "Ramo Atividade"
    assert nomes["referencia"] == "Referência" and "mes" not in nomes   # "Referência" é do endereço, não o mês
    assert nomes["inscricao_estadual"] == "Inscricao Estadual" and nomes["pessoa"] == "Pessoa"
    assert up["linha_cabecalho"] is None and up["total_linhas"] == 1  # cabeçalho achado depois das linhas vazias

    upc = enviar(client, CAB_CONTRATOS, [linha_contrato(1, cnpj(1))], "contratos", "contratos.xls")
    assert upc["colunas_desconhecidas"] == []                      # as 25 colunas reais foram reconhecidas
    nomes = {k: upc["colunas"][i]["nome"] for k, i in upc["mapeamento"].items() if i is not None}
    assert nomes["numero_contrato"] == "Nº" and nomes["chave_cliente"] == "Documento Cliente"
    assert nomes["valor_mensal"] == "Valor Atual" and nomes["valor_parcela_futuro"] == "Valor Parcela Futuro"
    assert nomes["observacoes"] == "Observacao" and nomes["cliente_nome"] == "Cliente"
    assert nomes["valor_original"] == "Valor Original" and nomes["quantidade_parcelas"] == "Quantidade Parcelas"
    assert upc["chave"] == "cnpj"


def test_variacoes_de_cabecalho_acentos_maiusculas_espacos(client):
    cab = ["  DOCUMENTO ", "razao social", "NOME-FANTASIA", "e mail", "inscrição estadual", "OBSERVAÇÕES"]
    up = enviar(client, cab, [[cnpj(9), "Alfa", "Alfa", "a@b.com.br", "123", "obs"]], "clientes", "c.csv")
    assert up["colunas_desconhecidas"] == []
    assert up["mapeamento"]["observacoes"] == 5 and up["mapeamento"]["inscricao_estadual"] == 4


# ============================================================================ duas etapas independentes

def test_clientes_e_depois_contratos_relacionados_pelo_documento(client, consulta):
    clientes = [linha_cliente(1, format_cnpj(cnpj(1))), linha_cliente(2, cnpj(2)),
                linha_cliente(3, "123.456.789-09".replace("123.456.789-09", format_cnpj(cpf(3))), pessoa="Física")]
    _, previa, feito = etapa(client, "clientes", CAB_CLIENTES, clientes)
    assert feito["clientes_novos"] == 3 and feito["linhas_rejeitadas"] == 0
    contratos = [linha_contrato(101, cnpj(1), 600), linha_contrato(102, format_cnpj(cnpj(1)), 500),
                 linha_contrato(201, cnpj(2), 150), linha_contrato(301, cpf(3), 90),          # CPF sem pontuação
                 linha_contrato(999, cnpj(99), 10)]                                         # cliente inexistente
    _, prev_c, feito_c = etapa(client, "contratos", CAB_CONTRATOS, contratos)
    r = prev_c["resumo"]
    assert (r["contratos_lidos"], r["contratos_relacionados"], r["contratos_sem_cliente"]) == (5, 4, 1)
    assert feito_c["contratos_novos"] == 4
    sem = [i for i in prev_c["inconsistencias"] if i["tipo"] == "sem_cliente"]
    assert len(sem) == 1 and sem[0]["identificacao"] == "999"   # não relaciona por nome: vira inconsistência
    alfa = consulta("SELECT p.valor_centavos, p.classificacao FROM pagamentos_mensais p JOIN clientes c "
                    "ON c.id = p.cliente_id WHERE c.cnpj = %s", (cnpj(1),))
    assert [tuple(r) for r in alfa] == [(110000, "A+")]                            # 600 + 500 = soma dos contratos do cliente
    assert consulta("SELECT cnpj FROM clientes WHERE dados_extras->>'pessoa' = 'Física'")[0][0] == cpf(3)


def test_erro_na_planilha_de_clientes_nao_impede_a_de_contratos(client, consulta):
    etapa(client, "clientes", CAB_CLIENTES, [linha_cliente(1, cnpj(1))])
    # Planilha de clientes quebrada: sem nenhuma coluna reconhecível -> a tela não grava nada dela
    up = enviar(client, ["Coluna A", "Coluna B"], [["x", "y"]], "clientes", "ruim.xls")
    assert up["colunas_desconhecidas"] and up["mapeamento"]["cnpj"] is None
    # ... e a de contratos segue normalmente, relacionando ao cliente que já existe
    _, prev, feito = etapa(client, "contratos", CAB_CONTRATOS, [linha_contrato(5, cnpj(1))])
    assert feito["contratos_novos"] == 1 and prev["resumo"]["contratos_relacionados"] == 1


def test_cada_etapa_tem_transacao_propria(client, consulta, monkeypatch):
    etapa(client, "clientes", CAB_CLIENTES, [linha_cliente(1, cnpj(1)), linha_cliente(2, cnpj(2))])
    from clientes import importer
    up = enviar(client, CAB_CONTRATOS, [linha_contrato(1, cnpj(1)), linha_contrato(2, cnpj(2))], "contratos", "c.xls")
    corpo = {"contratos": {"token": up["token"], "planilha": up["planilha"], "mapeamento": up["mapeamento"]},
             "mes_padrao": MES, "chave": "cnpj"}

    def falha(*a, **k):
        raise RuntimeError("falha simulada")
    monkeypatch.setattr(importer, "gravar_pagamentos", falha)
    assert client.post("/api/importacao/confirmar", json=corpo).status_code == 500
    c = contagem(consulta)
    assert c["clientes"] == 2 and c["contratos"] == 0           # clientes já gravados; contratos: nada parcial


def test_reimportacao_semanal_nao_duplica(client, consulta):
    clientes = [linha_cliente(i, cnpj(i)) for i in range(1, 21)]
    contratos = [linha_contrato(1000 + i, cnpj(i), 100 + i) for i in range(1, 21)]
    etapa(client, "clientes", CAB_CLIENTES, clientes)
    etapa(client, "contratos", CAB_CONTRATOS, contratos)
    antes = contagem(consulta)
    _, _, f1 = etapa(client, "clientes", CAB_CLIENTES, clientes)
    _, _, f2 = etapa(client, "contratos", CAB_CONTRATOS, contratos)
    assert contagem(consulta) == antes
    assert (f1["clientes_novos"], f1["clientes_atualizados"], f1["clientes_sem_alteracao"]) == (0, 0, 20)
    assert (f2["contratos_novos"], f2["contratos_atualizados"], f2["contratos_sem_alteracao"]) == (0, 0, 20)


def test_atualiza_contrato_existente_pelo_numero(client, consulta):
    etapa(client, "clientes", CAB_CLIENTES, [linha_cliente(1, cnpj(1))])
    etapa(client, "contratos", CAB_CONTRATOS, [linha_contrato(7, cnpj(1), 300, obs="primeira")])
    _, prev, feito = etapa(client, "contratos", CAB_CONTRATOS, [linha_contrato(7, cnpj(1), 450, **{"Quantidade Parcelas": 60})])
    assert feito["contratos_novos"] == 0 and feito["contratos_atualizados"] == 1
    assert consulta("SELECT COUNT(*) FROM contratos")[0][0] == 1
    obs, extras = consulta("SELECT observacoes, dados_extras FROM contratos")[0]
    assert obs == "primeira"                                   # observação não é apagada por célula vazia
    assert extras["quantidade_parcelas"] == "60" and extras["empresa"] == "EMPRESA CONTRATADA LTDA"
    assert consulta("SELECT valor_centavos FROM contrato_valores")[0][0] == 45000   # mesmo mês: substitui


# ============================================================================ dados adicionais e observações

def test_dados_adicionais_do_cliente_e_preservacao(client, consulta):
    etapa(client, "clientes", CAB_CLIENTES, [linha_cliente(7, cnpj(7), **{"Inscricao Estadual": "123.456.789.110"})])
    extras = consulta("SELECT dados_extras FROM clientes")[0][0]
    assert extras["inscricao_estadual"] == "123.456.789.110" and extras["referencia"] == "Perto da praça"
    assert extras["optante_simples"] == "NÃO" and extras["codigo_ibge"] == "3550308"
    # nova planilha com a inscrição vazia: o valor guardado continua
    _, _, feito = etapa(client, "clientes", CAB_CLIENTES, [linha_cliente(7, cnpj(7), **{"Inscricao Estadual": "", "Contato": "Novo"})])
    extras = consulta("SELECT dados_extras FROM clientes")[0][0]
    assert extras["inscricao_estadual"] == "123.456.789.110" and extras["contato"] == "Novo"
    assert feito["clientes_atualizados"] == 1
    cid = consulta("SELECT id FROM clientes")[0][0]
    ficha = client.get(f"/api/clientes/{cid}").json
    assert ficha["dados_extras"]["contato"] == "Novo" and ficha["cnpj"] == format_cnpj(cnpj(7))


def test_observacoes_de_cliente_e_contrato_separadas(client, consulta):
    cab = CAB_CLIENTES + ["Observações"]
    etapa(client, "clientes", cab, [linha_cliente(1, cnpj(1)) + ["Observação do cliente"]])
    etapa(client, "contratos", CAB_CONTRATOS, [linha_contrato(1, cnpj(1), obs="Observação do contrato")])
    cid = consulta("SELECT id FROM clientes")[0][0]
    ficha = client.get(f"/api/clientes/{cid}").json
    assert ficha["observacoes"] == "Observação do cliente"
    assert ficha["contratos"][0]["observacoes"] == "Observação do contrato"
    assert ficha["contratos"][0]["dados_extras"]["vigencia"] == "26/04/2020"


# ============================================================================ mapeamento padrão salvo e colunas novas

def test_padrao_salvo_na_primeira_importacao_e_coluna_nova_relatada(client):
    up, _, _ = etapa(client, "clientes", CAB_CLIENTES, [linha_cliente(1, cnpj(1))])
    assert up["configuracao"] is None
    cfgs = client.get("/api/importacao/configuracoes").json
    assert cfgs["clientes"][0]["padrao"] == 1                # reconhecimento salvo como padrão
    # semana seguinte: colunas em outra ordem + uma coluna nova desconhecida
    cab = list(reversed(CAB_CLIENTES)) + ["Coluna Inventada"]
    linha = list(reversed(linha_cliente(2, cnpj(2)))) + ["?"]
    up2 = enviar(client, cab, [linha], "clientes", "clientes2.xls")
    assert up2["configuracao"]["padrao"] == 1 and up2["pendencias"] == {}
    assert [c["nome"] for c in up2["colunas_desconhecidas"]] == ["Coluna Inventada"]
    assert up2["colunas"][up2["mapeamento"]["cnpj"]]["nome"] == "Documento"


def test_coluna_do_padrao_renomeada_usa_sugestao_e_avisa(client):
    etapa(client, "contratos", CAB_CONTRATOS, [linha_contrato(1, cnpj(1))], salvar_padrao=True)
    cab = ["Número do Contrato" if c == "Nº" else c for c in CAB_CONTRATOS]
    up = enviar(client, cab, [linha_contrato(2, cnpj(1))], "contratos", "c.xls")
    assert "numero_contrato" in up["pendencias"]
    assert up["colunas"][up["mapeamento"]["numero_contrato"]]["nome"] == "Número do Contrato"


# ============================================================================ Curva ABC: fonte do valor pago

def test_contrato_encerrado_conta_zero_no_mes(client, consulta):
    etapa(client, "clientes", CAB_CLIENTES, [linha_cliente(1, cnpj(1))])
    _, prev, _ = etapa(client, "contratos", CAB_CONTRATOS, [linha_contrato(1, cnpj(1), 700),
                                                           linha_contrato(2, cnpj(1), 900, encerrado="Sim")])
    assert tuple(consulta("SELECT valor_centavos, classificacao FROM pagamentos_mensais")[0]) == (70000, "A")
    assert any("contrato encerrado" in a for l in prev["contratos_linhas"] for a in l.get("avisos", []))


def test_fonte_configuravel_atrasados_e_desligada(client, consulta):
    assert client.get("/api/configuracoes/fonte-valor").json == {"ativo": True, "excluir_encerrados": True,
                                                               "excluir_atrasados": False}
    r = client.put("/api/configuracoes/fonte-valor", json={"ativo": True, "excluir_encerrados": True, "excluir_atrasados": True})
    assert r.status_code == 200 and r.json["excluir_atrasados"] is True
    etapa(client, "clientes", CAB_CLIENTES, [linha_cliente(1, cnpj(1)), linha_cliente(2, cnpj(2))])
    etapa(client, "contratos", CAB_CONTRATOS, [linha_contrato(1, cnpj(1), 500, atrasado=2), linha_contrato(2, cnpj(2), 200)])
    valores = dict(consulta("SELECT c.cnpj, p.valor_centavos FROM pagamentos_mensais p JOIN clientes c ON c.id = p.cliente_id"))
    assert valores == {cnpj(1): 0, cnpj(2): 20000}
    client.put("/api/configuracoes/fonte-valor", json={"ativo": False, "excluir_encerrados": True, "excluir_atrasados": True})
    antes = consulta("SELECT COUNT(*) FROM pagamentos_mensais")[0][0]
    etapa(client, "contratos", CAB_CONTRATOS, [linha_contrato(3, cnpj(1), 999)])
    assert consulta("SELECT COUNT(*) FROM pagamentos_mensais")[0][0] == antes   # desligada: valores do mês intactos
    assert consulta("SELECT COUNT(*) FROM contratos")[0][0] == 3                # o contrato foi gravado


def test_valor_parcela_futuro_e_original_nunca_viram_valor_pago(client, consulta):
    etapa(client, "clientes", CAB_CLIENTES, [linha_cliente(1, cnpj(1))])
    etapa(client, "contratos", CAB_CONTRATOS, [linha_contrato(1, cnpj(1), 120, **{"Valor Original": 99999,
                                                                               "Valor Parcela Futuro": 88888})])
    assert tuple(consulta("SELECT valor_centavos, classificacao FROM pagamentos_mensais")[0]) == (12000, "C")


# ============================================================================ volume real

def test_volume_real_1150_clientes_e_513_contratos(client, consulta):
    clientes = [linha_cliente(i, cnpj(i)) for i in range(1, 1031)] + \
               [linha_cliente(i, cpf(i), pessoa="Física") for i in range(1031, 1151)]
    contratos = [linha_contrato(10000 + i, cnpj(i % 1030 + 1), 100 + i % 900) for i in range(493)] + \
                [linha_contrato(20000 + i, cpf(1031 + i), 80) for i in range(20)]
    t0 = time.perf_counter()
    _, _, f1 = etapa(client, "clientes", CAB_CLIENTES, clientes)
    t1 = time.perf_counter()
    _, prev, f2 = etapa(client, "contratos", CAB_CONTRATOS, contratos)
    t2 = time.perf_counter()
    assert f1["clientes_novos"] == 1150 and f1["linhas_rejeitadas"] == 0
    assert f2["contratos_novos"] == 513 and prev["resumo"]["contratos_relacionados"] == 513
    c = contagem(consulta)
    assert c["clientes"] == 1150 and c["contratos"] == 513
    print(f"\nvolume: clientes {t1 - t0:.1f}s, contratos {t2 - t1:.1f}s (local, CPython)")
    # reimportação completa sem duplicar
    etapa(client, "clientes", CAB_CLIENTES, clientes)
    etapa(client, "contratos", CAB_CONTRATOS, contratos)
    assert contagem(consulta) == c


def test_email_ou_telefone_invalido_nao_impede_o_cadastro(client, consulta):
    _, prev, feito = etapa(client, "clientes", CAB_CLIENTES, [linha_cliente(1, cnpj(1), **{"E-mail": "sem-arroba"}),
                                                             linha_cliente(2, cnpj(2), **{"Telefone": "12"})])
    assert feito["clientes_novos"] == 2 and feito["linhas_rejeitadas"] == 0
    tipos = [i["tipo"] for i in prev["inconsistencias"]]
    assert tipos.count("campo_ignorado") == 2
    assert consulta("SELECT email FROM clientes WHERE cnpj = %s", (cnpj(1),))[0][0] is None


def test_cliente_sem_documento_vai_para_o_relatorio(client):
    _, prev, feito = etapa(client, "clientes", CAB_CLIENTES, [linha_cliente(1, ""), linha_cliente(2, cnpj(2))])
    assert feito["clientes_novos"] == 1 and feito["linhas_rejeitadas"] == 1
    erro = [i for i in prev["inconsistencias"] if i["tipo"] == "invalido"][0]
    assert erro["linha"] == 2 and "CNPJ" in erro["motivo"]


def test_mapeamento_antigo_sem_observacoes_passa_a_importar_observacoes(client, consulta):
    """Mapeamento salvo antes de existirem observações/dados adicionais (como o da produção, de 17/11)."""
    antigo = {"chave_cliente": "Documento Cliente", "data_fim": "Termino Contrato", "data_inicio": "Inicio Contrato",
              "mes": None, "numero_contrato": "Nº", "produto": None, "segmento": None, "situacao": "status",
              "valor_mensal": "Valor Atual", "valor_parcela_futuro": "Valor Parcela Futuro", "vendedor": None}
    r = client.post("/api/importacao/configuracoes", json={"tipo": "contratos", "nome": "Contratos — 17/11/2026",
                                                           "mapeamento": antigo, "chave": "cnpj", "padrao": True})
    assert r.status_code == 200, r.json
    etapa(client, "clientes", CAB_CLIENTES, [linha_cliente(1, cnpj(1))])
    up, _, _ = etapa(client, "contratos", CAB_CONTRATOS, [linha_contrato(1, cnpj(1), obs="Renovar em março")])
    assert up["configuracao"]["nome"] == "Contratos — 17/11/2026" and up["colunas_desconhecidas"] == []
    obs, extras = consulta("SELECT observacoes, dados_extras FROM contratos")[0]
    assert obs == "Renovar em março" and extras["quantidade_parcelas"] == "48"
