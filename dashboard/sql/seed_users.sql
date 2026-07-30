-- ======================================================================
--  The two demo accounts
--
--  Run auth_schema.sql first. Paste this into Supabase > SQL editor, or:
--      .\.venv\Scripts\python.exe -m dashboard.apply_sql
--
--  Supabase's Authentication > Users screen can create an account but not
--  give it a role, and its signup endpoint always yields `customer` (see
--  the trigger in auth_schema.sql). This does both in one statement, so
--  an admin exists without a service_role key ever leaving the SQL editor.
--
--  Idempotent: re-running resets these accounts' passwords to whatever is
--  in the VALUES list below and re-asserts their roles. Add a row to the
--  list to add an account.
-- ======================================================================

begin;

do $$
declare
  seed record;
  uid  uuid;
begin
  for seed in
    select * from (values
      --  email                    password              role
      ('customer@example.com',  'CustomerDemo#2026',  'customer'),
      ('admin@example.com',     'AdminDemo#2026',     'admin')
    ) as t(email, password, role)
  loop
    select u.id into uid from auth.users u where u.email = seed.email;

    if uid is null then
      uid := extensions.gen_random_uuid();

      -- email_confirmed_at is set here because nobody is going to click a
      -- confirmation link for a demo account, and GoTrue refuses to issue
      -- a session for an unconfirmed email.
      --
      -- The four empty-string token columns are not decoration: GoTrue
      -- scans them into Go `string`s during sign-in, and a NULL there
      -- fails the login with an opaque type error rather than a wrong
      -- password. Rows made through the API get '' — rows made by hand
      -- have to say so.
      insert into auth.users (
        instance_id, id, aud, role, email, encrypted_password,
        email_confirmed_at, raw_app_meta_data, raw_user_meta_data,
        created_at, updated_at,
        confirmation_token, recovery_token, email_change, email_change_token_new
      ) values (
        '00000000-0000-0000-0000-000000000000', uid,
        'authenticated', 'authenticated', seed.email,
        extensions.crypt(seed.password, extensions.gen_salt('bf')),
        now(),
        '{"provider":"email","providers":["email"]}'::jsonb,
        '{}'::jsonb,
        now(), now(),
        '', '', '', ''
      );

      -- An auth.users row alone is not a working account: the email
      -- provider looks the user up through auth.identities.
      insert into auth.identities (
        provider_id, user_id, identity_data, provider,
        last_sign_in_at, created_at, updated_at
      ) values (
        uid::text, uid,
        jsonb_build_object(
          'sub', uid::text,
          'email', seed.email,
          'email_verified', true,
          'phone_verified', false
        ),
        'email', now(), now(), now()
      );

    else
      update auth.users set
        encrypted_password = extensions.crypt(seed.password, extensions.gen_salt('bf')),
        email_confirmed_at = coalesce(email_confirmed_at, now()),
        updated_at         = now()
      where id = uid;
    end if;

    -- The trigger already wrote a `customer` row for a brand-new user;
    -- this is what makes an admin an admin.
    insert into public.profiles (id, email, role)
    values (uid, seed.email, seed.role)
    on conflict (id) do update
      set role  = excluded.role,
          email = excluded.email;
  end loop;
end
$$;

commit;
