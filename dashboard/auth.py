"""
Supabase-backed authentication for the dashboard.

Two audiences share one app: customer accounts see the forecast /
allocation surface (Overview, Forecast Studio, Capacity, Digital Twin),
admin accounts see the operational one (Data Health, Model, Cost & SLA,
Lineage & Config). Both sign in against the same Supabase Auth user
pool — which tabs a session unlocks is decided by the `role` column on
that user's row in the `profiles` table, never by which half of the
login screen was submitted. That is what stops a customer account from
picking the "Admin Login" tab and getting admin tabs: the role lookup
happens after Supabase confirms the password, against a table the user
cannot write.

Setup
-----
1. Create a Supabase project (https://supabase.com).
2. Authentication > Providers > enable Email.
3. Copy `.env.example` to `.env` (already gitignored) and fill in all
   three values from Settings > API and Settings > Database:

       SUPABASE_URL       Project URL
       SUPABASE_ANON_KEY  anon / public key (NOT the service_role key)
       SUPABASE_DB_URL    direct Postgres URI, used by step 4 only

4. Create the table, the row level security policies, the signup
   trigger and the two demo accounts:

       .\\.venv\\Scripts\\python.exe -m dashboard.apply_sql

   That runs `dashboard/sql/auth_schema.sql` then
   `dashboard/sql/seed_users.sql`; both are idempotent and both can be
   pasted straight into Supabase > SQL editor instead. Adding an
   account means adding a row to the VALUES list in seed_users.sql —
   accounts created any other way come out as `customer`, deliberately.

This mirrors the bootstrap-exception pattern in config.py (DB_PATH etc.):
these two values can't come from the `config` table because they're
needed before any database is reachable.
"""

import os

import streamlit as st

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY")


def _client():
    """One client per browser session, NOT one per process.

    The obvious spelling here is `@st.cache_resource`, and it is wrong:
    that cache is global, so every visitor would share a single client
    object — and the client is where the signed-in JWT lives. Two people
    on the dashboard at once would take turns overwriting each other's
    token, `_fetch_role` would run as whoever logged in last, and one
    person's Log out would end everybody's session. `st.session_state`
    is per-connection, which is the scope a credential belongs in.
    """
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        return None
    if "supabase_client" not in st.session_state:
        from supabase import create_client
        st.session_state["supabase_client"] = create_client(
            SUPABASE_URL, SUPABASE_ANON_KEY
        )
    return st.session_state["supabase_client"]


def _fetch_role(client, user_id):
    """The role is looked up server-side, never trusted from the form."""
    try:
        resp = (
            client.table("profiles")
            .select("role")
            .eq("id", user_id)
            .single()
            .execute()
        )
        return (resp.data or {}).get("role")
    except Exception:
        return None


def _sign_in(client, email, password, wanted_role):
    try:
        result = client.auth.sign_in_with_password(
            {"email": email, "password": password}
        )
    except Exception as exc:                                  # noqa: BLE001
        return None, f"Sign-in failed: {exc}"

    user = getattr(result, "user", None)
    if user is None:
        return None, "Invalid email or password."

    role = _fetch_role(client, user.id)
    if role != wanted_role:
        client.auth.sign_out()
        got = role or "no"
        return None, (
            f"This account has {got} access, not {wanted_role} access."
        )

    return {"email": user.email, "id": user.id, "role": role}, None


def _login_form(client, role):
    with st.form(f"login_{role}", clear_on_submit=False):
        email = st.text_input("Email", key=f"email_{role}")
        password = st.text_input(
            "Password", type="password", key=f"password_{role}"
        )
        submitted = st.form_submit_button("Sign in", type="primary",
                                          width="stretch")

    if submitted:
        user, error = _sign_in(client, email, password, role)
        if error:
            st.error(error)
        else:
            st.session_state["auth_user"] = user
            st.rerun()


def require_login():
    """Blocks the app until a customer or admin session exists.

    Returns the session dict: {"email", "id", "role"}.
    """
    if "auth_user" in st.session_state:
        return st.session_state["auth_user"]

    # app.py styles the page before calling this, but the sign-in screen
    # must not depend on that ordering — a duplicate <style> block is
    # harmless, an unstyled login page is not.
    from dashboard import theme
    theme.apply()

    client = _client()
    if client is None:
        st.error(
            "Supabase is not configured. Set SUPABASE_URL and "
            "SUPABASE_ANON_KEY (see dashboard/auth.py) and restart."
        )
        st.stop()

    # A narrow centred column: the sign-in screen is the only page in the
    # app with one job, and a full-width form on a 1500px canvas reads as
    # a broken layout rather than a deliberate one.
    _, middle, _ = st.columns([1, 1.1, 1])
    with middle:
        st.markdown(
            '<div class="rm-title" style="margin-top:3rem">'
            'Predictive Resource Monitor</div>'
            '<div class="rm-subtitle">Sign in to continue. Which tabs open is '
            'decided by your account, not by the tab you use here.</div>',
            unsafe_allow_html=True,
        )
        tab_customer, tab_admin = st.tabs(["Customer", "Administrator"])
        with tab_customer:
            _login_form(client, "customer")
        with tab_admin:
            _login_form(client, "admin")

    st.stop()


def logout():
    client = _client()
    if client is not None:
        try:
            client.auth.sign_out()
        except Exception:                                     # noqa: BLE001
            pass
    st.session_state.pop("auth_user", None)
    st.rerun()
