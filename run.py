"""Inicia o sistema:  python run.py   ->   http://127.0.0.1:5000"""
import os

from clientes import create_app

app = create_app()

if __name__ == "__main__":
    app.run(host=os.environ.get("CLIENTES_HOST", "127.0.0.1"),
            port=int(os.environ.get("CLIENTES_PORT", "5000")))
