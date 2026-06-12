from flask import Flask, request
from threading import Thread
import os
import requests
from urllib.parse import quote

app = Flask("")

AUTH_URL = "https://openapi.ctrader.com/apps/auth"
TOKEN_URL = "https://openapi.ctrader.com/apps/token"


@app.route("/")
def home():
    return "BTC Signal Bot is running"


@app.route("/auth")
def auth():
    cid = os.environ.get("CTRADER_CLIENT_ID", "")
    cb = os.environ.get("APP_URL", "https://btc-signal-bot-production-3d02.up.railway.app") + "/callback"
    url = AUTH_URL + "?client_id=" + cid + "&redirect_uri=" + cb + "&response_type=code&scope=trading"
    return "<h2>cTrader Auth</h2><a href='" + url + "'>Click here to authorize</a>"


@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        return "No code received", 400
    cid = os.environ.get("CTRADER_CLIENT_ID", "")
    csec = os.environ.get("CTRADER_CLIENT_SECRET", "")
    cb = os.environ.get("APP_URL", "https://btc-signal-bot-production-3d02.up.railway.app") + "/callback"
    print("DEBUG cid=" + repr(cid) + " csec_len=" + str(len(csec)) + " cb=" + cb, flush=True)
    try:
        body = (
            "grant_type=authorization_code"
            "&code=" + quote(code, safe="") +
            "&redirect_uri=" + quote(cb, safe="") +
            "&client_id=" + quote(cid, safe="") +
            "&client_secret=" + quote(csec, safe="")
        )
        r = requests.post(
            TOKEN_URL,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        data = r.json()
        access = data.get("accessToken") or data.get("access_token")
        refresh = data.get("refreshToken") or data.get("refresh_token")
        if access:
            print("FULL ACCESS TOKEN: " + access, flush=True)
            print("FULL REFRESH TOKEN: " + str(refresh), flush=True)
            return "<h2>Authorised!</h2><p>Copy tokens from Railway Deploy Logs.</p>"
        return "Token exchange failed: " + str(data), 400
    except Exception as e:
        return "Error: " + str(e), 500


def run():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))


def keep_alive():
    t = Thread(target=run)
    t.daemon = True
    t.start()
