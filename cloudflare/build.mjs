// Monta a pasta build/ com só o que vai para o Worker:
//   src/worker.py (entrada), clientes/ (pacote do sistema) e templates/ (página);
//   build/public/static/ (CSS e JS), publicado pelo Workers Static Assets em /static/*.
// Executado automaticamente pelo wrangler antes de `dev` e `deploy` (ver "build" no wrangler.jsonc).
// Testes, scripts, dados, .env e planilhas nunca entram no Worker.
import { cpSync, mkdirSync, rmSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "..");
const out = join(here, "build");

rmSync(out, { recursive: true, force: true });
mkdirSync(out, { recursive: true });
const semCache = (src) => !src.includes("__pycache__") && !src.endsWith(".pyc");
cpSync(join(root, "src", "worker.py"), join(out, "worker.py"));
cpSync(join(root, "clientes"), join(out, "clientes"), { recursive: true, filter: semCache });
cpSync(join(root, "templates"), join(out, "templates"), { recursive: true });
cpSync(join(root, "static"), join(out, "public", "static"), { recursive: true });
console.log("build/ pronto: worker.py, clientes/, templates/, public/static/");
