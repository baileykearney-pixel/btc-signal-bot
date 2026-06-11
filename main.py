from flask import Flask, request
from threading import Thread
import os
import requests

app = Flask("")

@app.route("/")
def home():
    return "BTC Signal Bot is running ✅"

@app.route("/auth")
def auth():
    client_id = os.environ.get("CTRADER_CLIENT_ID", "")
    redirect_uri = "https://btc-signal-bot-production-3d02.up.railway.app/callback"
    url = (f"https://connect.spotware.com/apps/auth"
           f"?client_id={client_id}"
           f"&redirect_uri={redirect_uri}"
           f"&response_type=code&scope=trading")
    return f'<h2>cTrader Auth</h2><a href="{url}">Click here to authorize</a>'

@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        return "No code received", 400
    client_id = os.environ.get("CTRADER_CLIENT_ID", "")
    client_secret = os.environ.get("CTRADER_CLIENT_SECRET", "")
    redirect_uri = "https://btc-signal-bot-production-3d02.up.railway.app/callback"
    try:
        r = requests.post(
            "https://connect.spotware.com/apps/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=15,
        )
        data = r.json()
        access = data.get("accessToken") or data.get("access_token")
        refresh = data.get("refreshToken") or data.get("refresh_token")
        if access:
            return (f"<h2>✅ Authorised!</h2>"
                    f"<p>Copy t
