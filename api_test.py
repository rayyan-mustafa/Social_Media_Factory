import os
import requests
import smtplib
from dotenv import load_dotenv

load_dotenv()

def test_apify():
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        print("[FAIL] Apify: Missing config")
        return
    url = f"https://api.apify.com/v2/users/me?token={token}"
    try:
        resp = requests.get(url)
        if resp.status_code == 200:
            print("[OK] Apify: Connection Successful (Auth valid)")
        else:
            print(f"[FAIL] Apify: Error {resp.status_code} - {resp.text}")
    except Exception as e:
        print(f"[FAIL] Apify: Exception {e}")

def test_runpod():
    api_key = os.environ.get("RUNPOD_API_KEY")
    if not api_key:
        print("[FAIL] Runpod: Missing config")
        return
    url = "https://api.runpod.io/graphql"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    query = '{"query": "query { myself { id email } }"}'
    try:
        resp = requests.post(url, headers=headers, data=query)
        if resp.status_code == 200:
            print("[OK] Runpod: Connection Successful (Auth valid)")
        else:
            print(f"[FAIL] Runpod: Error {resp.status_code} - {resp.text}")
    except Exception as e:
        print(f"[FAIL] Runpod: Exception {e}")

def test_gmail():
    user = os.environ.get("GMAIL_USER")
    pwd = os.environ.get("GMAIL_APP_PASSWORD")
    if not user or not pwd:
        print("[FAIL] Gmail: Missing config")
        return
    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(user, pwd)
        print("[OK] Gmail: Login Successful")
        server.quit()
    except Exception as e:
        print(f"[FAIL] Gmail: Exception {e}")

if __name__ == "__main__":
    print("Testing APIs from .env...")
    test_apify()
    test_runpod()
    test_gmail()
