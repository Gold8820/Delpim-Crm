"""Rotas da aplicação (páginas e API JSON)."""
from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import Blueprint, Response, current_app, jsonify, render_template, request

from . import configs as cfg, db as dbmod, importer, repository as repo
from .classification import LABELS, describe_faixas, validate_faixas
from .db import get_db
from .validation import (CONTRACT_LABELS, FIELD_LABELS, UFS, normalize, parse_money, to_note_checked)

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

@bp.get("/api/clientes")
def list_clients():
    a = request.args
    return jsonify(repo.list_clients(
        get_db(), q=a.get("q", ""), classificacao=a.get("classificacao", ""),
        segmento=a.get("segmento", ""), mes=a.get("mes") or None,
        sort=a.get("ordem", "codigo"), direction=a.get("direcao", "asc"),
        page=a.get("pagina", 1), per_page=a.get("por_pagina", 25)))


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
    if errors:
        return error("Corrija os campos destacados.", 400, errors)
    try:
        if client_id is None:
            client_id = repo.insert_client(db, clean)
        else:
            repo.update_client(db, client_id, clean)
        if clean["valor_centavos"] is not None:
            repo.upsert_payment(db, client_id, clean["mes"], clean["valor_centavos"], "manual")
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
    return {**info, "papel": papel, "mapeamento": mapping, "pendencias": pendencias, "chave": chave,
            "linha_cabecalho": header_row,
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


@bp.post("/api/importacao/previa")
def preview():
    try:
        _, analysis = _import_context()
    except importer.ImportFailure as exc:
        return error(str(exc))
    return jsonify(_public(analysis))


@bp.post("/api/importacao/confirmar")
def commit():
    try:
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
