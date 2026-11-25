-- Banco do Supabase para o Cloudflare Workers (Hyperdrive). Idempotente: pode rodar mais de uma vez.
-- Nada é apagado: só cria o que falta, concede permissões e liga o RLS.
--
-- Separação de permissões:
--   * migração (este script e scripts/migrar_schema.py): usuário "postgres" do Supabase (tem DDL);
--   * aplicação (Worker, via Hyperdrive): usuário "app_clientes", só SELECT/INSERT/UPDATE/DELETE,
--     sem DDL, sem BYPASSRLS, sem acesso a outros schemas do Supabase (auth, storage, vault...).
--
-- A senha do app_clientes NÃO fica aqui. Defina-a à parte (SQL Editor do Supabase):
--     ALTER ROLE app_clientes PASSWORD '<senha forte>';
-- e use a mesma senha só na configuração do Hyperdrive.

-- 1) Tabela que a versão atual da aplicação usa para guardar planilhas entre as etapas da importação
--    (no Cloudflare não há disco). Igual à de clientes/db.py.
CREATE TABLE IF NOT EXISTS public.arquivos_importacao (
    token TEXT PRIMARY KEY,
    extensao TEXT NOT NULL,
    conteudo BYTEA NOT NULL,
    criado_em TEXT NOT NULL
);

-- 2) Usuário da aplicação (criado sem senha utilizável; a senha é definida à parte, ver acima).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_clientes') THEN
        CREATE ROLE app_clientes LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
            CONNECTION LIMIT 40;
    END IF;
END $$;
-- unaccent() (ordenação/busca sem acento) fica no schema "extensions" do Supabase.
ALTER ROLE app_clientes SET search_path = public, extensions;
ALTER ROLE app_clientes SET statement_timeout = '30s';
ALTER ROLE app_clientes SET idle_in_transaction_session_timeout = '60s';

GRANT USAGE ON SCHEMA public TO app_clientes;
GRANT USAGE ON SCHEMA extensions TO app_clientes;
GRANT SELECT, INSERT, UPDATE, DELETE ON
    public.clientes, public.pagamentos_mensais, public.importacoes, public.contratos,
    public.contrato_valores, public.configuracoes_importacao, public.curvas_abc,
    public.arquivos_importacao, public.identificadores_externos
    TO app_clientes;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_clientes;

-- 3) API pública do Supabase (anon/authenticated) sem nenhum acesso às tabelas do sistema.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon, authenticated;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM anon, authenticated;

-- 4) RLS ligado em todas as tabelas. Única política: o usuário da aplicação.
--    (O usuário "postgres", usado em migrações e pelo backend antigo, ignora RLS e continua funcionando.)
DO $$
DECLARE
    t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['clientes', 'pagamentos_mensais', 'importacoes', 'contratos', 'contrato_valores',
                             'configuracoes_importacao', 'curvas_abc', 'arquivos_importacao',
                             'identificadores_externos']
    LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t);
        IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = 'public' AND tablename = t
                       AND policyname = 'app_clientes_acesso') THEN
            EXECUTE format('CREATE POLICY app_clientes_acesso ON public.%I FOR ALL TO app_clientes '
                           'USING (true) WITH CHECK (true)', t);
        END IF;
    END LOOP;
END $$;

-- 5) Login (Supabase Auth): lista de usuários autorizados pela empresa.
--    O login é validado no backend do Worker; o JWT do usuário NÃO chega ao PostgreSQL (o Worker conecta
--    pelo Hyperdrive como app_clientes), por isso o controle é feito na aplicação + nesta lista.
--    A aplicação só LÊ a lista (SELECT). Quem cadastra/desativa é o administrador, no SQL Editor:
--      INSERT INTO public.usuarios_autorizados (email, nome) VALUES ('pessoa@empresa.com.br', 'Nome');
--      UPDATE public.usuarios_autorizados SET ativo = false WHERE email = 'pessoa@empresa.com.br';
CREATE TABLE IF NOT EXISTS public.usuarios_autorizados (
    email TEXT PRIMARY KEY CHECK (email = lower(btrim(email)) AND email LIKE '%_@_%'),
    nome TEXT,
    ativo BOOLEAN NOT NULL DEFAULT true,
    criado_em TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE public.usuarios_autorizados ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.usuarios_autorizados FROM anon, authenticated, app_clientes;
GRANT SELECT ON public.usuarios_autorizados TO app_clientes;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = 'public' AND tablename = 'usuarios_autorizados'
                   AND policyname = 'app_clientes_leitura') THEN
        CREATE POLICY app_clientes_leitura ON public.usuarios_autorizados FOR SELECT TO app_clientes USING (true);
    END IF;
END $$;

-- 6) Importação automática: dados adicionais das planilhas e configurações gerais (fonte do valor pago).
ALTER TABLE public.clientes ADD COLUMN IF NOT EXISTS dados_extras JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE public.contratos ADD COLUMN IF NOT EXISTS dados_extras JSONB NOT NULL DEFAULT '{}'::jsonb;
CREATE TABLE IF NOT EXISTS public.configuracoes_sistema (
    chave TEXT PRIMARY KEY,
    valor JSONB NOT NULL,
    atualizado_em TEXT NOT NULL
);
ALTER TABLE public.configuracoes_sistema ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.configuracoes_sistema FROM anon, authenticated;
GRANT SELECT, INSERT, UPDATE ON public.configuracoes_sistema TO app_clientes;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = 'public' AND tablename = 'configuracoes_sistema'
                   AND policyname = 'app_clientes_acesso') THEN
        CREATE POLICY app_clientes_acesso ON public.configuracoes_sistema FOR ALL TO app_clientes
            USING (true) WITH CHECK (true);
    END IF;
END $$;

-- 6b) Perfil comercial do cliente (classificação automática, editável). Só adiciona colunas.
ALTER TABLE public.clientes ADD COLUMN IF NOT EXISTS tipo_cadastro TEXT;
ALTER TABLE public.clientes ADD COLUMN IF NOT EXISTS escritorio_contabil TEXT;
ALTER TABLE public.clientes ADD COLUMN IF NOT EXISTS situacao_contabil TEXT;
ALTER TABLE public.clientes ADD COLUMN IF NOT EXISTS segmento_nome TEXT;
ALTER TABLE public.clientes ADD COLUMN IF NOT EXISTS regiao1 TEXT;
ALTER TABLE public.clientes ADD COLUMN IF NOT EXISTS regiao2 TEXT;
ALTER TABLE public.clientes ADD COLUMN IF NOT EXISTS perfil_auto JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE public.clientes ADD COLUMN IF NOT EXISTS perfil_manual JSONB NOT NULL DEFAULT '[]'::jsonb;

-- 7) Conferência: RLS ligado, política presente, nenhum privilégio para anon/authenticated.
SELECT c.relname AS tabela,
       c.relrowsecurity AS rls_ativo,
       EXISTS (SELECT 1 FROM pg_policies p WHERE p.schemaname = 'public' AND p.tablename = c.relname
               AND p.policyname = 'app_clientes_acesso') AS politica_app,
       has_table_privilege('anon', c.oid, 'SELECT') AS anon_le,
       has_table_privilege('app_clientes', c.oid, 'SELECT') AS app_le
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind = 'r'
ORDER BY 1;
