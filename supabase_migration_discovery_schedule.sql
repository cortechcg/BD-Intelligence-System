-- Durable singleton lease for the production discovery runner and truthful
-- per-source result counts. Apply through the repository's normal Supabase
-- migration process before deploying the worker change.

alter table public.procurement_sources
    add column if not exists last_discovered_count integer,
    add column if not exists last_duplicate_count integer,
    add column if not exists last_filtered_count integer,
    add column if not exists last_stored_count integer,
    add column if not exists candidate_count_total bigint not null default 0,
    add column if not exists duplicate_count_total bigint not null default 0,
    add column if not exists filtered_count_total bigint not null default 0,
    add column if not exists stored_count_total bigint not null default 0;

-- The historical discovered_count column records successful ledger inserts.
-- Preserve it and copy that known value into stored_count_total; candidate,
-- duplicate and filtered history was never recorded and is deliberately not
-- reconstructed.
update public.procurement_sources
   set stored_count_total = discovered_count
 where stored_count_total = 0 and discovered_count > 0;

create table if not exists public.discovery_schedule_state (
    schedule_key text primary key check (schedule_key = 'production'),
    next_run_at timestamptz not null default now(),
    last_attempt_at timestamptz,
    last_success_at timestamptz,
    last_outcome text not null default 'never'
        check (last_outcome in ('never', 'running', 'success', 'partial', 'failure')),
    last_error text not null default '',
    last_summary jsonb not null default '{}'::jsonb,
    consecutive_failures integer not null default 0 check (consecutive_failures >= 0),
    lease_token uuid,
    lease_expires_at timestamptz,
    updated_at timestamptz not null default now()
);

insert into public.discovery_schedule_state (schedule_key, next_run_at)
values ('production', now())
on conflict (schedule_key) do nothing;

alter table public.discovery_schedule_state enable row level security;
revoke all on public.discovery_schedule_state from anon, authenticated;
grant all on public.discovery_schedule_state to service_role;

create or replace function public.claim_discovery_schedule(
    p_token uuid,
    p_lease_seconds integer default 900
) returns boolean
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    claimed_count integer;
begin
    update public.discovery_schedule_state
       set lease_token = p_token,
           lease_expires_at = now() + make_interval(secs => greatest(120, least(p_lease_seconds, 1800))),
           last_attempt_at = now(),
           last_outcome = 'running',
           last_error = '',
           updated_at = now()
     where schedule_key = 'production'
       and next_run_at <= now()
       and (lease_expires_at is null or lease_expires_at <= now());
    get diagnostics claimed_count = row_count;
    return claimed_count = 1;
end;
$$;

create or replace function public.finish_discovery_schedule(
    p_token uuid,
    p_outcome text,
    p_interval_seconds integer default 7200,
    p_error text default '',
    p_summary jsonb default '{}'::jsonb
) returns boolean
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    finish_count integer;
begin
    if p_outcome not in ('success', 'partial', 'failure') then
        return false;
    end if;

    update public.discovery_schedule_state
       set last_success_at = case when p_outcome in ('success', 'partial') then now() else last_success_at end,
           last_outcome = p_outcome,
           last_error = left(coalesce(p_error, ''), 500),
           last_summary = coalesce(p_summary, '{}'::jsonb),
           consecutive_failures = case when p_outcome = 'failure' then consecutive_failures + 1 else 0 end,
           next_run_at = case
               when p_outcome in ('success', 'partial') then now() + make_interval(secs => greatest(3600, least(p_interval_seconds, 86400)))
               when consecutive_failures < 1 then now() + interval '5 minutes'
               when consecutive_failures < 3 then now() + interval '15 minutes'
               when consecutive_failures < 5 then now() + interval '1 hour'
               when consecutive_failures < 8 then now() + interval '6 hours'
               else now() + interval '24 hours'
           end,
           lease_token = null,
           lease_expires_at = null,
           updated_at = now()
     where schedule_key = 'production' and lease_token = p_token;
    get diagnostics finish_count = row_count;
    return finish_count = 1;
end;
$$;

create or replace function public.finish_procurement_source_run_with_counts(
    p_source_id uuid,
    p_token uuid,
    p_outcome text,
    p_error text default '',
    p_discovered_delta integer default 0,
    p_duplicate_delta integer default 0,
    p_filtered_delta integer default 0,
    p_stored_delta integer default 0,
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
           last_discovered_count = case when p_outcome = 'success' then greatest(0, least(p_discovered_delta, 1000)) else last_discovered_count end,
           last_duplicate_count = case when p_outcome = 'success' then greatest(0, least(p_duplicate_delta, 1000)) else last_duplicate_count end,
           last_filtered_count = case when p_outcome = 'success' then greatest(0, least(p_filtered_delta, 1000)) else last_filtered_count end,
           last_stored_count = case when p_outcome = 'success' then greatest(0, least(p_stored_delta, 1000)) else last_stored_count end,
           candidate_count_total = candidate_count_total + case when p_outcome = 'success' then greatest(0, least(p_discovered_delta, 1000)) else 0 end,
           duplicate_count_total = duplicate_count_total + case when p_outcome = 'success' then greatest(0, least(p_duplicate_delta, 1000)) else 0 end,
           filtered_count_total = filtered_count_total + case when p_outcome = 'success' then greatest(0, least(p_filtered_delta, 1000)) else 0 end,
           stored_count_total = stored_count_total + case when p_outcome = 'success' then greatest(0, least(p_stored_delta, 1000)) else 0 end,
           discovered_count = discovered_count + case when p_outcome = 'success' then greatest(0, least(p_stored_delta, 1000)) else 0 end,
           last_preview = case when p_outcome = 'success' then coalesce(p_preview, '[]'::jsonb) else last_preview end,
           capability = coalesce(nullif(p_capability, ''), capability),
           last_error = case when p_outcome = 'success' then ''
               when p_outcome = 'failure' then left(coalesce(p_error, ''), 500) else last_error end,
           consecutive_failures = case when p_outcome = 'success' then 0
               when p_outcome = 'failure' then consecutive_failures + 1 else consecutive_failures end,
           next_attempt_at = case when p_outcome in ('success', 'cancelled') then null
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

revoke all on function public.claim_discovery_schedule(uuid, integer) from public, anon, authenticated;
revoke all on function public.finish_discovery_schedule(uuid, text, integer, text, jsonb) from public, anon, authenticated;
revoke all on function public.finish_procurement_source_run_with_counts(uuid, uuid, text, text, integer, integer, integer, integer, jsonb, text) from public, anon, authenticated;
grant execute on function public.claim_discovery_schedule(uuid, integer) to service_role;
grant execute on function public.finish_discovery_schedule(uuid, text, integer, text, jsonb) to service_role;
grant execute on function public.finish_procurement_source_run_with_counts(uuid, uuid, text, text, integer, integer, integer, integer, jsonb, text) to service_role;
