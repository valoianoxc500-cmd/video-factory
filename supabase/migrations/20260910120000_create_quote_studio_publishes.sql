-- Quote Studio publishing state. Additive only: one new table, and nothing
-- existing is altered or dropped.
--
-- This table exists for one reason: a publish that is retried must not post
-- twice. Everything else it records is in service of that. The idempotency
-- key is derived from the project, the platform and the exact carousel
-- content, so pressing Publish Now again after a network timeout resolves to
-- the same row rather than a second Instagram post.
--
-- What is deliberately NOT here: OAuth access tokens. Those live encrypted in
-- vrf_accounts and are read only by the code that talks to the provider. A
-- publishing audit table is the wrong place for a credential, and putting one
-- here would put it in every backup of this table forever.

create table if not exists public.quote_studio_publishes (
  id               uuid primary key default gen_random_uuid(),
  user_id          uuid        not null references auth.users(id) on delete cascade,
  -- Not a foreign key on purpose: a publish is a record of something that
  -- actually happened on a third-party platform, and deleting the draft it
  -- came from should not erase the evidence that a post exists.
  quote_project_id uuid        not null,
  platform         text        not null,
  idempotency_key  text        not null,
  status           text        not null default 'pending',
  remote_post_id   text        not null default '',
  -- A category, never a provider message. "auth_expired", "rate_limited",
  -- "media_rejected" -- enough to choose a customer sentence and to decide
  -- whether a retry is sane, with nothing quotable from the provider.
  error_code       text        not null default '',
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),

  constraint quote_studio_publishes_status_check
    check (status in ('pending', 'publishing', 'published', 'failed')),
  constraint quote_studio_publishes_platform_check
    check (platform in ('instagram', 'facebook', 'threads', 'x'))
);

-- The duplicate-post guard itself. One row per user, platform and payload:
-- a second attempt with the same content collides here instead of reaching
-- the provider.
create unique index if not exists quote_studio_publishes_idempotency_idx
  on public.quote_studio_publishes (user_id, platform, idempotency_key);

create index if not exists quote_studio_publishes_project_idx
  on public.quote_studio_publishes (user_id, quote_project_id, created_at desc);

alter table public.quote_studio_publishes enable row level security;

-- Same tenant isolation as quote_studio_projects and vrf_assets: a row is
-- reachable only by the user it belongs to, enforced per command.
create policy quote_studio_publishes_select_own on public.quote_studio_publishes
  for select using (auth.uid() = user_id);
create policy quote_studio_publishes_insert_own on public.quote_studio_publishes
  for insert with check (auth.uid() = user_id);
create policy quote_studio_publishes_update_own on public.quote_studio_publishes
  for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy quote_studio_publishes_delete_own on public.quote_studio_publishes
  for delete using (auth.uid() = user_id);
