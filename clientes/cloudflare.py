"""Partes usadas só no Cloudflare Workers (Pyodide).

Arquivos estáticos: a pasta static/ é publicada pelo Workers Static Assets (binding ASSETS, ver
wrangler.jsonc). Como todas as requisições passam primeiro pelo Worker (run_worker_first), a rota
/static/<arquivo> exige login como as demais e só então busca o arquivo no ASSETS.
"""
from __future__ import annotations

from flask import Flask, Response, abort

from .db import worker_env


def _fetch_asset(filename: str):
    from pyodide.ffi import run_sync   # disponível só no Pyodide

    env = worker_env()
    assets = getattr(env, "ASSETS", None) if env is not None else None
    if assets is None:
        abort(404)
    asset = run_sync(assets.fetch(f"https://assets.local/{filename}"))
    if asset.status == 404:
        abort(404)
    body = run_sync(asset.bytes())
    return asset.status, bytes(body), asset.headers.get("content-type")


def register_static(app: Flask) -> None:
    def static(filename: str):
        status, body, content_type = _fetch_asset(filename)
        # Revalida a cada carga: uma nova versão de CSS/JS aparece sem limpar o cache do navegador.
        return Response(body, status=status, content_type=content_type or "application/octet-stream",
                        headers={"Cache-Control": "no-cache"})

    # Mesmo nome de rota do Flask ("static"), para url_for('static', ...) no template continuar igual.
    app.add_url_rule("/static/<path:filename>", endpoint="static", view_func=static)
