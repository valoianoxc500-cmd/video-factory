-- AI Video Maker persistence. Additive only: two new tables, one new claim
-- function, and nothing existing is altered or dropped. Quote Studio, the VRF
-- tables and the channel `jobs` queue are untouched.
--
-- Its own queue, deliberately. AI Video Maker could have been another channel
-- on the shared `jobs` table, but then a stuck AI Video Maker job would sit in
-- the same queue the other products are claimed from. A separate table and a
-- separate worker loop is the same isolation vrf_tasks already uses, and is
-- what makes "a broken job in one product cannot block another" true rather
-- than merely intended.

create table if not exists public.ai_video_prefs (
  user_id      uuid primary key references auth.users(id) on delete cascade,
  -- One JSON blob rather than thirty columns. These are presentation
  -- preferences read and written as a unit by one form, and the engine
  -- already clamps every field on the way in (aivideo/spec.normalise_spec),
  -- so a preference written by an older client stays readable after the form
  -- grows a control.
  settings     jsonb       not null default '{}'::jsonb,
  named_presets jsonb      not null default '[]'::jsonb,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);

create table if not exists public.ai_video_jobs (
  id            uuid        primary key default gen_random_uuid(),
  user_id       uuid        not null references auth.users(id) on delete cascade,
  topic         text        not null default '',
  language      text        not null default 'en',
  duration_seconds int      not null default 30,
  aspect_ratio  text        not null default '9:16',
  spec          jsonb       not null default '{}'::jsonb,

  -- queued -> running -> done | error. `queued` is also what a recoverable
  -- failure returns to, which is what makes an automatic resume a status
  -- change rather than a new job.
  status        text        not null default 'queued',
  stage         text        not null default '',
  progress      int         not null default 0,
  message       text        not null default '',
  -- Customer-safe only. Provider names, HTTP statuses and stack traces stay
  -- in the worker log and never reach this column.
  error         text        not null default '',

  video_url     text        not null default '',
  thumbnail_url text        not null default '',
  duration_actual real      not null default 0,
  cost_usd       numeric(10, 4) not null default 0,
  providers_used text[]     not null default '{}',
  fallbacks      text[]     not null default '{}',

  attempts      int         not null default 0,
  claimed_at    timestamptz,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

create index if not exists ai_video_jobs_user_created_idx
  on public.ai_video_jobs (user_id, created_at desc);
-- The worker's claim path: oldest queued job first.
create index if not exists ai_video_jobs_queue_idx
  on public.ai_video_jobs (status, created_at)
  where status in ('queued', 'running');

alter table public.ai_video_prefs enable row level security;
alter table public.ai_video_jobs  enable row level security;

-- Same tenant isolation as every other table here: a row is reachable only by
-- the user it belongs to, enforced per command.
create policy ai_video_prefs_select_own on public.ai_video_prefs
  for select using (auth.uid() = user_id);
create policy ai_video_prefs_insert_own on public.ai_video_prefs
  for insert with check (auth.uid() = user_id);
create policy ai_video_prefs_update_own on public.ai_video_prefs
  for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy ai_video_prefs_delete_own on public.ai_video_prefs
  for delete using (auth.uid() = user_id);

create policy ai_video_jobs_select_own on public.ai_video_jobs
  for select using (auth.uid() = user_id);
create policy ai_video_jobs_insert_own on public.ai_video_jobs
  for insert with check (auth.uid() = user_id);
create policy ai_video_jobs_update_own on public.ai_video_jobs
  for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy ai_video_jobs_delete_own on public.ai_video_jobs
  for delete using (auth.uid() = user_id);


-- ── worker claim ────────────────────────────────────────────────────
--
-- Both functions below run as SECURITY DEFINER, because the worker acts on
-- rows it does not own and has no user session. They are reachable with the
-- publishable key, so each verifies the shared worker token through the
-- existing `_worker_authorized()` helper — the same check `worker_claim_job`
-- already uses, against the same hashed secret in `worker_credentials`. No new
-- secret store and nothing extra to seed: rotating the worker token in one
-- place rotates it for this product too.
--
-- `for update skip locked` is what keeps two workers from claiming the same
-- job. A job whose worker died mid-run is reclaimed after the stale window
-- rather than sitting `running` forever; it resumes from its checkpoint on
-- disk, so reclaiming costs only the stage that was interrupted.

create or replace function public.ai_video_claim_job(p_token text, p_stale_minutes int default 25)
returns setof public.ai_video_jobs
language plpgsql
security definer
set search_path = public
as $$
begin
  if not public._worker_authorized(p_token) then
    raise exception 'unauthorized';
  end if;

  return query
  with candidate as (
    select id from public.ai_video_jobs
     where status = 'queued'
        or (status = 'running'
            and claimed_at is not null
            and claimed_at < now() - make_interval(mins => p_stale_minutes))
     order by created_at
     limit 1
     for update skip locked
  )
  update public.ai_video_jobs j
     set status     = 'running',
         claimed_at = now(),
         attempts   = j.attempts + 1,
         updated_at = now()
    from candidate c
   where j.id = c.id
  returning j.*;
end;
$$;


-- ── worker update ───────────────────────────────────────────────────
--
-- Only the fields the worker actually sends are written. A progress ping that
-- carried nulls for everything else would blank the video URL a later call
-- had already recorded, so every column is coalesced against its current
-- value.

create or replace function public.ai_video_update_job(
  p_token          text,
  p_id             uuid,
  p_status         text default null,
  p_stage          text default null,
  p_progress       int  default null,
  p_message        text default null,
  p_error          text default null,
  p_video_url      text default null,
  p_thumbnail_url  text default null,
  p_duration       real default null,
  p_cost_usd       numeric default null,
  p_providers      text[] default null,
  p_fallbacks      text[] default null
)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
  if not public._worker_authorized(p_token) then
    raise exception 'unauthorized';
  end if;

  update public.ai_video_jobs
     set status          = coalesce(p_status, status),
         stage           = coalesce(p_stage, stage),
         progress        = coalesce(p_progress, progress),
         message         = coalesce(p_message, message),
         error           = coalesce(p_error, error),
         video_url       = coalesce(nullif(p_video_url, ''), video_url),
         thumbnail_url   = coalesce(nullif(p_thumbnail_url, ''), thumbnail_url),
         duration_actual = coalesce(p_duration, duration_actual),
         cost_usd        = coalesce(p_cost_usd, cost_usd),
         providers_used  = coalesce(p_providers, providers_used),
         fallbacks       = coalesce(p_fallbacks, fallbacks),
         updated_at      = now()
   where id = p_id;
end;
$$;

-- Executable with the publishable key, because that is what the server route
-- holds; the token check inside each function is the actual gate.
grant execute on function public.ai_video_claim_job(text, int) to anon, authenticated;
grant execute on function public.ai_video_update_job(
  text, uuid, text, text, int, text, text, text, text, real, numeric, text[], text[]
) to anon, authenticated;
