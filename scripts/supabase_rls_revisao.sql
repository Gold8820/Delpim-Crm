-- REVISÃO DE SEGURANÇA DO SUPABASE. NÃO EXECUTE SEM REVISAR.
--
-- Por que existe: o Supabase expõe automaticamente as tabelas do schema "public" pela API (papéis anon e
-- authenticated). Sem RLS habilitado, qualquer pessoa com a chave pública do projeto pode ler os dados
-- dos clientes. A aplicação conecta com o usuário "postgres" (que ignora RLS), então estas instruções
-- NÃO afetam o funcionamento do sistema; bloqueiam apenas o acesso pela API pública.
--
-- Como usar: revise, rode no SQL Editor do Supabase (ou via psql) e confirme que a aplicação continua
-- funcionando (a tela de clientes deve carregar normalmente).

-- 1) Remove o acesso da API pública (anon e authenticated) a todas as tabelas do schema public.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon, authenticated;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM anon, authenticated;

-- 2) Habilita RLS em todas as tabelas da aplicação. Sem políticas, anon/authenticated não leem nada.
ALTER TABLE public.clientes                  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.pagamentos_mensais        ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.importacoes               ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.contratos                 ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.contrato_valores          ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.configuracoes_importacao  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.curvas_abc                ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.arquivos_importacao       ENABLE ROW LEVEL SECURITY;   -- contém as planilhas enviadas
ALTER TABLE public.identificadores_externos  ENABLE ROW LEVEL SECURITY;

-- 3) Verificação: todas as linhas devem mostrar rls_ativo = true e nenhum privilégio para anon/authenticated.
SELECT c.relname AS tabela, c.relrowsecurity AS rls_ativo
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind = 'r'
ORDER BY 1;

-- Pendências (não resolvidas por este script): o sistema ainda NÃO tem login de usuários no banco.
-- O controle de acesso atual é a senha ACCESS_PASSWORD na aplicação. Antes de abrir a usuários
-- diferentes, implemente autenticação real (ex.: Supabase Auth) e políticas por usuário.
