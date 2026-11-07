-- Durable user-configured procurement sources and cross-process check leases.
-- Apply this migration through the normal reviewed Supabase migration process.

alter table if exists public.opportunities_cache
    add column if not exists source_metadata jsonb not null default '{}'::jsonb;

create table if not exists public.procurement_sources (
    id uuid primary key default gen_random_uuid(),
    source_key text not null unique,
    name text not null,
    url text not null unique,
    source_type text not null check (source_type in ('builtin', 'custom')),
    capability text not null default 'adapter_required',
    adapter_config jsonb not null default '{}'::jsonb,
    enabled boolean not null default false,
    status text not null default 'needs_attention',
    last_attempt_at timestamptz,
    last_success_at timestamptz,
    discovered_count bigint not null default 0 check (discovered_count >= 0),
    last_error text not null default '',
    last_preview jsonb not null default '[]'::jsonb,
    consecutive_failures integer not null default 0 check (consecutive_failures >= 0),
    next_attempt_at timestamptz,
    run_token uuid,
    lease_expires_at timestamptz,
    created_by text not null default '',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists procurement_sources_enabled_due_idx
    on public.procurement_sources (enabled, next_attempt_at, last_attempt_at)
    where source_type = 'custom';
create index if not exists procurement_sources_custom_active_lease_idx
    on public.procurement_sources (lease_expires_at)
    where source_type = 'custom' and run_token is not null;

alter table public.procurement_sources enable row level security;
revoke all on public.procurement_sources from anon, authenticated;
grant all on public.procurement_sources to service_role;

create or replace function public.claim_procurement_source_run(
    p_source_id uuid,
    p_token uuid,
    p_lease_seconds integer default 120,
    p_allow_disabled boolean default false,
    p_respect_backoff boolean default true
) returns boolean
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    claimed_count integer;
    active_count integer;
    source_kind text;
begin
    select source_type into source_kind
      from public.procurement_sources
     where id = p_source_id;
    if not found then
        return false;
    end if;

    -- Custom feed checks can originate from multiple dashboard replicas.
    -- Serialize claims only long enough to enforce a global concurrency cap.
    if source_kind = 'custom' then
        perform pg_advisory_xact_lock(731849201);
        select count(*) into active_count
          from public.procurement_sources
         where source_type = 'custom'
           and run_token is not null
           and lease_expires_at > now();
        if active_count >= 4 then
            return false;
        end if;
    end if;

    update public.procurement_sources
       set run_token = p_token,
           lease_expires_at = now() + make_interval(secs => greatest(30, least(p_lease_seconds, 600))),
           last_attempt_at = now(),
           status = 'checking',
           updated_at = now()
     where id = p_source_id
       and (p_allow_disabled or enabled)
       and (not p_allow_disabled or last_attempt_at is null
            or last_attempt_at <= now() - interval '60 seconds')
       and (lease_expires_at is null or lease_expires_at <= now())
       and (not p_respect_backoff or next_attempt_at is null or next_attempt_at <= now());
    get diagnostics claimed_count = row_count;
    return claimed_count = 1;
end;
$$;

create or replace function public.finish_procurement_source_run(
    p_source_id uuid,
    p_token uuid,
    p_outcome text,
    p_error text default '',
    p_discovered_delta integer default 0,
    p_preview jsonb default '[]'::jsonb,
    p_capability text default null
) returns boolean
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    finish_count integer;
begin
    if p_outcome not in ('success', 'failure', 'cancelled') then
        return false;
    end if;

    update public.procurement_sources
       set last_success_at = case when p_outcome = 'success' then now() else last_success_at end,
           discovered_count = discovered_count + case when p_outcome = 'success'
               then greatest(0, least(p_discovered_delta, 1000)) else 0 end,
           last_preview = case when p_outcome = 'success' then coalesce(p_preview, '[]'::jsonb)
               else last_preview end,
           capability = coalesce(nullif(p_capability, ''), capability),
           last_error = case when p_outcome = 'success' then ''
               when p_outcome = 'failure' then left(coalesce(p_error, ''), 500)
               else last_error end,
           consecutive_failures = case when p_outcome = 'success' then 0
               when p_outcome = 'failure' then consecutive_failures + 1
               else consecutive_failures end,
           next_attempt_at = case when p_outcome = 'success' or p_outcome = 'cancelled' then null
               when consecutive_failures < 1 then now() + interval '5 minutes'
               when consecutive_failures < 3 then now() + interval '15 minutes'
               when consecutive_failures < 5 then now() + interval '1 hour'
               when consecutive_failures < 8 then now() + interval '6 hours'
               else now() + interval '24 hours' end,
           enabled = case when p_outcome = 'failure'
                    and coalesce(p_capability, capability) in
                        ('unsupported', 'authentication_required', 'access_restricted', 'captcha_blocked', 'adapter_required')
               then false else enabled end,
           status = case when p_outcome = 'success' and enabled then 'ready'
               when p_outcome = 'success' then 'paused'
               when p_outcome = 'failure' then 'needs_attention'
               when not enabled then 'paused' else 'ready' end,
           run_token = null,
           lease_expires_at = null,
           updated_at = now()
     where id = p_source_id and run_token = p_token;
    get diagnostics finish_count = row_count;
    return finish_count = 1;
end;
$$;

create or replace function public.set_procurement_source_enabled(
    p_source_id uuid,
    p_enabled boolean
) returns boolean
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    changed_count integer;
begin
    if p_enabled then
        update public.procurement_sources
           set enabled = true, status = 'ready', updated_at = now()
         where id = p_source_id
           and source_type = 'custom'
           and capability in ('rss_atom', 'public_listing')
           and last_success_at is not null;
    else
        update public.procurement_sources
           set enabled = false, status = 'paused', updated_at = now()
         where id = p_source_id and source_type = 'custom';
    end if;
    get diagnostics changed_count = row_count;
    return changed_count = 1;
end;
$$;

revoke all on function public.claim_procurement_source_run(uuid, uuid, integer, boolean, boolean) from public, anon, authenticated;
revoke all on function public.finish_procurement_source_run(uuid, uuid, text, text, integer, jsonb, text) from public, anon, authenticated;
revoke all on function public.set_procurement_source_enabled(uuid, boolean) from public, anon, authenticated;
grant execute on function public.claim_procurement_source_run(uuid, uuid, integer, boolean, boolean) to service_role;
grant execute on function public.finish_procurement_source_run(uuid, uuid, text, text, integer, jsonb, text) to service_role;
grant execute on function public.set_procurement_source_enabled(uuid, boolean) to service_role;
