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
- Acesso protegido por senha: defina `ACCESS_PASSWORD` (e, opcionalmente, `ACCESS_USER`, padrão `admin`). Sem ela, o sistema abre sem senha, o que só é aceitável em rede local de teste.
- Porta: `CLIENTES_PORT=8080`.
- Os dados ficam no PostgreSQL indicado em `DATABASE_URL`. Faça backup pelo painel do Supabase
  (ou `pg_dump`). Os arquivos enviados para importação ficam guardados no próprio banco (tabela `arquivos_importacao`) e são apagados após 24 horas. Nada é gravado no disco do servidor.
- Migração de um arquivo SQLite antigo (`data/clientes.db`): veja a seção *Migração do SQLite* abaixo.

## Implantação no Render (aplicação) + Supabase (banco)

Arquitetura: o Render executa o Flask com Gunicorn; o Supabase guarda o PostgreSQL. Nada depende do disco do servidor.

### Antes de publicar (obrigatório)

1. **Revise o RLS no Supabase.** O script `scripts/supabase_rls_revisao.sql` remove o acesso das chaves públicas (`anon`, `authenticated`) às tabelas e habilita RLS. **Não é executado automaticamente:** leia, rode no SQL Editor do Supabase e confirme a verificação ao final.
2. **Não há login de usuários.** O acesso é por uma senha única (`ACCESS_PASSWORD`). Não compartilhe o link com quem não deve ver os dados e não libere o serviço publicamente sem autenticação real. O Render se recusa a iniciar sem `ACCESS_PASSWORD`.
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
   - `ACCESS_PASSWORD` = senha forte (16 caracteres ou mais, gerada em um gerenciador de senhas)
   - `ACCESS_USER` = `admin` (ou outro nome de usuário)
5. Clique em **Create Web Service** ou **Deploy**. Não há outras variáveis: `PORT` e `RENDER=true` são definidas pelo próprio Render.
6. Ao final do deploy, abra `https://<seu-servico>.onrender.com/healthz`: deve mostrar `{"status":"ok"}`. Depois abra a raiz: o navegador pedirá usuário e senha.

Na primeira inicialização, a aplicação cria no Supabase as tabelas que ainda não existem (inclusive `arquivos_importacao`). Isso não apaga nem altera dados já existentes. Se preferir criá-las manualmente, peça a revisão antes.

### Variáveis de ambiente

| Variável | Obrigatória | Onde | Descrição |
|---|---|---|---|
| `DATABASE_URL` | sim | Render (Secret) | Conexão PostgreSQL do Supabase (pooler, porta 6543) |
| `ACCESS_PASSWORD` | sim no Render | Render (Secret) | Senha do acesso ao sistema. Sem ela, o Render não inicia o serviço |
| `ACCESS_USER` | não | Render | Usuário do acesso (padrão `admin`) |
| `RENDER` | automática | Render | Definida pelo Render; ativa a exigência de `ACCESS_PASSWORD` |
| `PORT` | automática | Render | Porta HTTP; o Gunicorn usa `$PORT` no comando de início |
| `TEST_DATABASE_URL` | não | local | Banco de **teste** para `pytest` (nunca produção) |
| `CLIENTES_HOST` / `CLIENTES_PORT` | não | local | Endereço e porta do `python run.py` |

O modelo completo está em `.env.example`. O arquivo `.env` real nunca deve ir para o repositório.

### Verificação pós-deploy (checklist)

- [ ] `/healthz` responde `{"status":"ok"}` (200)
- [ ] Sem usuário e senha, a raiz responde 401
- [ ] Com usuário e senha, a tela de clientes carrega
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
- `tests/test_implantacao.py`: arquivos guardados no banco, expiração, XML malicioso, senha de acesso, `/healthz`, varredura de segredos e `.gitignore`.
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

- Acesso por senha única (`ACCESS_PASSWORD`): não há usuários individuais, perfis nem registro de quem alterou o quê. Antes de abrir a mais de uma pessoa, implemente login real e permissões por usuário.
- No Supabase, ative o RLS (script `scripts/supabase_rls_revisao.sql`, a ser revisado antes de executar).
- A ordenação de textos segue a regra antiga do sistema (sem acentos, sem diferença de maiúsculas), via `unaccent`. Depende da extensão estar disponível no `search_path` (no Supabase, schema `extensions`).
- Importação aceita `.xlsx`, CSV e `.xls` no formato XML do Excel 2003 (SpreadsheetML, lido com a biblioteca padrão). O `.xls` binário antigo (BIFF) não é aceito: abra no Excel e salve como `.xlsx`. Limite de 25 MB e 50 mil linhas por arquivo.
- O número da linha do cabeçalho escolhida manualmente não é salvo no mapeamento padrão: se o título não estiver na primeira linha com duas células preenchidas, informe-o de novo.
- A busca de CEP não consulta serviços externos (os campos de endereço são preenchidos manualmente).
