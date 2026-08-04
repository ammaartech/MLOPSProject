import os
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()

url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_ANON_KEY")
supabase = create_client(url, key)

def check_role(email, password, wanted_role):
    try:
        res = supabase.auth.sign_in_with_password({"email": email, "password": password})
        user = res.user
        resp = supabase.table("profiles").select("role").eq("id", user.id).single().execute()
        role = (resp.data or {}).get("role")
        print(f"[{email}] Logged in. Role found in DB: '{role}'. Expected: '{wanted_role}'")
    except Exception as e:
        print(f"[{email}] Error: {e}")

check_role("customer@example.com", "CustomerDemo#2026", "customer")
check_role("admin@example.com", "AdminDemo#2026", "admin")
