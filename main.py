"""
main.py — entry point for Replit
Starts the keep-alive web server, then runs the signal bot.
"""
from keep_alive import keep_alive
import btc_signal_bot

keep_alive()          # start Flask pinger thread
btc_signal_bot.run()  # start the main bot loop (blocking)
