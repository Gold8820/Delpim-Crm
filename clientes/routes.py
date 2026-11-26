"""Rotas da aplicação (páginas e API JSON)."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import Blueprint, Response, current_app, jsonify, render_template, request

from . import configs as cfg, db as dbmod, importer, perfil, repository as repo
from .classification import LABELS, describe_faixas, validate_faixas
from .db import get_db
from .validation import (CLIENT_EXTRA_LABELS, CONTRACT_EXTRA_LABELS, CONTRACT_LABELS, FIELD_LABELS, UFS,
                         limpar_extras_editados, normalize, parse_money, to_note_checked)

bp = Blueprint("clientes", __name__)

TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")


def error(message: str, status: int = 400, fields: dict | None = None):
    body = {"erro": message}
    if fields:
        body["campos"] = fields
    return jsonify(body), status


# --------------------------------------------------------------------------- páginas

@bp.get("/")
def index():
    version = current_app.config.get("STATIC_VERSION")
    if not version:  # local: data dos arquivos (CSS/JS novos aparecem sem limpar o cache do navegador)
        static = Path(current_app.static_folder)
        version = max((int(f.stat().st_mtime) for f in static.glob("*") if f.is_file()), default=0)
    return render_template("index.html", version=version)


@bp.get("/api/meta")
def meta():
    db = get_db()
    return jsonify({
        "meses": repo.list_months(db),
        "mes_atual": date.today().strftime("%Y-%m"),
        "segmentos": repo.list_segments(db),
        "perfil": {"opcoes": repo.list_perfil_opcoes(db), "tipos": list(perfil.TIPOS),
                   "rotulos": perfil.ROTULOS,
                   "situacoes_contratuais": list(perfil.SITUACOES_CONTRATUAIS),
                   "pendencias": perfil.ROTULOS_PENDENCIA,
                   "nunca_classificados": db.execute(
                       "SELECT COUNT(*) FROM clientes WHERE perfil_auto = '{}'::jsonb").fetchone()[0]},
        "faixas": describe_faixas(repo.load_faixas(db)),
        "ufs": UFS,
        "campos": [{"chave": k, "rotulo": v} for k, v in FIELD_LABELS.items()],
        "campos_contratos": [{"chave": k, "rotulo": v} for k, v in CONTRACT_LABELS.items()],
    })


# --------------------------------------------------------------------------- curva ABC

def _money_input(cents: int | None) -> str:
    """Valor em formato de campo de texto: 1.000,00 (sem R$)."""
    if cents is None:
        return ""
    return f"{cents // 100:,}".replace(",", ".") + f",{cents % 100:02d}"


def _curva_payload(faixas: list[dict]) -> dict:
    return {"faixas": [{"classificacao": f["classificacao"],
                        "minimo": _money_input(f["minimo_centavos"]),
                        "maximo": _money_input(f["maximo_centavos"]),
                        "minimo_centavos": f["minimo_centavos"],
                        "maximo_centavos": f["maximo_centavos"]} for f in faixas]}


@bp.get("/api/configuracoes/fonte-valor")
def fonte_valor_get():
    """Fonte do valor pago no mês (Curva ABC)."""
    return jsonify(repo.load_fonte_valor(get_db()))


@bp.put("/api/configuracoes/fonte-valor")
def fonte_valor_put():
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not all(isinstance(body.get(k, False), bool) for k in repo.FONTE_VALOR_PADRAO):
        return error("Dados inválidos.")
    db = get_db()
    fonte = repo.save_fonte_valor(db, body)
    db.commit()
    return jsonify({**fonte, "mensagem": "Configuração salva. Vale a partir da próxima importação de contratos."})


@bp.get("/api/configuracoes/curva-abc")
def curva_get():
    return jsonify(_curva_payload(repo.load_faixas(get_db())))


@bp.put("/api/configuracoes/curva-abc")
def curva_put():
    """Salva os limites das faixas e reclassifica todos os pagamentos mensais (tudo ou nada)."""
    body = request.get_json(silent=True)
    items = body.get("faixas") if isinstance(body, dict) else None
    if not isinstance(items, list):
        return error("Dados inválidos.")
    parsed, errors = [], {}
    for item in items:
        if not isinstance(item, dict) or item.get("classificacao") not in LABELS:
            return error("Classificação inválida. Use A+, A, B ou C.")
        label = item["classificacao"]
        minimo, minimo_error = parse_money(item.get("minimo"))
        maximo, maximo_error = parse_money(item.get("maximo"))
        if minimo_error:
            errors[f"{label}.minimo"] = minimo_error
        if maximo_error:
            errors[f"{label}.maximo"] = maximo_error
        parsed.append({"classificacao": label, "minimo_centavos": minimo, "maximo_centavos": maximo})
    if errors:
        return error("Corrija os valores destacados.", 400, errors)
    problems = validate_faixas(parsed)
    if problems:
        return error("Os limites precisam formar uma sequência contínua, sem sobreposição nem lacunas.",
                     400, problems)
    db = get_db()
    try:
        changed = repo.save_faixas(db, parsed)
        db.commit()
    except dbmod.Error:
        db.rollback()
        current_app.logger.exception("Falha ao salvar a Curva ABC")
        return error("Não foi possível salvar a Curva ABC. Nada foi alterado.", 500)
    return jsonify({**_curva_payload(repo.load_faixas(db)), "reclassificados": changed,
                    "mensagem": f"{changed} pagamento(s) reclassificado(s)." if changed
                    else "Nenhum pagamento precisou ser reclassificado."})


# --------------------------------------------------------------------------- clientes

def _filtros(a) -> dict:
    """Filtros da lista de clientes (combináveis), vindos da URL ou do corpo JSON."""
    return {"q": str(a.get("q") or ""), "classificacao": str(a.get("classificacao") or ""),
            "segmento": str(a.get("segmento") or ""), "mes": a.get("mes") or None,
            "contrato": str(a.get("contrato") or ""), "pendente": str(a.get("pendente") or ""),
            **{k: str(a.get(k) or "")[:200] for k in repo.FILTROS_PERFIL}}


@bp.get("/api/clientes")
def list_clients():
    a = request.args
    return jsonify(repo.list_clients(
        get_db(), sort=a.get("ordem", "codigo"), direction=a.get("direcao", "asc"),
        page=a.get("pagina", 1), per_page=a.get("por_pagina", 25), **_filtros(a)))


@bp.post("/api/clientes/vendedor-em-lote")
def assign_seller():
    """Gestão da carteira: atribui o vendedor a todos os clientes da lista filtrada."""
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("filtros"), dict):
        return error("Dados inválidos.")
    vendedor = re.sub(r"\s+", " ", str(body.get("vendedor") or "")).strip() or None
    if vendedor and len(vendedor) > 100:
        return error("Use até 100 caracteres no nome do vendedor.")
    filtros = _filtros(body["filtros"])
    if not any(v for k, v in filtros.items() if k != "mes") and not body.get("todos"):
        return error("Aplique ao menos um filtro antes de atribuir o vendedor em lote.")
    db = get_db()
    total = repo.atribuir_vendedor(db, vendedor, **filtros)
    db.commit()
    return jsonify({"alterados": total, "mensagem": f"Vendedor {'definido' if vendedor else 'removido'} em "
                                                    f"{total} cliente(s)."})


# --------------------------------------------------------------------------- perfil comercial

@bp.post("/api/perfil/reclassificar")
def perfil_reclassify():
    """Recalcula a classificação automática de toda a carteira (preserva as edições manuais)."""
    db = get_db()
    resultado = perfil.reclassificar(db)
    db.commit()
    return jsonify(resultado)


@bp.get("/api/perfil/diagnostico")
def perfil_diagnostico():
    """Relatório: quantos clientes tiveram cada campo identificado e quantos estão pendentes."""
    return jsonify(perfil.diagnostico(get_db()))


@bp.get("/api/perfil/pendentes.csv")
def perfil_pendentes_csv():
    import csv
    import io
    buffer = io.StringIO()
    w = csv.writer(buffer, delimiter=";")
    w.writerow(["Código", "Documento", "Cliente", "Razão social", "Nome fantasia", "Bairro", "Cidade",
                "Pendente de identificação"])
    for p in perfil.pendentes(get_db()):
        w.writerow([p["codigo"], p["documento"], p["nome"], p["razao_social"], p["nome_fantasia"], p["bairro"],
                    p["cidade"], p["pendentes"]])
    return Response("\ufeff" + buffer.getvalue(), content_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="pendentes-de-identificacao.csv"'})


@bp.get("/api/configuracoes/regioes")
def regioes_get():
    mapa = perfil.carregar_mapa(get_db())
    return jsonify({"texto": perfil.mapa_para_texto(mapa), "linhas": len(mapa)})


@bp.put("/api/configuracoes/regioes")
def regioes_put():
    """Salva o mapeamento geográfico (Cidade; Bairro; Região 1; Região 2) e reclassifica a carteira."""
    body = request.get_json(silent=True)
    texto = body.get("texto") if isinstance(body, dict) else None
    if not isinstance(texto, str) or len(texto) > 400_000:
        return error("Dados inválidos.")
    mapa, erros = perfil.mapa_de_texto(texto)
    if erros:
        return error("Corrija o mapeamento: " + " ".join(erros[:5]))
    db = get_db()
    perfil.salvar_mapa(db, mapa if mapa else [dict(m) for m in perfil.MAPA_PADRAO])
    resultado = perfil.reclassificar(db)
    db.commit()
    return jsonify({**resultado, "linhas": len(mapa), "texto": perfil.mapa_para_texto(perfil.carregar_mapa(db)),
                    "mensagem": f"Mapeamento salvo. {resultado['alterados']} cliente(s) com classificação alterada."})


@bp.get("/api/clientes/<int:client_id>")
def get_client(client_id: int):
    db = get_db()
    row = repo.get_client(db, client_id)
    if row is None:
        return error("Cliente não encontrado.", 404)
    return jsonify({**repo.serialize(row), "pagamentos": repo.list_payments(db, client_id),
                    "contratos": repo.list_client_contracts(db, client_id)})


def _validate_client_payload(db, payload: dict, client_id: int | None):
    clean, errors = normalize(payload)
    if not clean["codigo"] and "codigo" not in errors:
        errors["codigo"] = "Informe o código do cliente."
    if not (clean["razao_social"] or clean["nome_fantasia"]) \
            and "razao_social" not in errors and "nome_fantasia" not in errors:
        errors["razao_social"] = "Informe a razão social ou o nome fantasia."
    if clean["valor_centavos"] is not None and not clean["mes"] and "mes" not in errors:
        errors["mes"] = "Informe o mês de referência do valor."
    if "codigo" not in errors:
        other = repo.find_by_codigo(db, clean["codigo"], client_id)
        if other:
            errors["codigo"] = f"Já existe um cliente com este código ({repo.serialize(other)['nome']})."
    if clean["cnpj"] and "cnpj" not in errors:
        other = repo.find_by_cnpj(db, clean["cnpj"], client_id)
        if other:
            errors["cnpj"] = f"Este CNPJ já está cadastrado para “{repo.serialize(other)['nome']}” " \
                             f"(código {other['codigo']})."
    return clean, errors


def _save_client(client_id: int | None, success_status: int = 200):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return error("Dados inválidos.")
    db = get_db()
    if client_id is not None and repo.get_client(db, client_id) is None:
        return error("Cliente não encontrado.", 404)
    clean, errors = _validate_client_payload(db, payload, client_id)
    edicoes = payload.get("perfil") if isinstance(payload.get("perfil"), dict) else {}
    atual = repo.get_client(db, client_id) if client_id is not None else None
    extras_mudancas, extras_erros = limpar_extras_editados(
        payload.get("dados_extras"), repo._json_obj(atual["dados_extras"]) if atual is not None else {})
    errors.update(extras_erros)
    for campo, valor in edicoes.items():
        if campo in perfil.CAMPOS:
            erro = perfil.limpar_valor(campo, valor)[1]
            if erro:
                errors[campo] = erro
    if errors:
        return error("Corrija os campos destacados.", 400, errors)
    try:
        if client_id is None:
            client_id = repo.insert_client(db, clean)
        else:
            repo.update_client(db, client_id, clean)
        if clean["valor_centavos"] is not None:
            repo.upsert_payment(db, client_id, clean["mes"], clean["valor_centavos"], "manual")
        # perfil: primeiro o automático (nome/bairro podem ter mudado), depois o que foi editado à mão
        repo.save_client_extras(db, client_id, extras_mudancas)
        perfil.reclassificar(db, [client_id])
        perfil.salvar_manual(db, client_id, edicoes)
        db.commit()
    except dbmod.IntegrityError:
        db.rollback()
        return error("Já existe um cliente com este código ou CNPJ.", 409)
    row = repo.get_client(db, client_id)
    return jsonify({**repo.serialize(row), "pagamentos": repo.list_payments(db, client_id),
                    "contratos": repo.list_client_contracts(db, client_id)}), success_status


@bp.post("/api/clientes")
def create_client():
    return _save_client(None, 201)


@bp.put("/api/clientes/<int:client_id>")
def update_client(client_id: int):
    return _save_client(client_id)


@bp.put("/api/clientes/<int:client_id>/contratos/<int:contract_id>/observacoes")
def update_contract_note(client_id: int, contract_id: int):
    """Edição manual da observação de um contrato. Não altera a observação geral do cliente."""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return error("Dados inválidos.")
    note, note_error = to_note_checked(body.get("observacoes"))
    if note_error:
        return error(note_error, 400, {"observacoes": note_error})
    db = get_db()
    if repo.get_client(db, client_id) is None:
        return error("Cliente não encontrado.", 404)
    if not repo.set_contract_note(db, client_id, contract_id, note):
        return error("Contrato não encontrado para este cliente.", 404)
    db.commit()
    return jsonify({"contratos": repo.list_client_contracts(db, client_id)})


@bp.delete("/api/clientes/<int:client_id>")
def delete_client(client_id: int):
    db = get_db()
    row = repo.get_client(db, client_id)
    if row is None:
        return error("Cliente não encontrado.", 404)
    repo.delete_client(db, client_id)
    db.commit()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- importação

# Os arquivos enviados ficam no próprio banco (tabela arquivos_importacao), não no disco do servidor.
# Assim funcionam em qualquer instância (ex.: Render com várias réplicas) e após reinícios.
UPLOAD_TTL_HOURS = 24


def _upload_cutoff() -> str:
    return (datetime.now() - timedelta(hours=UPLOAD_TTL_HOURS)).isoformat(timespec="seconds")


def _cleanup_uploads(db) -> None:
    db.execute("DELETE FROM arquivos_importacao WHERE criado_em < %s", (_upload_cutoff(),))


def _save_upload(db, token: str, ext: str, content: bytes) -> None:
    db.execute("INSERT INTO arquivos_importacao (token, extensao, conteudo, criado_em) VALUES (%s, %s, %s, %s)",
               (token, ext, content, repo.now()))


def _delete_upload(db, token: str) -> None:
    db.execute("DELETE FROM arquivos_importacao WHERE token = %s", (token,))


def _find_upload(db, token) -> tuple[bytes, str] | None:
    """Conteúdo e extensão do arquivo enviado, ou None se o token não existe ou expirou."""
    if not isinstance(token, str) or not TOKEN_RE.match(token):
        return None
    row = db.execute("SELECT extensao, conteudo FROM arquivos_importacao WHERE token = %s AND criado_em >= %s",
                     (token, _upload_cutoff())).fetchone()
    return (bytes(row["conteudo"]), row["extensao"]) if row else None


@bp.get("/api/importacao/modelo.csv")
def template():
    tipo = "contratos" if request.args.get("tipo") == "contratos" else "clientes"
    body = "\ufeff" + importer.template_csv(tipo)
    return Response(body, mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="modelo-{tipo}.csv"'})


def _papel(value) -> str:
    return "contratos" if value == "contratos" else "clientes"


def _header_row(value) -> int | None:
    """Linha do cabeçalho escolhida pelo usuário (1 = primeira linha). Vazio = automático."""
    if value in (None, "", False):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise importer.ImportFailure("Linha do cabeçalho inválida.")
    if number < 1 or number > 1000:
        raise importer.ImportFailure("A linha do cabeçalho deve ficar entre 1 e 1000.")
    return number


def _file_payload(db, papel: str, table: dict, *, config_id=None, manual: bool = False,
                  header_row: int | None = None) -> dict:
    """Colunas da planilha + associação sugerida: a configuração escolhida, ou a padrão, ou a automática."""
    info = importer.describe_table(table, papel)
    available = cfg.list_configs(db, papel)
    config = None
    if not manual:
        if config_id is not None:
            config = cfg.get_config(db, config_id)
            if config is None or config["tipo"] != papel:
                raise importer.ImportFailure("Configuração não encontrada.")
        else:
            config = cfg.default_config(db, papel)
    mapping, pendencias, chave = info["mapeamento_sugerido"], {}, info["chave_sugerida"]
    if config:
        applied = cfg.apply_to_header(config, table["header"], papel)
        mapping, pendencias = applied["mapeamento"], applied["pendencias"]
        if papel == "contratos":
            chave = config["chave"] or "cnpj"
        # Campo que não existia quando o mapeamento foi salvo (campo novo do sistema): usa o reconhecimento
        # automático. Campo que o usuário deixou de fora de propósito (salvo sem coluna) continua de fora.
        salvo = config.get("mapeamento") or {}
        # campos criados depois dos primeiros mapeamentos salvos: observações e dados adicionais
        novos = {"observacoes", *(CLIENT_EXTRA_LABELS if papel == "clientes" else CONTRACT_EXTRA_LABELS)}
        used = {v for v in mapping.values() if v is not None}
        for key, idx in info["mapeamento_sugerido"].items():
            if key in novos and key not in salvo and mapping.get(key) is None and idx is not None and idx not in used:
                mapping[key] = idx
                used.add(idx)
    used = {v for v in mapping.values() if v is not None}
    desconhecidas = [{"indice": i, "nome": h} for i, h in enumerate(table["header"]) if i not in used]
    return {**info, "papel": papel, "mapeamento": mapping, "pendencias": pendencias, "chave": chave,
            "colunas_desconhecidas": desconhecidas, "linha_cabecalho": header_row,
            "configuracao": {"id": config["id"], "nome": config["nome"], "padrao": config["padrao"]} if config else None,
            "configuracoes": [{"id": c["id"], "nome": c["nome"], "padrao": c["padrao"]} for c in available]}


@bp.post("/api/importacao/arquivo")
def upload():
    file = request.files.get("arquivo")
    if file is None or not file.filename:
        return error("Selecione um arquivo.")
    papel = _papel(request.form.get("papel"))
    ext = Path(file.filename).suffix.lower()
    if ext not in (".xlsx", ".csv", ".xls"):
        return error("Formato não aceito. Envie um arquivo Excel (.xlsx), CSV ou .xls (XML do Excel 2003).")
    db = get_db()
    _cleanup_uploads(db)
    token = uuid.uuid4().hex
    content = file.read()
    _save_upload(db, token, ext, content)
    db.commit()
    try:
        header_row = _header_row(request.form.get("linha_cabecalho"))
        table = importer.read_table(content, ext, None, header_row)
        payload = _file_payload(db, papel, table, header_row=header_row)
    except importer.ImportFailure as exc:
        _delete_upload(db, token)
        db.commit()
        return error(str(exc))
    return jsonify({"token": token, "arquivo": file.filename, **payload})


MAX_JSON_UPLOAD = 40 * 1024 * 1024     # planilha lida no navegador, somando todos os lotes
MAX_LOTE_LINHAS = 5000


def _celula_valida(c) -> bool:
    return c is None or isinstance(c, (str, int, float, bool)) or (
        isinstance(c, dict) and len(c) == 1 and next(iter(c)) in ("$d", "$i", "$f", "$x")
        and isinstance(next(iter(c.values())), str))


@bp.post("/api/importacao/lote")
def upload_batch():
    """Planilha lida no navegador (static/planilhas.js), enviada em lotes de linhas.

    1º lote (sem token): {arquivo, planilhas: [abas], aba, linhas: [[nº da linha, [células]]], final}
    lotes seguintes:     {token, aba, linhas, final}
    No último lote (final: true, com papel e linha_cabecalho), a resposta é igual à de /api/importacao/arquivo.
    O servidor não abre arquivos Excel: só guarda o JSON (barato em CPU no Cloudflare Workers).
    """
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return error("Dados inválidos.")
    linhas = body.get("linhas")
    if not isinstance(linhas, list) or len(linhas) > MAX_LOTE_LINHAS:
        return error("Lote de linhas inválido.")
    for item in linhas:
        if not (isinstance(item, list) and len(item) == 2 and isinstance(item[0], int) and isinstance(item[1], list)
                and all(_celula_valida(c) for c in item[1])):
            return error("Lote de linhas inválido.")
    try:
        aba = int(body.get("aba") or 0)
    except (TypeError, ValueError):
        return error("Aba inválida.")
    pedaco = (json.dumps({"s": aba, "rows": linhas}, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    db = get_db()
    token = body.get("token")
    if not token:
        nome = str(body.get("arquivo") or "")
        if Path(nome).suffix.lower() not in (".xlsx", ".csv", ".xls"):
            return error("Formato não aceito. Envie um arquivo Excel (.xlsx), CSV ou .xls (XML do Excel 2003).")
        planilhas = body.get("planilhas") or []
        if not isinstance(planilhas, list) or len(planilhas) > 200 or not all(isinstance(p, str) for p in planilhas):
            return error("Lista de abas inválida.")
        _cleanup_uploads(db)
        token = uuid.uuid4().hex
        meta = json.dumps({"arquivo": nome, "planilhas": planilhas}, ensure_ascii=False) + "\n"
        _save_upload(db, token, ".json", meta.encode() + pedaco)
    else:
        if not isinstance(token, str) or not TOKEN_RE.match(token):
            return error("O arquivo enviado expirou. Selecione o arquivo novamente.")
        row = db.execute("UPDATE arquivos_importacao SET conteudo = conteudo || %s "
                         "WHERE token = %s AND extensao = '.json' AND criado_em >= %s "
                         "AND octet_length(conteudo) + %s <= %s RETURNING token",
                         (pedaco, token, _upload_cutoff(), len(pedaco), MAX_JSON_UPLOAD)).fetchone()
        if row is None:
            db.rollback()
            return error("O arquivo enviado expirou ou passou do limite de tamanho. Selecione o arquivo novamente.")
    db.commit()
    if not body.get("final"):
        return jsonify({"token": token})
    found = _find_upload(db, token)
    try:
        header_row = _header_row(body.get("linha_cabecalho"))
        table = importer.read_table(found[0], found[1], None, header_row)
        payload = _file_payload(db, _papel(body.get("papel")), table, header_row=header_row)
    except importer.ImportFailure as exc:
        _delete_upload(db, token)
        db.commit()
        return error(str(exc))
    meta = json.loads(bytes(found[0]).split(b"\n", 1)[0])
    return jsonify({"token": token, "arquivo": meta.get("arquivo"), **payload})


@bp.post("/api/importacao/identificar")
def identificar():
    """Identifica cada arquivo selecionado (clientes ou contratos) pelas primeiras linhas, lidas no navegador.

    Corpo: {"arquivos": [{"nome", "linhas": [[nº da linha, [células]], ...]}]} (até 10 arquivos, 200 linhas cada).
    Resposta: {"arquivos": [{"nome", "papel": "clientes"|"contratos"|null, "motivo"}]}. Não grava nada.
    """
    body = request.get_json(silent=True)
    arquivos = body.get("arquivos") if isinstance(body, dict) else None
    if not isinstance(arquivos, list) or not 0 < len(arquivos) <= 10:
        return error("Selecione de 1 a 10 arquivos.")
    resposta = []
    for arq in arquivos:
        nome = str((arq or {}).get("nome") or "")[:200]
        linhas = (arq or {}).get("linhas")
        if not isinstance(linhas, list) or len(linhas) > 200 or not all(
                isinstance(i, list) and len(i) == 2 and isinstance(i[0], int) and isinstance(i[1], list)
                and all(_celula_valida(c) for c in i[1]) for i in linhas):
            return error("Conteúdo de arquivo inválido.")
        try:
            tabela = importer._build(iter([(n, [importer._json_cell(c) for c in cells]) for n, cells in linhas]),
                                     sheets=[], sheet=None)
        except importer.ImportFailure as exc:
            resposta.append({"nome": nome, "papel": None, "motivo": str(exc)})
            continue
        r = importer.identificar_planilha(tabela["header"])
        resposta.append({"nome": nome, "papel": r["papel"], "motivo": r["motivo"]})
    return jsonify({"arquivos": resposta})


@bp.post("/api/importacao/planilha")
def reread():
    """Troca de aba ou de configuração: reaplica a associação sobre o mesmo arquivo."""
    body = request.get_json(silent=True) or {}
    db = get_db()
    found = _find_upload(db, body.get("token"))
    if found is None:
        return error("O arquivo enviado expirou. Selecione o arquivo novamente.")
    content, ext = found
    try:
        header_row = _header_row(body.get("linha_cabecalho"))
        table = importer.read_table(content, ext, body.get("planilha"), header_row)
        return jsonify(_file_payload(db, _papel(body.get("papel")), table,
                                     config_id=body.get("config_id"), manual=bool(body.get("manual")),
                                     header_row=header_row))
    except importer.ImportFailure as exc:
        return error(str(exc))


def _side(db, part, papel: str):
    """Lê uma das planilhas (já enviada) e o mapeamento escolhido. Retorna (tabela, mapeamento) ou None."""
    if not part:
        return None
    found = _find_upload(db, part.get("token"))
    if found is None:
        raise importer.ImportFailure("O arquivo enviado expirou. Selecione o arquivo novamente.")
    content, ext = found
    table = importer.read_table(content, ext, part.get("planilha"), _header_row(part.get("linha_cabecalho")))
    mapping = part.get("mapeamento")
    if not isinstance(mapping, dict):
        raise importer.ImportFailure("Mapeamento de colunas ausente.")
    labels = CONTRACT_LABELS if papel == "contratos" else FIELD_LABELS
    return table, {k: mapping.get(k) for k in labels}


def _import_context():
    """Lê e valida os parâmetros comuns de prévia, confirmação e relatório."""
    body = request.get_json(silent=True) or {}
    if "clientes" not in body and "contratos" not in body and "token" in body:   # formato de uma só planilha
        body = {**body, "clientes": body, "arquivo_clientes": body.get("arquivo")}
    db = get_db()
    clients = _side(db, body.get("clientes"), "clientes")
    contracts = _side(db, body.get("contratos"), "contratos")
    # planilha única: a mesma planilha (mesmo arquivo enviado) serve como clientes e como contratos
    mixed = bool(body.get("clientes") and body.get("contratos")
                 and body["clientes"].get("token") == body["contratos"].get("token"))
    analysis = importer.analyze(
        get_db(), clients[0] if clients else None, clients[1] if clients else None,
        default_month=body.get("mes_padrao") or None, replace_empty=bool(body.get("substituir_vazios")),
        contracts={"table": contracts[0], "mapping": contracts[1]} if contracts else None,
        chave=body.get("chave") or "cnpj", mixed=mixed)
    return body, analysis


def _public(analysis: dict) -> dict:
    return {k: v for k, v in analysis.items() if not k.startswith("_")}


# Plano da prévia reaproveitado na confirmação (evita refazer toda a análise e economiza CPU no Worker).
# Só é usado se a confirmação tiver exatamente os mesmos parâmetros E o banco não tiver mudado desde a
# prévia (impressão digital abaixo). Em qualquer outro caso, a análise é refeita, como sempre foi.

def _params_key(body: dict) -> str:
    chaves = ("clientes", "contratos", "chave", "mes_padrao", "substituir_vazios", "token", "mapeamento", "planilha",
              "linha_cabecalho")
    canon = json.dumps({k: body.get(k) for k in chaves}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canon.encode()).hexdigest()[:32]


def _fingerprint(db) -> str:
    row = db.execute(
        "SELECT (SELECT count(*) || ':' || coalesce(max(id), 0) || ':' || coalesce(max(atualizado_em), '') FROM clientes) AS cli, "
        "(SELECT count(*) || ':' || coalesce(max(atualizado_em), '') FROM contratos) AS ct, "
        "(SELECT count(*) || ':' || coalesce(max(atualizado_em), '') FROM contrato_valores) AS cv, "
        "(SELECT count(*) || ':' || coalesce(max(atualizado_em), '') FROM pagamentos_mensais) AS pg, "
        "(SELECT string_agg(classificacao || minimo_centavos || '-' || coalesce(maximo_centavos, -1), ',' "
        "ORDER BY classificacao) FROM curvas_abc) AS abc").fetchone()
    return "|".join(str(v) for v in row.values())


def _save_plan(db, key: str, analysis: dict) -> None:
    data = json.dumps({"fp": _fingerprint(db), "plano": importer.plan_to_json(analysis)},
                      ensure_ascii=False, default=str).encode()
    db.execute("INSERT INTO arquivos_importacao (token, extensao, conteudo, criado_em) VALUES (%s, '.plano', %s, %s) "
               "ON CONFLICT (token) DO UPDATE SET conteudo = excluded.conteudo, criado_em = excluded.criado_em",
               ("p" + key[1:], data, repo.now()))
    db.commit()


def _load_plan(db, key: str) -> dict | None:
    row = db.execute("SELECT conteudo FROM arquivos_importacao WHERE token = %s AND extensao = '.plano' "
                     "AND criado_em >= %s", ("p" + key[1:], _upload_cutoff())).fetchone()
    if row is None:
        return None
    saved = json.loads(bytes(row["conteudo"]))
    if saved.get("fp") != _fingerprint(db):
        return None
    return importer.plan_from_json(saved["plano"])


@bp.post("/api/importacao/previa")
def preview():
    try:
        body, analysis = _import_context()
    except importer.ImportFailure as exc:
        return error(str(exc))
    try:
        _save_plan(get_db(), _params_key(body), analysis)
    except dbmod.Error:   # o plano é só uma otimização: sem ele, a confirmação refaz a análise
        get_db().rollback()
        current_app.logger.warning("Plano da prévia não foi guardado", exc_info=True)
    return jsonify(_public(analysis))


@bp.post("/api/importacao/confirmar")
def commit():
    try:
        body = request.get_json(silent=True) or {}
        analysis = _load_plan(get_db(), _params_key(body))
        if analysis is None:
            body, analysis = _import_context()
        result = importer.apply(get_db(), analysis, body.get("arquivo_clientes") if body.get("clientes") else None,
                                body.get("arquivo_contratos"))
    except importer.ImportFailure as exc:
        return error(str(exc))
    except dbmod.Error:
        current_app.logger.exception("Falha ao gravar importação")
        return error("Não foi possível gravar a importação. Nenhum dado foi alterado.", 500)
    return jsonify(result)


@bp.post("/api/importacao/inconsistencias.csv")
@bp.post("/api/importacao/rejeitadas.csv")
def inconsistencies():
    try:
        _, analysis = _import_context()
    except importer.ImportFailure as exc:
        return error(str(exc))
    body = "\ufeff" + importer.inconsistencies_csv(analysis)
    return Response(body, mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="inconsistencias-importacao.csv"'})


# --------------------------------------------------------------------------- configurações de importação

def _config_call(fn, *args, **kwargs):
    try:
        return jsonify(fn(*args, **kwargs))
    except cfg.ConfigError as exc:
        return error(str(exc), exc.status)


@bp.get("/api/importacao/configuracoes")
def configs_list():
    items = cfg.list_configs(get_db())
    return jsonify({tipo: [c for c in items if c["tipo"] == tipo] for tipo in cfg.TIPOS})


@bp.post("/api/importacao/configuracoes")
def configs_create():
    b = request.get_json(silent=True) or {}
    return _config_call(lambda: cfg.save_config(
        get_db(), b.get("tipo"), b.get("nome"), b.get("mapeamento"), b.get("chave"),
        b.get("cabecalhos"), bool(b.get("padrao"))))


@bp.put("/api/importacao/configuracoes/<int:config_id>")
def configs_update(config_id: int):
    b = request.get_json(silent=True) or {}
    db = get_db()
    current = cfg.get_config(db, config_id)
    if current is None:
        return error("Configuração não encontrada.", 404)
    return _config_call(lambda: cfg.save_config(
        db, current["tipo"], b.get("nome", current["nome"]), b.get("mapeamento", current["mapeamento"]),
        b.get("chave", current["chave"]), b.get("cabecalhos"), bool(b.get("padrao")), config_id))


@bp.post("/api/importacao/configuracoes/<int:config_id>/padrao")
def configs_default(config_id: int):
    return _config_call(cfg.set_default, get_db(), config_id)


@bp.delete("/api/importacao/configuracoes/<int:config_id>")
def configs_delete(config_id: int):
    try:
        cfg.delete_config(get_db(), config_id)
    except cfg.ConfigError as exc:
        return error(str(exc), exc.status)
    return jsonify({"ok": True})


@bp.post("/api/importacao/configuracoes/do-arquivo")
def configs_from_file():
    """'Definir como padrão' no assistente: salva a associação feita sobre o arquivo enviado."""
    b = request.get_json(silent=True) or {}
    papel = _papel(b.get("papel"))
    db = get_db()
    found = _find_upload(db, b.get("token"))
    if found is None:
        return error("O arquivo enviado expirou. Selecione o arquivo novamente.")
    content, ext = found
    try:
        table = importer.read_table(content, ext, b.get("planilha"), _header_row(b.get("linha_cabecalho")))
        mapping = b.get("mapeamento")
        if not isinstance(mapping, dict):
            return error("Mapeamento de colunas ausente.")
        names = cfg.names_from_indexes(table["header"], mapping)
        config_id = b.get("config_id")
        current = cfg.get_config(db, config_id) if config_id else None
        if current:
            nome = current["nome"]
        else:
            base = f"{'Contratos' if papel == 'contratos' else 'Clientes'} — {date.today():%d/%m/%Y}"
            nome = cfg.unique_name(db, papel, base)
        return jsonify(cfg.save_config(db, papel, nome, names, b.get("chave"), table["header"], True,
                                       current["id"] if current else None))
    except importer.ImportFailure as exc:
        return error(str(exc))
    except cfg.ConfigError as exc:
        return error(str(exc), exc.status)
