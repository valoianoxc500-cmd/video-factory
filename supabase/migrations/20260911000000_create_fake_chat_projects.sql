-- Fake Chat Studio projects. Additive: no existing objects are modified.
create table if not exists public.fake_chat_projects (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  title text not null default 'Untitled chat',
  document jsonb not null default '{"version":1}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists fake_chat_projects_user_updated_idx
  on public.fake_chat_projects (user_id, updated_at desc);

alter table public.fake_chat_projects enable row level security;
revoke all on table public.fake_chat_projects from anon, authenticated;
grant select, insert, update, delete on table public.fake_chat_projects to authenticated;

create policy fake_chat_projects_select_own on public.fake_chat_projects
  for select to authenticated using ((select auth.uid()) = user_id);
create policy fake_chat_projects_insert_own on public.fake_chat_projects
  for insert to authenticated with check ((select auth.uid()) = user_id);
create policy fake_chat_projects_update_own on public.fake_chat_projects
  for update to authenticated using ((select auth.uid()) = user_id)
  with check ((select auth.uid()) = user_id);
create policy fake_chat_projects_delete_own on public.fake_chat_projects
  for delete to authenticated using ((select auth.uid()) = user_id);
