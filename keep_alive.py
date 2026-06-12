from flask import Flask, request
from threading import Thread
import os
import requests

app = Flask("")

RAILWAY_URL = "https://btc-signal-bot-production-3d02.up.railway.app"


@app.route("/")
def home():
    return "BTC Signal Bot is running"


@app.route("/auth")
def auth():
    cid = os.environ.get("CTRADER_CLIENT_ID", "")
    cb = RAILWAY_URL + "/callback"
    base = "https://connect.spotware.com/apps/auth"
    url = base + "?client_id=" + cid + "&redirect_uri=" + cb + "&response_type=code&scope=trading"
    return "<h2>cTrader Auth</h2><a href='" + url + "'>Click here to authorize</a>"


@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        return "No code received", 400
    cid = os.environ.get("CTRADER_CLIENT_ID", "")
    csec = os.environ.get("CTRADER_CLIENT_SECRET", "")
    cb = RAILWAY_URL + "/callback"
    try:
        r = requests.post(
            "https://connect.spotware.com/apps/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": cb,
                "client_id": cid,
                "client_secret": csec,
            },
            timeout=15,
        )
        data = r.json()
        access = data.get("accessToken") or data.get("access_token")
        refresh = data.get("refreshToken") or data.get("refresh_token")
        if access:
            html = "<h2>Authorised!</h2>"
            html += "<p>Add these to Railway Variables:</p>"
            html += "<b>CTRADER_ACCESS_TOKEN:</b><br><code>" + access + "</code><br><br>"
            html += "<b>CTRADER_REFRESH_TOKEN:</b><br><code>" + str(refresh) + "</code>"
            return html
        return "Failed: " + str(data), 400
    except Exception as e:
        return "Error: " + str(e), 500


def run():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))


def keep_alive():
    t = Thread(target=run)
    t.daemon = True
    t.start()
