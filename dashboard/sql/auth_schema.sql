-- ======================================================================
--  Dashboard authentication — the Postgres half
--
--  Paste into Supabase > SQL editor, or apply with:
--      .\.venv\Scripts\python.exe -m dashboard.apply_sql
--
--  The Python half is dashboard/auth.py. The contract between them:
--  Supabase Auth proves WHO you are, this table decides WHAT you see.
--  `dashboard/app.py` builds the customer tab set (Overview, Forecast
--  Studio, Capacity, Digital Twin) or the admin one (Data Health, Model,
--  Cost & SLA, Lineage & Config) from `profiles.role` — never from which
--  half of the login screen was submitted, because the form is client
--  input and this table is not.
--
--  Idempotent: safe to re-run.
-- ======================================================================

begin;

-- 1 -------------------------------------------------------------- the table
-- One row per account. No row means no dashboard, which is the desired
-- default for anyone who signs up through Supabase directly.
create table if not exists public.profiles (
  id   uuid primary key references auth.users (id),
  role text not null check (role in ('customer', 'admin'))
);

-- Denormalised copy of the email so the admin can read this table on its
-- own; auth.users is not exposed through PostgREST.
alter table public.profiles add column if not exists email      text;
alter table public.profiles add column if not exists created_at timestamptz not null default now();

-- Deleting the auth user should take the profile with it. Without the
-- cascade, removing an account fails on this foreign key and leaves a
-- role row pointing at nothing.
alter table public.profiles drop constraint if exists profiles_id_fkey;
alter table public.profiles
  add constraint profiles_id_fkey
  foreign key (id) references auth.users (id) on delete cascade;


-- 2 ---------------------------------------------------------------- the rules
alter table public.profiles enable row level security;

drop policy if exists "read own profile" on public.profiles;
create policy "read own profile" on public.profiles
  for select
  to authenticated
  using (auth.uid() = id);

-- There is deliberately NO insert/update/delete policy. RLS denies what
-- it does not permit, so a signed-in customer holding the anon key cannot
-- write `admin` into their own row — the one escalation path that would
-- make the whole split cosmetic. Role changes happen here, in the SQL
-- editor, under the service credentials.
--
-- Supabase's default grants hand anon/authenticated full DML on new
-- public tables and rely on RLS alone to stop it. Revoking as well means
-- a future `alter table ... disable row level security` is a broken
-- dashboard rather than a silent privilege escalation.
revoke insert, update, delete, truncate on public.profiles from anon, authenticated;
grant  select on public.profiles to authenticated;


-- 3 ------------------------------------------------------------- new signups
-- Anyone with the anon key can call Supabase's signup endpoint, and that
-- request carries user-supplied metadata. So the role is hardcoded here
-- rather than read from `new.raw_user_meta_data->>'role'`: reading it
-- would let a stranger mint themselves an admin account by sending one
-- extra JSON field. Promotion is a deliberate act — see seed_users.sql.
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  insert into public.profiles (id, email, role)
  values (new.id, new.email, 'customer')
  on conflict (id) do nothing;
  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();

commit;
