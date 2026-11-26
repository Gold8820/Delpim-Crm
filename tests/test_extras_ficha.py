"""Dados adicionais da planilha de clientes na ficha: consulta e edição, sem perder nada."""
from test_importacao_automatica import CAB_CLIENTES, cnpj, etapa, linha_cliente


def importar(client, **mudar):
    linhas = [linha_cliente(1, cnpj(1), **{"Inscricao Estadual": "123.456.789.110", "Inscrição Municipal": "998877",
                                           "Contato Cobrança": "Maria", **mudar})]
    etapa(client, "clientes", CAB_CLIENTES, linhas)
    return client.get("/api/clientes", query_string={"q": cnpj(1)}).json["clientes"][0]["id"]


def corpo_base(atual, **extra):
    return {**{k: atual[k] for k in ("codigo", "razao_social", "nome_fantasia", "cnpj", "cidade")}, **extra}


def test_importacao_guarda_pessoa_ie_e_demais(client):
    cid = importar(client)
    d = client.get(f"/api/clientes/{cid}").json["dados_extras"]
    assert d["pessoa"] == "Jurídica" and d["inscricao_estadual"] == "123.456.789.110"
    assert d["inscricao_municipal"] == "998877" and d["contato_cobranca"] == "Maria"


def test_edicao_altera_so_o_que_mudou(client):
    cid = importar(client)
    atual = client.get(f"/api/clientes/{cid}").json
    antes = dict(atual["dados_extras"])
    r = client.put(f"/api/clientes/{cid}", json=corpo_base(atual, dados_extras={
        "inscricao_estadual": "ISENTO", "pessoa": "Física", "contato_cobranca": "",   # vazio = remover o valor
        "campo_inventado": "x"}))                                                      # desconhecido: ignorado
    assert r.status_code == 200, r.json
    d = r.json["dados_extras"]
    assert d["inscricao_estadual"] == "ISENTO" and d["pessoa"] == "Física"
    assert "contato_cobranca" not in d and "campo_inventado" not in d
    for k, v in antes.items():                      # nenhum outro dado adicional foi perdido
        if k not in ("inscricao_estadual", "pessoa", "contato_cobranca"):
            assert d[k] == v
    # salvar sem enviar dados adicionais não mexe neles
    r2 = client.put(f"/api/clientes/{cid}", json=corpo_base(atual))
    assert r2.json["dados_extras"] == d


def test_valor_muito_longo_e_recusado_sem_gravar(client):
    cid = importar(client)
    atual = client.get(f"/api/clientes/{cid}").json
    r = client.put(f"/api/clientes/{cid}", json=corpo_base(atual, dados_extras={"inscricao_municipal": "x" * 501}))
    assert r.status_code == 400 and "extra_inscricao_municipal" in r.json["campos"]
    assert client.get(f"/api/clientes/{cid}").json["dados_extras"]["inscricao_municipal"] == "998877"


def test_novo_cliente_manual_com_pessoa_e_ie(client):
    r = client.post("/api/clientes", json={"codigo": "M1", "razao_social": "Cliente Manual",
                                           "dados_extras": {"pessoa": "Jurídica", "inscricao_estadual": "111"}})
    assert r.status_code == 201, r.json
    assert r.json["dados_extras"] == {"pessoa": "Jurídica", "inscricao_estadual": "111"}


def test_reimportacao_com_celula_vazia_nao_apaga_edicao(client):
    cid = importar(client)
    atual = client.get(f"/api/clientes/{cid}").json
    client.put(f"/api/clientes/{cid}", json=corpo_base(atual, dados_extras={"data_nascimento": "01/01/1990"}))
    importar(client)            # a planilha não tem data de nascimento preenchida
    assert client.get(f"/api/clientes/{cid}").json["dados_extras"]["data_nascimento"] == "01/01/1990"
