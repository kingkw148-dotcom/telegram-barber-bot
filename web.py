# web.py
from threading import Thread
from flask import Flask
import os
import barber_bot  # this imports your main Telegram bot file

app = Flask(__name__)

# Render checks this
@app.route("/")
def home():
    return "Bot is running!"

def run_bot():
    barber_bot.main()  # your main bot function

if __name__ == "__main__":
    # run Telegram bot in background thread
    Thread(target=run_bot).start()

    # run web server for Render
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
