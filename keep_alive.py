"""
keep_alive.py — runs a tiny Flask web server so Replit/UptimeRobot
can ping the bot and keep it alive 24/7 on the free tier.
"""
from flask import Flask
from threading import Thread

app = Flask("")

@app.route("/")
def home():
    return "BTC Signal Bot is running ✅"

def run():
    app.run(host="0.0.0.0", port=8080)

def keep_alive():
    t = Thread(target=run)
    t.daemon = True
    t.start()
