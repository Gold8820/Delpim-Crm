# HANDOFF — Sistema de Inteligência Geográfica Comercial (continuar a programação)

Documento para outro Claude (ou desenvolvedor) continuar de onde parou. Idioma do sistema: **português brasileiro**. Valores em R$. Interface SaaS limpa e simples. **Não antecipar módulos futuros.**

## 1. O que é o projeto
Sistema web interno de clientes (Flask + Gunicorn no Render + PostgreSQL/Supabase + HTML/JS puro, sem dependências externas no front). Rodar: `pip install -r requirements.txt && python run.py` → http://127.0.0.1:5000. Dados em `data/clientes.db` (variável `CLIENTES_DATA_DIR` muda a pasta). Testes: `python -m pytest -q`.

Fora de escopo (futuro, NÃO fazer agora): mapas, geolocalização, rotas, calendário de visitas, integração com CRM (Agendor), relatórios.

## 2. Etapa 1 (CONCLUÍDA e entregue)
Tela "Clientes": tabela (código, cliente, CNPJ, cidade, estado, segmento, vendedor, valor do mês, classificação), busca por nome/código/CNPJ (sem acento), filtros (classificação, segmento, mês), ordenação, paginação; cadastro manual/edição/exclusão com confirmação; importação de uma planilha (xlsx/CSV) com mapeamento de colunas, prévia, duplicidades (por código ou CNPJ), tudo-ou-nada.
Classificação pelo valor do mês: A+ ≥ R$1.000 · A 400–999,99 · B 180–399,99 · C ≤ 179,99. Valor não informado = **sem classificação** (nunca vira C). Valores guardados em centavos, um por cliente/mês (histórico); novo total do mesmo mês **substitui**, nunca soma. Células vazias na planilha **preservam** dados (salvo opção "substituir vazios").

## 3. Etapa 2 (IMPLEMENTADA e validada com as planilhas reais) — pedido do usuário
Melhorar o módulo de importação SEM reconstruir o app, preservando cadastros/banco:
1. Importar **duas planilhas separadas** (clientes + contratos), cada uma xlsx ou CSV.
2. Relacionar automaticamente por **CNPJ** (preferencial; ou código), ignorando pontos/barras/traços. Um cliente → vários contratos; sem duplicar clientes. Sem correspondência → **relatório de inconsistências**.
3. **"Definir como padrão"** na tela de associação de colunas: salva o mapeamento permanentemente. Tela de **Configurações de importação**: ver mapeamentos, qual é o padrão (separado para clientes e contratos), editar, mudar o padrão, excluir.
4. Próximas importações: reconhecer colunas e aplicar o padrão automaticamente (usuário só escolhe os 2 arquivos e avança). Se o cabeçalho mudou, destacar **somente** os campos a corrigir, mantendo o resto. Associação **por nome do cabeçalho**, não por posição.
5. Prévia com resumo: total de clientes, de contratos, contratos relacionados, clientes sem correspondência, duplicados, inválidos. Só grava após confirmar.
6. Atualizar sem duplicar; não apagar dado por célula vazia; contrato identificado por **número do contrato** (não duplicar em importações semanais); **"Valor Parcela Futuro" NUNCA é usado automaticamente como valor mensal** — o usuário escolhe qual coluna é o valor mensal.

## 4. ESTADO ATUAL

### Rodada 5 — preparação para hospedagem no Render (aplicação) + Supabase (banco) — 195 testes passando
- **Servidor:** `gunicorn run:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 120` (testado localmente com o mesmo comando). `run.py` expõe `app`. Pins em `requirements.txt` (Flask 3.1.3, openpyxl 3.1.5, psycopg 3.3.9, gunicorn 27.0.1, defusedxml 0.7.1). Python 3.13 (`.python-version`).
- **Armazenamento:** nada mais no disco do servidor. Arquivos enviados para importação vão para a tabela `arquivos_importacao` (BYTEA, expiram em 24 h, limpos no envio seguinte). `importer.read_table` recebe bytes. Testado com dois workers do Gunicorn: upload numa requisição, prévia em outras quatro, sem nenhum arquivo criado no projeto.
- **Acesso:** HTTP Basic com `ACCESS_USER`/`ACCESS_PASSWORD` (comparação em tempo constante). Fail-closed: com `RENDER=true` e sem `ACCESS_PASSWORD`, `create_app` lança erro e o Gunicorn não sobe (testado). `/healthz` é a única rota sem senha; retorna 503 se o banco cair.
- **Segurança do XML (.xls):** `defusedxml` recusa DTD e entidades (billion laughs e XXE). Coberto por testes.
- **Configuração:** `DATABASE_URL` (obrigatória), `ACCESS_PASSWORD`, `ACCESS_USER`. Removidos `UPLOAD_DIR` e `CLIENTES_DATA_DIR` (não são mais usados). `.env.example` documenta tudo; `render.yaml` define o Blueprint sem segredos e com `autoDeploy: false`.
- **Git:** `.gitignore` bloqueia `.env*` (exceto `.env.example`), chaves, `data/`, `uploads/`, `*.db`/`*.sqlite*`, `*.xlsx`/`*.xls`/`*.csv`. Exceção única: `tests/fixtures/etapa1.db` (fixture sintético com nomes fictícios, usado pelos testes de migração). Testes varrem o projeto procurando senhas escritas em arquivos e planilhas fora dos fixtures.
- **RLS:** `scripts/supabase_rls_revisao.sql` (REVOKE para anon/authenticated + ENABLE RLS em todas as tabelas, inclusive `arquivos_importacao`). **Não executado.** Precisa de revisão e execução manual.
- **Validado:** suíte (195), Gunicorn com autenticação, `/healthz`, 12 checks de navegador contra o Gunicorn com credenciais.
- **Não feito (por decisão):** nenhuma publicação no Render; nenhuma alteração no projeto Supabase além da criação de tabelas que a aplicação faz no primeiro boot (esperado no deploy, sem apagar dados).

### Rodada 4 — migração do SQLite para PostgreSQL/Supabase (165 testes passando, contra Postgres 16 local)
- **Banco:** `clientes/db.py` agora usa `psycopg` (driver PostgreSQL 3). Conexão por `DATABASE_URL`; nada de caminho de arquivo. `create_app(database_url=None, upload_dir=None, schema=None)`; `schema` é só para os testes.
- **Esquema:** mesmas tabelas e regras. `AUTOINCREMENT` → identidade (`BIGINT GENERATED BY DEFAULT AS IDENTITY`). `COLLATE NOCASE` → índices únicos sobre `lower(...)` (codigo, numero, nome da configuração). `GLOB` → regex (`~`). `SCHEMA` é uma lista de comandos (o driver executa um por vez).
- **Ordenação:** collation `PTBR` do SQLite → `unaccent(lower(x)) COLLATE "C"` (mesma ordem de antes). Textos sem regra especial usam `COLLATE "C"` para reproduzir a ordem binária do SQLite. Exige extensão `unaccent` no `search_path` (no Supabase: schema `extensions`).
- **Acesso:** placeholders `%s` e `%(nome)s`. `lastrowid` → `INSERT ... RETURNING id` (contratos, clientes, configurações). `sqlite3.Row` → `clientes/db.py::Row` (dict que também itera pelos valores e acessa por posição; ver a docstring). Erros: `psycopg.Error` / `psycopg.IntegrityError`.
- **Pooler do Supabase:** `prepare_threshold=None` (sem prepared statements no servidor), porque o modo transação do PgBouncer não os mantém.
- **Migração:** `scripts/migrar_sqlite_para_postgres.py` (ver README). Copia uma cópia do SQLite, preserva ids, ajusta sequências, recusa destino com dados (sem `--substituir`), confirma só se contagens e somas batem. Testado com `tests/fixtures/etapa1.db`.
- **Testes:** exigem `TEST_DATABASE_URL` (ou `DATABASE_URL`) apontando para um Postgres de TESTE. Cada teste cria e apaga um schema próprio (`tests/conftest.py`: `app`, `client`, `consulta`, `reabrir`). `tests/test_migracao.py` (7) substitui o antigo teste de migração por arquivo. Sem ajuste de regra de negócio: a única diferença funcional foi a correção de `Row` (iteração pelos valores, como no sqlite3).
- **Navegador:** checks do Playwright (Curva ABC, observações, reclassificação) repetidos com o servidor sobre Postgres local: 12/12 OK.
- **Não verificado aqui:** conexão ao projeto Supabase real (não tenho a senha; a validação foi em Postgres 16 local), comportamento do pooler em produção, RLS e permissões da extensão `unaccent` no projeto. Veja a seção de próximos passos.

### Rodada 3 — Curva ABC configurável e observações (159 testes passando, `python -m pytest -q`)
- **Curva ABC:** limites no banco (`curvas_abc`), editáveis na seção "Configuração da Curva ABC" (menu Configurações de importação). Validação de lacunas, sobreposição, classe faltando e A+ com máximo. Ao salvar, todos os pagamentos são reclassificados (`repository.save_faixas`). `classification.py` não tem mais regras fixas: `VALORES_INICIAIS` só alimenta o banco na primeira criação. Endpoints: `GET/PUT /api/configuracoes/curva-abc`.
- **Observações:** clientes (`clientes.observacoes`) e contratos (`contratos.observacoes`), separadas. Texto de até 4.000 caracteres, com quebras de linha. Edição manual na ficha e, para contratos, em `PUT /api/clientes/<id>/contratos/<cid>/observacoes`.
- **Importação:** regra de mesclagem em `validation.merge_note`. Célula vazia nunca apaga; texto diferente mantém o cadastrado e gera aviso na prévia.
- **Testes novos:** `tests/test_curva_observacoes.py` (41). Browser (Playwright, 1280px): edição da Curva com erro e salvamento, reclassificação do cliente, ficha com observação do cliente e do contrato. Sem erros de JavaScript.
- **Mudança em teste antigo:** `tests/test_app.py` chamava `classify(cents)`. Agora chama `classify(cents, FAIXAS_INICIAIS)` (mesmos valores e mesmas expectativas). É a única alteração nos 101 testes originais, necessária porque a classificação passou a ler as faixas.

### Atualização da rodada anterior (118 testes passando)
- **Reconhecimento:** "Nº" → número do contrato; "Documento Cliente" → CNPJ/chave; "Valor Atual" → sugestão de valor mensal (configurável). "Valor Contrato" e "Valor Parcela Futuro" não são sugeridos.
- **.xls:** aceito no formato XML do Excel 2003 (`clientes/importer.py::_read_xls`, só biblioteca padrão). Binário BIFF recusado com mensagem.
- **Cabeçalho:** automático = primeira linha com ≥ 2 células preenchidas; `linha_cabecalho` (campo na tela, por planilha) força a linha.
- **Planilha única:** `mixed` na análise quando clientes e contratos vêm do mesmo token. Linhas repetidas do mesmo cliente (outro contrato) não são duplicadas; sem coluna de número do contrato, só os clientes são importados (aviso).
- **Cliente novo sem código:** o CNPJ (só dígitos) vira o código, com aviso.
- **Frontend:** opção de planilha única, campo de linha do cabeçalho, aceita `.xls`. Testado no navegador (Playwright) em 1440px e 390px, sem erros de JavaScript.
- **Validação real:** `contratos.csv` (513) + clientes do xlsx → 481 clientes novos, 510 contratos relacionados, 3 contratos sem cliente no cadastro, 1 CNPJ inválido. Reimportação idêntica: 0 novos clientes, contratos ou valores.
- Testes novos: `tests/test_formatos_reais.py` (17). Os dados são sintéticos; os arquivos reais não foram copiados para o projeto.

### Backend — PRONTO e testado (101 testes passando, `python -m pytest -q`)
| Arquivo | Conteúdo |
|---|---|
| `clientes/db.py` | Schema + migração idempotente (`_migrate`). Novas tabelas: `contratos` (número UNIQUE NOCASE, FK cliente ON DELETE CASCADE, `valor_parcela_futuro_centavos` só informativo), `contrato_valores` (contrato × mês, UNIQUE), `configuracoes_importacao` (JSON `{campo: nome do cabeçalho}`, `chave_relacionamento`, `cabecalhos`, `padrao`; índice único parcial = 1 padrão por tipo). Colunas novas em `importacoes`. |
| `clientes/validation.py` | Validações (CNPJ incl. alfanumérico, CEP, e-mail, UF, moeda BR, mês). Novos: `CONTRACT_FIELDS/LABELS`, `key_cnpj` (normaliza documento, completa zeros do Excel), `key_codigo`, `parse_date`, `normalize_contract`, `format_money`, `month_label`. |
| `clientes/contracts.py` | `process(ctx)`: relaciona cada contrato ao cliente (planilha de clientes da importação → depois cadastro existente), detecta duplicados/conflitos/sem cliente, soma valores por cliente/mês, lista "cliente sem contrato". |
| `clientes/importer.py` | `read_table`, `suggest_mapping(header, tipo)`, `fuzzy_suggest`, `describe_table`, `analyze(db, table, mapping, contracts=, chave=)` (prévia, não grava; aceita só clientes, só contratos ou ambos), `apply(...)` (transação única), `inconsistencies_csv`, `template_csv`. |
| `clientes/configs.py` | CRUD de mapeamentos, `apply_to_header` (aplica por nome; devolve `pendencias` só dos campos cuja coluna sumiu, com sugestão), `names_from_indexes`. |
| `clientes/routes.py` | `/api/importacao/arquivo` (form: `arquivo`, `papel`=clientes\|contratos; devolve colunas, `mapeamento`, `pendencias`, `configuracao`, `configuracoes`, `chave`), `/planilha` (reler: troca de aba/config; `manual:true` = automático), `/previa`, `/confirmar`, `/inconsistencias.csv`, `/modelo.csv?tipo=`, `/configuracoes` (GET/POST), `/configuracoes/<id>` (PUT/DELETE), `/configuracoes/<id>/padrao`, `/configuracoes/do-arquivo` (salva a associação do assistente como padrão). `GET /api/clientes/<id>` agora traz `contratos`. Payload de prévia/confirmar: `{clientes:{token,planilha,mapeamento}, contratos:{...}, chave:"cnpj"|"codigo", mes_padrao, substituir_vazios, arquivo_clientes, arquivo_contratos}` (formato antigo de 1 planilha ainda aceito). |
| `tests/test_app.py` (65) + `tests/test_duas_planilhas.py` (36) | Cobrem tudo acima, incluindo migração de banco real da etapa 1 (`tests/fixtures/etapa1.db`). |

### Frontend — CONCLUÍDO e testado no navegador (Playwright)
`static/app.js` (assistente de 2 planilhas, tela de configurações, contratos no formulário do cliente), `static/style.css` e `templates/index.html` estão integrados. Fluxos verificados ponta a ponta: duas planilhas sem padrão → mapear → "Definir como padrão" → prévia (cartões, abas Clientes/Contratos/Valores do mês/Inconsistências, CSV) → confirmar; 2ª semana com colunas em outra ordem vai direto à revisão; cabeçalho renomeado destaca só o campo afetado (com sugestão); configurações (ver, editar, trocar padrão, excluir); contratos no formulário do cliente; layout celular. Sem erros de console.

### O que ainda falta / próximos passos
1. **Planilha única real sem número de contrato** ("Planilha sem título.xlsx"): só clientes são importados. Se a planilha tiver o número do contrato em alguma coluna, associe-a em "Nº" e o sistema cria os contratos.
2. **Valor mensal da planilha única:** a associação de valor do cliente não sugere "Valor Contrato". Se quiser usá-lo como valor mensal, escolha-o manualmente e confirme que é pagamento recebido.
3. **CNPJ inválido** em uma linha da planilha de clientes real: reportado na prévia; corrigir na origem.
4. Sem login; sem teste automatizado de navegador versionado (o roteiro Playwright usado fica fácil de recriar: seletores `input[data-file=clientes|contratos]`, `#to-next`, `#to-review`, `#confirm`, `#finish`, `[data-tab=...]`, `a[data-view=configuracoes]`).

5. **Conectar ao Supabase real:** definir `DATABASE_URL` (modo Transaction pooler, porta 6543) no ambiente do servidor, rodar `python scripts/migrar_sqlite_para_postgres.py data/clientes.db` se houver dados antigos, e conferir a tela de contagem.
6. **Validar a extensão `unaccent`** no projeto Supabase (`select * from pg_extension where extname='unaccent'`). Se ela estiver fora do `search_path`, a ordenação por nome falha: o caminho correto é o schema `extensions`.
7. **Segurança no Supabase:** ativar RLS em todas as tabelas, sem políticas para `anon`. Continua valendo o aviso de que não há login.
8. **Evolução possível:** trocar as datas de texto por `timestamptz` (exige cuidado com comparações e com a migração).

## 5. Decisões de projeto (manter, ou perguntar ao usuário antes de mudar)
- **Valor do mês do cliente = soma dos valores mensais dos seus contratos naquele mês** (`contrato_valores`); grava em `pagamentos_mensais` com origem `contratos`. Reimportar o mesmo contrato/mês **substitui**. Contratos do cliente que não estão na planilha da semana continuam somando (nada é apagado).
- Com planilha de contratos presente, `valor`/`mes` da planilha de clientes são **ignorados** (servidor e UI).
- `segmento`/`vendedor` podem vir na planilha de contratos, mas só preenchem o que a planilha de clientes não trouxe; vence a primeira linha não vazia.
- Contrato repetido no mesmo mês na planilha = duplicado (rejeitado); mesmo nº em outro mês = valor adicional do mesmo contrato; mesmo nº para outro cliente = conflito.
- Contrato sem cliente na planilha **nem** no cadastro → inconsistência `sem_cliente`. Cliente da planilha sem nenhum contrato → aviso `cliente_sem_contrato` (o cadastro é importado normalmente).
- Mapeamento salvo guarda **nome do cabeçalho** (comparação sem acento/maiúscula/pontuação). Coluna sumida → só aquele campo vira pendência (com sugestão por nome parecido/apelido; **nunca** sugere "parcela futuro" para valor mensal).
- `valor_mensal` nunca é adivinhado a partir de "Valor Parcela Futuro"; há aviso na prévia se o usuário escolher uma coluna com esse nome.
- Cliente **novo** sem código na planilha usa o **CNPJ (só dígitos) como código**, com aviso na prévia. Cliente já cadastrado continua sendo localizado pelo CNPJ.
- Planilha única (mesmo arquivo como clientes e contratos): sem coluna de número do contrato, só clientes são importados. Linhas repetidas do mesmo cliente são aceitas se os dados do cliente forem iguais; caso contrário, são duplicadas.
- Sem autenticação (rede interna). `.xls` binário (BIFF) não aceito; `.xls` no formato XML do Excel 2003 aceito. Limites: 25 MB, 50 mil linhas.
- "Valor Atual" é a sugestão de valor mensal (escolha configurável); "Valor Contrato" e "Valor Parcela Futuro" não são sugeridos.
- **Curva ABC:** faixas inclusivas, A+ sem máximo, C começando em R$ 0,00 e cada faixa terminando 1 centavo antes da próxima. Alterar limites reclassifica todo o histórico, inclusive meses anteriores. A classificação usa o valor efetivamente pago no mês.
- **Observações da planilha:** nunca apagam texto cadastrado. Texto diferente mantém o cadastrado e gera aviso. Para trocar, edite manualmente. Plural ("Observações") é sugerido para cliente; singular ("Observacao") só para contrato.
- **Observações de cliente e contrato:** independentes. Editar uma não altera a outra. Limite de 4.000 caracteres.
- **Banco (rodada 4):** PostgreSQL via `DATABASE_URL` (nunca no código). Datas em texto ISO e valores em centavos inteiros, como antes. Ordenação de nomes sem acento e sem caixa (`unaccent(lower(x)) COLLATE "C"`). Sem prepared statements no servidor (pooler). SQLite só é lido pelo script de migração, e o arquivo de origem não é alterado.
- **Migração:** o destino precisa estar vazio, salvo `--substituir` (destrutivo). Só confirma se contagens e somas batem.

## 6. Estrutura
```
run.py · requirements.txt (Flask, openpyxl) · README.md · HANDOFF.md
clientes/  __init__ routes db repository importer contracts configs validation classification
templates/index.html · static/app.js · static/style.css
tests/ conftest.py test_app.py test_duas_planilhas.py test_formatos_reais.py test_curva_observacoes.py test_migracao.py fixtures/etapa1.db
scripts/ migrar_sqlite_para_postgres.py
```
