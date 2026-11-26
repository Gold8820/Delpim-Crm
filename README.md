# Sistema de Inteligência Geográfica Comercial — Etapa 1: Clientes

Sistema web interno para cadastro e gerenciamento de clientes, com cadastro manual,
importação de planilhas (Excel `.xlsx` e CSV) e classificação automática A+/A/B/C.

> Esta etapa **não** inclui mapas, geolocalização, rotas, calendário de visitas nem integração com CRM.

## Como executar

Requer Python 3.10 ou superior e um banco **PostgreSQL** (Supabase ou outro, versão 14+).

```bash
pip install -r requirements.txt
export DATABASE_URL="postgresql://usuario:senha@host:6543/postgres"   # Windows: set DATABASE_URL=...
python run.py
```

Abra **http://127.0.0.1:5000** no navegador.

**Conexão com o Supabase:** Project Settings > Database > *Connection string* (modo *Transaction pooler*, porta 6543).
A senha nunca vai para o código nem para o repositório: use a variável de ambiente (ou um arquivo `.env` fora do controle de versão, a partir de `.env.example`).
O sistema cria as tabelas automaticamente na primeira execução (é seguro repetir).
A extensão `unaccent` é usada na ordenação e na busca por nome; no Supabase ela já vem disponível (o sistema tenta criá-la; se não tiver permissão, peça ao administrador do banco para executar `CREATE EXTENSION IF NOT EXISTS unaccent;`).

- Para acessar de outros computadores da rede: `CLIENTES_HOST=0.0.0.0 python run.py`.
- Login com **Supabase Auth** (e-mail e senha), só para usuários cadastrados e autorizados pela empresa (veja *Usuários e login*). Localmente, sem `SUPABASE_URL`, o sistema abre direto (rede interna); com `SUPABASE_URL` e `SUPABASE_PUBLISHABLE_KEY`, exige login também.
- Porta: `CLIENTES_PORT=8080`.
- Os dados ficam no PostgreSQL indicado em `DATABASE_URL`. Faça backup pelo painel do Supabase
  (ou `pg_dump`). Os arquivos enviados para importação ficam guardados no próprio banco (tabela `arquivos_importacao`) e são apagados após 24 horas. Nada é gravado no disco do servidor.
- Migração de um arquivo SQLite antigo (`data/clientes.db`): veja a seção *Migração do SQLite* abaixo.

## Implantação no Cloudflare Workers (Python Workers) + Hyperdrive + Supabase

A mesma aplicação Flask roda no Cloudflare Workers pelo adaptador WSGI oficial
(`src/worker.py`: `from workers import wsgi` / `Default = wsgi.entrypoint(app)`), com `create_app(cloudflare=True)`.
O `python run.py` continua funcionando como antes.

| Peça | Onde |
|---|---|
| Entrada do Worker | `src/worker.py` |
| Configuração do Cloudflare | `cloudflare/wrangler.jsonc` (Worker `sistema-clientes`, Hyperdrive, Static Assets, variáveis) |
| Dependências do Worker | `cloudflare/pyproject.toml`: Flask, openpyxl, defusedxml, pg8000 (todas em Python puro) |
| Montagem do pacote | `cloudflare/build.mjs` (roda sozinho no `dev`/`deploy`; copia só `src/worker.py`, `clientes/`, `templates/`) |
| Configuração assistida | `cloudflare/configurar.py` (senha do banco, Hyperdrive, pacotes, deploy de validação e de produção, verificação) |
| Login | `clientes/autenticacao.py` (Supabase Auth, JWT ES256, usuários autorizados) e `templates/login.html` |
| Leitura de planilhas no navegador | `static/planilhas.js` (envio em lotes para `/api/importacao/lote`) |
| Banco (permissões e RLS) | `scripts/supabase_cloudflare.sql` · migração de esquema: `scripts/migrar_schema.py` |
| Verificação ponta a ponta | `scripts/verificar_worker.py <url>` (dados fictícios, apaga o que cria) |

**Por que a pasta `cloudflare/`:** o `pywrangler` se recusa a rodar com um `requirements.txt` na mesma pasta, e o
`requirements.txt` da raiz continua servindo ao `python run.py` e ao Render.

**Banco:** localmente, `DATABASE_URL` (psycopg). No Worker, o binding `HYPERDRIVE` (propriedades `host`, `port`,
`user`, `password`, `database` lidas a cada requisição) com o driver **pg8000**: o psycopg depende da libpq, que não
existe no Pyodide. Uma conexão por requisição, fechada no fim dela; transação desfeita se a requisição falhar.
`CLIENTES_DB_DRIVER=pg8000` faz o ambiente local usar o mesmo driver do Worker (a suíte inteira passa nos dois).
O Hyperdrive usa a conexão **Direct** do Supabase, com o cache de consultas **desligado** (o sistema lê logo depois
de gravar; com cache, a tela mostraria dados antigos).

**Permissões:** o Worker conecta como `app_clientes` (só SELECT/INSERT/UPDATE/DELETE, sem DDL, sem BYPASSRLS).
Alterações de esquema rodam à parte (`scripts/migrar_schema.py`, usuário `postgres`). RLS ligado em todas as tabelas,
com uma única política (para `app_clientes`); `anon`/`authenticated` sem acesso.

**Login (Supabase Auth):** tela de login própria (`/login`). O navegador envia e-mail e senha ao backend, que
pede a sessão ao Supabase Auth com a chave **publicável** (fica no servidor; a `service_role` não é usada em lugar
nenhum). A sessão fica em cookies `HttpOnly` + `Secure` + `SameSite=Lax`. Em cada requisição o backend valida o JWT
(ES256, com as chaves públicas do projeto em `/auth/v1/.well-known/jwks.json`: assinatura, emissor, audiência
`authenticated`, validade) e renova o token vencido com o refresh token. Só entra quem estiver **ativo** na tabela
`usuarios_autorizados`; sem isso, mesmo uma conta existente no Supabase é recusada. Sem sessão, as páginas levam ao
login e as APIs respondem 401. "Sair" encerra a sessão no Supabase e apaga os cookies. Requisições que alteram dados
vindas de outro site são recusadas (verificação de `Origin`). Não há rota de cadastro no sistema.

**Banco e RLS:** o JWT do usuário não chega ao PostgreSQL: o Worker conecta pelo Hyperdrive como `app_clientes`
(privilégio mínimo, sem BYPASSRLS), e a autorização é feita no backend. RLS ligado em todas as tabelas, com
políticas só para `app_clientes`; `anon` e `authenticated` (inclusive usuários logados usando a API REST do Supabase
diretamente) não têm acesso a nenhuma tabela. `usuarios_autorizados` é só leitura para a aplicação.

**Importação (CPU do plano gratuito):** o navegador lê o `.xlsx`/`.xls`/`.csv` (`static/planilhas.js`, sem
bibliotecas externas) e envia as linhas em lotes para `/api/importacao/lote`. O servidor não abre o Excel. A gravação
é feita em poucos comandos SQL em lote (o PostgreSQL faz o trabalho) e a confirmação reaproveita o plano da prévia
quando nada mudou no banco. Os valores lidos no navegador são idênticos aos da leitura no servidor (teste
`tests/test_planilhas_navegador.py`). Se o navegador não conseguir ler por um motivo técnico, o arquivo é enviado
inteiro, como antes (`/api/importacao/arquivo`, mantido).

### Usuários e login (painel do Supabase)

1. **Fechar o cadastro público** (uma vez): Supabase > Authentication > Sign In / Providers > desative
   **Allow new users to sign up**. (Mesmo aberto, quem se cadastrar por fora não entra: falta a autorização abaixo.)
2. **Criar o usuário:** Authentication > Users > **Add user** > *Create new user*: e-mail e senha, marcando
   **Auto Confirm User**. Entregue a senha à pessoa por um canal seguro.
3. **Autorizar no sistema:** SQL Editor:
   `INSERT INTO public.usuarios_autorizados (email, nome) VALUES ('pessoa@empresa.com.br', 'Nome da pessoa');`
   (o e-mail em letras minúsculas, igual ao do passo 2).
4. **Bloquear alguém:** `UPDATE public.usuarios_autorizados SET ativo = false WHERE email = 'pessoa@empresa.com.br';`
   (vale em até 1 minuto). Para tirar também do Supabase Auth: Authentication > Users > ... > *Delete user*.
5. **Trocar senha:** Authentication > Users > ... > *Send password recovery* (ou a pessoa usa a recuperação).

### Publicar: primeiro a validação, depois a produção

```bash
python cloudflare/configurar.py deploy-dev     # Worker de validação: sistema-clientes-dev.<conta>.workers.dev
WORKER_URL=https://sistema-clientes-dev.<conta>.workers.dev VERIFICAR_EMAIL=... VERIFICAR_SENHA=... \
  python cloudflare/configurar.py verificar     # bloqueio sem login, login, telas, APIs, importação, sair
python cloudflare/configurar.py deploy --producao   # só depois de validar
npx wrangler secret delete ACCESS_PASSWORD --config cloudflare/wrangler.jsonc   # Secret da versão antiga
```

### Desenvolvimento local no runtime do Cloudflare

```bash
python cloudflare/configurar.py pacotes
# banco de TESTE (nunca o de produção):
set CLOUDFLARE_HYPERDRIVE_LOCAL_CONNECTION_STRING_HYPERDRIVE=postgresql://usuario:senha@127.0.0.1:5432/banco_teste
cd cloudflare && npx wrangler dev        # login pelo Supabase Auth configurado no wrangler.jsonc
```

No Linux/macOS (e no Workers Builds) também funciona `uv run pywrangler dev` / `uv run pywrangler deploy` dentro de
`cloudflare/`. No Windows o `pywrangler sync` falha ao criar o ambiente Pyodide; por isso `configurar.py pacotes`.

## Implantação no Render (aplicação) + Supabase (banco)

Arquitetura: o Render executa o Flask com Gunicorn; o Supabase guarda o PostgreSQL. Nada depende do disco do servidor.

### Antes de publicar (obrigatório)

1. **Revise o RLS no Supabase.** O script `scripts/supabase_rls_revisao.sql` remove o acesso das chaves públicas (`anon`, `authenticated`) às tabelas e habilita RLS. **Não é executado automaticamente:** leia, rode no SQL Editor do Supabase e confirme a verificação ao final.
2. **Login pelo Supabase Auth.** O Render se recusa a iniciar sem `SUPABASE_URL` e `SUPABASE_PUBLISHABLE_KEY`.
3. **Use um banco de teste** para o `pytest`. Nunca rode os testes apontando para o banco de produção.

### Configuração no painel do Render (passo a passo)

1. Suba este projeto para um repositório Git **privado**. O `.gitignore` já impede o envio de `.env`, bancos SQLite, planilhas e da pasta `data/`.
2. No Render: **New > Web Service** e escolha o repositório. Ou **New > Blueprint** para usar o `render.yaml` deste projeto.
3. Preencha os campos (se não usar o Blueprint):
   - **Runtime:** Python 3 (a versão vem do arquivo `.python-version`, 3.13)
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn run:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 120 --access-logfile -`
   - **Health Check Path:** `/healthz`
   - **Auto-Deploy:** desligado (publique manualmente, depois de conferir)
4. Em **Environment**, cadastre (marque as duas primeiras como *Secret*):
   - `DATABASE_URL` = conexão do Supabase no modo **Transaction pooler** (porta **6543**), no formato `postgresql://postgres.<ref>:<senha>@aws-0-<regiao>.pooler.supabase.com:6543/postgres`. Copie-a de Project Settings > Database.
   - `SUPABASE_URL` = `https://<projeto>.supabase.co`
   - `SUPABASE_PUBLISHABLE_KEY` = chave publicável (`sb_publishable_...`); nunca a `service_role`
5. Clique em **Create Web Service** ou **Deploy**. Não há outras variáveis: `PORT` e `RENDER=true` são definidas pelo próprio Render.
6. Ao final do deploy, abra `https://<seu-servico>.onrender.com/healthz`: deve mostrar `{"status":"ok"}`. Depois abra a raiz: deve aparecer a tela de login.

Na primeira inicialização, a aplicação cria no Supabase as tabelas que ainda não existem (inclusive `arquivos_importacao`). Isso não apaga nem altera dados já existentes. Se preferir criá-las manualmente, peça a revisão antes.

### Variáveis de ambiente

| Variável | Obrigatória | Onde | Descrição |
|---|---|---|---|
| `DATABASE_URL` | sim | Render (Secret) | Conexão PostgreSQL do Supabase (pooler, porta 6543) |
| `SUPABASE_URL` | sim no Render/Cloudflare | variável | Endereço do projeto Supabase (login) |
| `SUPABASE_PUBLISHABLE_KEY` | sim no Render/Cloudflare | variável | Chave publicável do Supabase (não é segredo) |
| `RENDER` | automática | Render | Definida pelo Render; ativa a exigência de login |
| `PORT` | automática | Render | Porta HTTP; o Gunicorn usa `$PORT` no comando de início |
| `TEST_DATABASE_URL` | não | local | Banco de **teste** para `pytest` (nunca produção) |
| `CLIENTES_HOST` / `CLIENTES_PORT` | não | local | Endereço e porta do `python run.py` |

O modelo completo está em `.env.example`. O arquivo `.env` real nunca deve ir para o repositório.

### Verificação pós-deploy (checklist)

- [ ] `/healthz` responde `{"status":"ok"}` (200)
- [ ] Sem login, a raiz leva à tela de login e as APIs respondem 401
- [ ] Com o e-mail autorizado, a tela de clientes abre direto
- [ ] Importe um arquivo de teste (não um arquivo real) e confira a prévia
- [ ] Os logs do Render não mostram tracebacks

## Testes

Os testes rodam contra um PostgreSQL **de teste** (nunca o de produção). Cada teste cria e apaga o próprio schema.

```bash
pip install -r requirements.txt pytest
export TEST_DATABASE_URL="postgresql://usuario:senha@host:5432/postgres"   # banco de teste
python -m pytest -q
```

Sem `TEST_DATABASE_URL` (ou `DATABASE_URL`), os testes falham com uma mensagem explicando o que falta.
Recomendação no Supabase: crie um *branch* de desenvolvimento e use a conexão dele.

Cobertura principal:

- `tests/test_app.py`: cadastro, edição, exclusão, importação, duplicidades, classificação (101 testes originais, com o ajuste de chamada descrito no HANDOFF).
- `tests/test_formatos_reais.py`: formatos dos arquivos reais (17).
- `tests/test_duas_planilhas.py`: importação com duas planilhas e mapeamentos padrão.
- `tests/test_curva_observacoes.py`: Curva ABC configurável e observações de clientes e contratos.
- `tests/test_migracao.py`: migração do SQLite antigo (`tests/fixtures/etapa1.db`) para o PostgreSQL.
- `tests/test_implantacao.py`: arquivos guardados no banco, expiração, XML malicioso, `/healthz`, varredura de segredos e `.gitignore`.
- `tests/test_autenticacao.py`: login com Supabase Auth (simulado), JWT ES256, usuários autorizados, sessão, saída, CSRF.
- `tests/conftest.py`: fixtures compartilhadas (schema isolado por teste, consulta direta ao banco, reabertura da aplicação).

## Migração do SQLite

Se você ainda tem o arquivo `data/clientes.db` da versão anterior (ele fica só na sua máquina, nunca no repositório):

```bash
export DATABASE_URL="postgresql://usuario:senha@host:6543/postgres"
python scripts/migrar_sqlite_para_postgres.py data/clientes.db
```

- O arquivo SQLite **não é alterado** (o script copia e trabalha na cópia).
- Os ids são preservados e as sequências são ajustadas, então novos cadastros não colidem.
- Se o banco de destino já tiver dados, o script para. Use `--substituir` apenas se tiver certeza de que pode apagar o conteúdo atual do destino.
- Só confirma se as contagens e a soma dos valores forem iguais; caso contrário, não grava nada.
- Opcional: `--schema nome` para copiar para um schema específico.

## Regras principais

**Classificação** (pelo valor pago no mês de referência; editável em `clientes/classification.py`):

| Classe | Faixa                      | Cor      |
|--------|----------------------------|----------|
| A+     | R$ 1.000,00 ou mais        | Vermelho |
| A      | R$ 400,00 a R$ 999,99      | Laranja  |
| B      | R$ 180,00 a R$ 399,99      | Azul     |
| C      | até R$ 179,99              | Verde    |

Cliente sem valor no mês fica **sem classificação** (nunca vira C automaticamente).

**Valores por mês:** cada cliente tem um valor por mês de referência, guardado em tabela
separada (`pagamentos_mensais`). Meses diferentes nunca se misturam. Ao importar um novo
total acumulado do mesmo mês, o valor anterior é **substituído**, não somado.

**Identificação de duplicidades:** por código ou CNPJ.
- Cliente existente é atualizado, nunca duplicado.
- Células vazias na planilha **preservam** os dados já cadastrados (a menos que o usuário marque
  "Substituir dados existentes por células vazias").
- O código cadastrado nunca é alterado por importação. Se o CNPJ da planilha já existe com outro
  código, o cadastro existente é atualizado e a linha recebe um aviso.
- Linhas em conflito (código de um cliente + CNPJ de outro, ou mesmo código com CNPJ diferente) são rejeitadas.

**Importação:** arquivo → associação de colunas (sugerida automaticamente; a associação usada
fica salva neste navegador para planilhas com o mesmo cabeçalho) → prévia com situação de cada
linha → confirmação. A gravação é "tudo ou nada". Linhas rejeitadas podem ser baixadas em CSV com o motivo.
Há um modelo de planilha (apenas títulos de colunas) para download na própria tela.

## Curva ABC configurável

Menu **Configurações de importação** → seção **Configuração da Curva ABC**. Cada classe (A+, A, B, C) tem mínimo e máximo editáveis, gravados na tabela `curvas_abc`. Não há valores fixos no código: os valores iniciais (A+ ≥ R$ 1.000,00; A R$ 400,00 a R$ 999,99; B R$ 180,00 a R$ 399,99; C R$ 0,00 a R$ 179,99) são usados apenas para preencher o banco na primeira criação.

- **Regras:** cada faixa é inclusiva (mínimo e máximo). A+ não tem máximo, C começa em R$ 0,00, e cada faixa termina 1 centavo antes do mínimo da faixa acima. O sistema recusa sobreposição, lacuna e classe faltando, e indica o campo a ajustar. Se algo estiver inválido, nada é salvo.
- **Reclassificação:** ao salvar, todos os pagamentos mensais de todos os clientes, em todos os meses, são reclassificados com as novas faixas. A classificação sempre usa o valor efetivamente pago no mês de referência.
- **Endpoints:** `GET /api/configuracoes/curva-abc` e `PUT /api/configuracoes/curva-abc` (corpo `{"faixas": [{"classificacao", "minimo", "maximo"}]}`, valores em texto brasileiro). A resposta do PUT informa quantos pagamentos foram reclassificados.

## Observações de clientes e contratos

- **Cliente:** campo *Observações gerais do cliente* na ficha. **Contrato:** coluna *Observações do contrato* na lista de contratos da ficha, com botão de edição. As duas são independentes: editar uma não altera a outra.
- **Formato:** aceita textos longos (até 4.000 caracteres), acentos, símbolos e várias linhas. As quebras de linha são preservadas.
- **Importação:** a coluna **Observações** (ou *Observações do cliente*, *Obs*, etc.) é sugerida para clientes; **Observacao** (singular) e *Observações do contrato* são sugeridas para contratos. A coluna pode ser salva no mapeamento padrão, como as demais.
- **Preservação nas reimportações:** célula vazia nunca apaga a observação cadastrada, mesmo com a opção "substituir por vazios". Se não houver observação cadastrada, o texto da planilha é usado. Se a planilha trouxer um texto diferente do cadastrado, o cadastrado é mantido e a prévia mostra um aviso. Para trocar o texto, edite manualmente na ficha.

## Layout (estrutura visual)

- Estrutura única: menu lateral (recolhível em tablet/celular, botão ☰), cabeçalho superior com o caminho da página e o
  usuário, conteúdo central. Camada visual em `static/layout.css` (carregada depois de `style.css`) e comportamento em
  `static/layout.js`; nenhuma regra comercial, rota ou dado mudou.
- **Clientes:** indicadores clicáveis (carteira, contratados, sem contrato, ex-clientes, pendentes), barra com pesquisa,
  mês e classificação; demais filtros no painel **Filtros** (com contador); **Colunas** escolhe o que aparece
  (preferência do navegador). Quando a tela não comporta, as colunas menos importantes saem sozinhas (UF, ramo, código,
  CNPJ, vendedor, cidade...) e o botão mostra quantas. Sem rolagem horizontal. No celular a lista vira cartões.
- **Cadastro:** seções em duas colunas (Identificação, Endereço e Observações | Perfil comercial e Informações
  comerciais), campos em grade fluida, contratos e histórico abaixo; Salvar/Cancelar sempre visíveis.
- Verificado em 1366×768, 1920×1080, tablet (768) e celular (375): sem rolagem horizontal na listagem, cadastro,
  importação e configurações.

## Perfil comercial (classificação automática e filtros)

Campos preenchidos e **gravados no banco** a cada importação (inclusive reimportação sem alterações), ao salvar um
cliente, ao salvar o mapa de regiões ou pelo botão "Classificar agora" (`clientes/perfil.py`). O nome original não muda.

- **Tipo de cliente:** Razão Social ou Nome Fantasia começando com `(INATIVO)` → Ex-cliente; REVENDA ou FORNECEDOR
  explícitos no início do nome → Revenda / Fornecedor ("(REVENDA JORGE JAPONES)" entre parênteses não conta); nome
  fantasia formado só por escritório contábil → Contabilidade; caso contrário → Cliente. Não existe a categoria Lead.
- **Situação contratual:** Contratado (há contrato relacionado pelo CPF/CNPJ) ou Cliente sem contrato. Calculada
  sempre pelos contratos.
- **Contabilidade:** o texto associado a CONTABILIDADE/CONTADOR antes ou depois de um asterisco (inclusive erros de
  digitação comuns), sem telefone. "CONTABILIDADE NÃO UTILIZA" / "SOLICITAR CONTATO" nunca viram nome de escritório.
- **Ramo de atividade:** o único filtro de classificação por atividade (coluna Ramo Atividade da planilha). No
  cadastro é uma lista suspensa com os valores já cadastrados, com "Outro (digitar)…" para um ramo novo.
- **Região 1:** zona pelo mapeamento de bairros configurável (Configurações; inicial: distritos de São Paulo).
  "(Zona Leste)" escrito no bairro vale mais. Sem regra → pendente (nunca inventada). **Região 2:** o campo Bairro.
- **Vendedor:** editável na ficha e em lote; preservado nas importações sem coluna de vendedor.
- **Edição manual** preservada; só é substituída se a informação da planilha que gerou o valor mudar.
- **Diagnóstico** (Configurações e fim da importação): identificados e pendentes por campo, situação contratual,
  tipos, bairros mais frequentes sem regra e CSV dos pendentes (`/api/perfil/pendentes.csv`). Filtro
  "Pendentes de identificação" na tela principal. Aviso na tela quando há clientes ainda não classificados.
- Planilhas reais (banco de teste, 1.274 clientes): tipo 1.274/0 pendentes; segmento 1.196/78; contabilidade
  1.047/227; Região 1 509/765 (bairros sem regra); Região 2 1.248/26 (sem bairro); 484 contratados, 790 sem contrato.

## Importação automática (clientes e depois contratos)

Botão **Importar planilha**: abre direto a janela de seleção de arquivos do Windows (seleção múltipla; `.xlsx`,
`.xls` XML do Excel 2003 ou `.csv`). Selecione a planilha de **clientes** e a de **contratos** juntas (também
funciona com uma só). Não há tela intermediária nem associação manual de colunas.

- **Identificação automática:** cada arquivo é reconhecido pelos **cabeçalhos** (não pelo nome do arquivo), em
  `POST /api/importacao/identificar` (primeiras 200 linhas lidas no navegador; nada é gravado). Contratos exigem
  Nº e Documento Cliente; clientes exigem Documento (ou código) e Razão Social (ou Nome Fantasia). Se um arquivo
  não puder ser identificado com segurança (formato não aceito, cabeçalhos insuficientes ou ambíguos, duas
  planilhas do mesmo tipo), aparece só uma mensagem objetiva e **nada é importado**.
- Os valores dos contratos vão para o mês de referência atual do sistema.

1. **Clientes:** o navegador lê o arquivo (`static/planilhas.js`), envia as linhas em lotes, as colunas são
   reconhecidas automaticamente (inclusive com linhas vazias antes do cabeçalho, acentos, maiúsculas e espaços),
   e o servidor cadastra novos, atualiza existentes e separa os sem alteração, numa transação própria.
2. **Contratos:** em seguida, o mesmo para os contratos, relacionados aos clientes pelo **documento**
   (Documento × Documento Cliente; CPF ou CNPJ, sem pontuação). Nunca por nome: sem documento correspondente,
   o contrato fica como "não relacionado" no relatório. O **Nº** é o identificador único (sem duplicar em
   importações semanais).
3. **Resultado:** cartões de cada etapa (total, novos, atualizados, sem alteração, erros; para contratos também
   relacionados, não relacionados e duplicidades), resumo geral e **CSV de inconsistências** (arquivo, linha, motivo).

As etapas são independentes: se uma planilha falhar, nada dela é gravado e a outra é processada normalmente.

- **Colunas reconhecidas (arquivos reais):** clientes — Pessoa, Documento, Razão Social, Nome Fantasia, Endereço
  Padrão, Logradouro, Numero, Bairro, Complemento, CEP, Cidade, Referência (do endereço; não é o mês), Codigo
  IBGE/Cidade, UF, Contato Padrão, Contato Cobrança, Contato, Telefone, E-mail, Inscrição Estadual e o seu Estado,
  Data de Nascimento, Optante Simples Nacional, Ramo Atividade (segmento), Dias Antecedência/Dia/Configuração de
  Faturamento, Inscrição Municipal, Observações. Contratos — Nº, Documento Cliente, Cliente, Valor Atual, Valor
  Original, Quantidade Parcelas, Valor Parcela Futuro, Observacao, status, Início/Término, Encerrado, Atrasado,
  Quitado, Vigência, Empresa e as demais colunas do arquivo. Campos sem regra própria ficam em **dados adicionais**
  (`dados_extras`), aparecem na ficha e nunca são apagados por célula vazia.
- **Mapeamento padrão:** na primeira importação o reconhecimento automático é salvo como padrão de cada planilha
  (editável em Configurações de importação). Coluna nova desconhecida ou coluna do padrão que sumiu: só ela vai para
  o relatório de inconsistências; a importação continua.
- **CPF:** o documento aceita CPF (11 dígitos) além de CNPJ; os zeros que o Excel remove são recuperados.
- **Contato inválido** (e-mail ou telefone mal formatado) não impede o cadastro: o campo é ignorado e relatado.
- **Fonte do valor pago (Curva ABC):** a planilha de contratos não traz valores recebidos. O valor do mês do contrato
  vem do campo "Valor mensal pago" do mapeamento (padrão "Valor Atual", a mensalidade recorrente); "Valor Original",
  "Valor Parcela Futuro" e quantidades de parcelas nunca são usados. Em Configurações > **Fonte do valor pago**:
  contrato encerrado conta R$ 0,00 (ligado por padrão), contrato com parcelas em atraso conta R$ 0,00 (opcional) e a
  opção de não alterar os valores do mês. O valor do cliente no mês é a soma dos contratos; classificação recalculada.
- A importação detalhada (com revisão de colunas) continua disponível em "Importação avançada".

## Importação com duas planilhas (clientes + contratos)

Não é preciso juntar as planilhas: selecione o arquivo de **clientes** e o de **contratos** (cada um `.xlsx`, `.csv` ou `.xls` no formato XML do Excel 2003; também funciona só com um deles).

**Planilha única:** marque *Esta planilha tem clientes e contratos juntos*. O mesmo arquivo é lido como clientes e como contratos, cada um com a sua associação de colunas. Cada linha é um cliente (ou um contrato do cliente, quando ele se repete). Sem coluna de **número do contrato**, nenhum contrato é criado: só os clientes são importados, com um aviso na prévia.

**Cabeçalho:** por padrão, o cabeçalho é a primeira linha com pelo menos duas células preenchidas (linhas vazias e títulos de uma célula acima da tabela são ignoradas). Se os títulos estiverem em outra linha, informe o número dela no campo *Linha do cabeçalho* de cada planilha.

- **Relacionamento:** por **CNPJ** (recomendado) ou por código do cliente. Pontos, barras e traços são ignorados (`11.222.333/0001-81` = `11222333000181`) e o zero à esquerda perdido pelo Excel é recuperado. Um cliente pode ter vários contratos; clientes nunca são duplicados. O contrato é procurado primeiro na planilha de clientes e depois no cadastro existente.
- **Contrato:** identificado pelo **número do contrato** (único no sistema), para não duplicar em importações semanais. A coluna **"Nº"** é reconhecida automaticamente como número do contrato. Um mesmo número em outro cliente é conflito e a linha é rejeitada.
- **Cliente sem código:** se o cliente for novo e a planilha não tiver código, o **CNPJ (só dígitos)** é usado como código e a prévia avisa. Clientes já cadastrados continuam sendo localizados pelo CNPJ.
- **Valor mensal:** a coluna **"Valor Atual"** é sugerida inicialmente (em todos os arquivos reais analisados, "Valor Parcela Futuro" = "Valor Atual" × "Quantidade Parcelas"). A sugestão é só um ponto de partida: o usuário pode trocar a coluna em qualquer importação. **"Valor Parcela Futuro" e "Valor Contrato" nunca são sugeridos como valor mensal pago** (são valores contratuais, não pagamentos recebidos). A prévia avisa se a coluna escolhida parecer ser a de parcela futura. O valor do mês do cliente é a **soma dos valores mensais dos seus contratos** naquele mês; reimportar o mesmo contrato/mês substitui, não soma. Contratos que não vêm na planilha da semana continuam valendo (nada é apagado).
- **Preservação:** células vazias não apagam dados (salvo a opção "substituir por vazios"). `Segmento` e `Vendedor` podem vir na planilha de contratos, mas só preenchem o que a de clientes não trouxe.
- **Inconsistências:** contratos sem cliente correspondente, registros duplicados, dados inválidos, conflitos e clientes sem contrato aparecem na aba *Inconsistências* da prévia, com o número da linha e o motivo, e podem ser baixados em CSV com o conteúdo original da linha.
- **Prévia:** total de clientes e contratos, contratos relacionados, clientes sem contrato, duplicados e inválidos, além do total de cada cliente no mês e da classificação. Nada é gravado até confirmar (tudo ou nada).

### Mapeamento padrão
Na tela de associação de colunas, marque **Definir como padrão** em cada planilha. Nas próximas vezes basta escolher os dois arquivos e avançar: as colunas são reconhecidas pelo **nome do cabeçalho** (não pela posição) e, se tudo estiver reconhecido, o sistema vai direto para a prévia. Se um cabeçalho mudar ou sumir, apenas aquele campo é destacado para correção (com sugestão), e as demais associações são mantidas.
Em **Configurações de importação** (menu lateral) é possível ver os mapeamentos salvos, qual é o padrão de clientes e qual é o de contratos, editar associações, trocar o padrão e excluir.

**Validações:** CNPJ (dígitos verificadores, inclusive o CNPJ alfanumérico adotado em 2026),
CEP, e-mail, telefone, UF, valores em formato brasileiro (`1.234,56`, `R$ 1.234,56`) e meses
(`11/2026`, `nov/2026`, `2026-11`, datas do Excel).

## Estrutura

```
run.py                  inicia o servidor
clientes/
  __init__.py           criação da aplicação
  routes.py             páginas e API JSON
  db.py                 esquema do banco (PostgreSQL) e conexão
  repository.py         consultas e gravações
  importer.py           leitura de planilhas, análise e gravação da importação
  contracts.py          planilha de contratos: relacionamento, duplicidades, totais do mês
  configs.py            mapeamentos de colunas salvos (padrão por tipo de planilha)
  validation.py         normalização e validação de campos
  classification.py     regras A+/A/B/C
templates/index.html    página única
static/                 style.css e app.js (sem dependências externas, funciona offline)
tests/                  testes automatizados (conftest.py, test_app.py, test_duas_planilhas.py, test_formatos_reais.py,
                       test_curva_observacoes.py, test_migracao.py, fixtures/etapa1.db)
scripts/                migrar_sqlite_para_postgres.py (migração do SQLite antigo)
```

## Banco de dados

PostgreSQL. Esquema criado e atualizado automaticamente por `clientes/db.py` (idempotente). Convenções:
datas e horários em texto ISO (`2026-11-17T10:00:00`), valores monetários em centavos (inteiros), ids `BIGINT` gerados pelo banco.
Nomes de cliente e código não diferenciam maiúsculas (índices únicos sobre `lower(...)`).

- `clientes` — cadastro, com a coluna `observacoes` (texto livre). Já possui as colunas `latitude`, `longitude` e `geocodificado_em`, reservadas para a etapa de geolocalização (ainda sem uso).
- `contratos` — também tem `observacoes`, separada da do cliente.
- `pagamentos_mensais` — um valor por cliente/mês, com classificação.
- `curvas_abc` — limites (mínimo e máximo, em centavos) de cada classe da Curva ABC. Gravada na criação com os valores iniciais e alterada pela tela de configuração.
- `contratos` e `contrato_valores` — contratos de cada cliente e o valor mensal pago de cada um, por mês.
- `configuracoes_importacao` — mapeamentos de colunas salvos (um padrão por tipo de planilha).
- `importacoes` — registro de cada importação realizada.
- `identificadores_externos` — reservada para vincular clientes a CRMs (como o Agendor) na etapa de integração (ainda sem uso).

## Limitações desta versão

- Login individual (Supabase Auth), mas ainda sem perfis nem registro de quem alterou o quê (o e-mail do usuário já chega à aplicação e pode ser usado para isso).
- A extensão `unaccent` está no schema `public` (aviso do Supabase, risco baixo). Pode ser movida para `extensions` numa janela de manutenção.
- No Supabase, ative o RLS (script `scripts/supabase_rls_revisao.sql`, a ser revisado antes de executar).
- A ordenação de textos segue a regra antiga do sistema (sem acentos, sem diferença de maiúsculas), via `unaccent`. Depende da extensão estar disponível no `search_path` (no Supabase, schema `extensions`).
- Importação aceita `.xlsx`, CSV e `.xls` no formato XML do Excel 2003 (SpreadsheetML, lido com a biblioteca padrão). O `.xls` binário antigo (BIFF) não é aceito: abra no Excel e salve como `.xlsx`. Limite de 25 MB e 50 mil linhas por arquivo.
- O número da linha do cabeçalho escolhida manualmente não é salvo no mapeamento padrão: se o título não estiver na primeira linha com duas células preenchidas, informe-o de novo.
- A busca de CEP não consulta serviços externos (os campos de endereço são preenchidos manualmente).
