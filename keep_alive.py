from flask import Flask, request
from threading import Thread
import os
import requests

app = Flask("")

@app.route("/")
def home():
    return "BTC Signal Bot is running"

@app.route("/auth")
def auth():
    client_id = os.environ.get("CTRADER_CLIENT_ID", "")
    redirect_uri = "https://btc-signal-bot-production-3d02.up.railway.app/callback"
    url = (
        "https://connect.spotware.com/apps/auth"
        "?client_id=" + client_id +
        "&redirect_uri=" + redirect_uri +
        "&response_type=code&scope=trading"
    )
    return "<h2>cTrader Auth</h2><a href='" + url + "'>Click here to authorize</a>"

@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        return "No code received", 400
    client_id = os.environ.get("CTRADER_CLIENT_ID", "")
    client_secret = os.environ.get("CTRADER_CLIENT_SECRET", "")
    redirect_uri = "https:
