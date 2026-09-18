create table public.generations (
 id uuid primary key,
 user_id uuid not null references auth.users(id) on delete cascade,
 settings jsonb not null,
 status text not null default 'submitting' check(status in ('submitting','queued','processing','completed','failed','cancelled','needs_review')),
 provider_id text unique,
 video_url text,
 error text,
 created_at timestamptz not null default now()
);
create index generations_owner_created on public.generations(user_id, created_at desc);
alter table public.generations enable row level security;
revoke all on public.generations from anon, authenticated;
grant select on public.generations to authenticated;
grant all on public.generations to service_role;
create policy owner_read on public.generations for select to authenticated using ((select auth.uid()) = user_id);

-- Serializes reservations per user: protects double clicks and concurrent requests.
create function public.reserve_generation(p_id uuid, p_user uuid, p_settings jsonb)
returns jsonb language plpgsql security definer set search_path = public as $$
declare existing public.generations; fresh public.generations;
begin
 perform pg_advisory_xact_lock(hashtextextended(p_user::text, 0));
 select * into existing from public.generations where id = p_id;
 if found then
  if existing.user_id <> p_user then return jsonb_build_object('error','Request identifier unavailable.'); end if;
  return jsonb_build_object('created',false,'job',to_jsonb(existing));
 end if;
 if (select count(*) from public.generations where user_id=p_user and created_at > now()-interval '24 hours') >= 10 then
  return jsonb_build_object('error','Daily beta limit reached. Try again tomorrow.');
 end if;
 if exists(select 1 from public.generations where user_id=p_user and status in ('submitting','queued','processing','needs_review')) then
  return jsonb_build_object('error','Your previous generation is still active or needs review.');
 end if;
 insert into public.generations(id,user_id,settings) values(p_id,p_user,p_settings) returning * into fresh;
 return jsonb_build_object('created',true,'job',to_jsonb(fresh));
end $$;
revoke all on function public.reserve_generation(uuid,uuid,jsonb) from public, anon, authenticated;
grant execute on function public.reserve_generation(uuid,uuid,jsonb) to service_role;

insert into storage.buckets(id,name,public,file_size_limit,allowed_mime_types)
values('references','references',false,4194304,array['image/jpeg'])
on conflict(id) do nothing;
-- Private references are only accessible through the authenticated server.
