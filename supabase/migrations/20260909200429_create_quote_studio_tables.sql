-- Quote Studio persistence. Additive only: creates two new tables and
-- touches nothing that already exists.
--
-- background_id defaults to 'pure_white' so a row written before a client
-- knows about backgrounds -- or by an older client that omits the column --
-- reads back as the default the deck was designed against, rather than null.

create table if not exists public.quote_studio_profiles (
  user_id            uuid primary key references auth.users(id) on delete cascade,
  photo              text        not null default '',
  display_name       text        not null default '',
  username           text        not null default '',
  preferred_language text        not null default 'en',
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now()
);

create table if not exists public.quote_studio_projects (
  id               uuid primary key default gen_random_uuid(),
  user_id          uuid        not null references auth.users(id) on delete cascade,
  topic            text        not null default '',
  language         text        not null default 'en',
  font_id          text        not null default '',
  ratio            text        not null default '4:5',
  background_id    text        not null default 'pure_white',
  profile_snapshot jsonb       not null default '{}'::jsonb,
  quotes           jsonb       not null default '[]'::jsonb,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);

create index if not exists quote_studio_projects_user_updated_idx
  on public.quote_studio_projects (user_id, updated_at desc);

alter table public.quote_studio_profiles enable row level security;
alter table public.quote_studio_projects enable row level security;

-- Same tenant isolation as vrf_assets / vrf_tasks: a row is reachable only
-- by the user it belongs to, enforced per command.
create policy quote_studio_profiles_select_own on public.quote_studio_profiles
  for select using (auth.uid() = user_id);
create policy quote_studio_profiles_insert_own on public.quote_studio_profiles
  for insert with check (auth.uid() = user_id);
create policy quote_studio_profiles_update_own on public.quote_studio_profiles
  for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy quote_studio_profiles_delete_own on public.quote_studio_profiles
  for delete using (auth.uid() = user_id);

create policy quote_studio_projects_select_own on public.quote_studio_projects
  for select using (auth.uid() = user_id);
create policy quote_studio_projects_insert_own on public.quote_studio_projects
  for insert with check (auth.uid() = user_id);
create policy quote_studio_projects_update_own on public.quote_studio_projects
  for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy quote_studio_projects_delete_own on public.quote_studio_projects
  for delete using (auth.uid() = user_id);
