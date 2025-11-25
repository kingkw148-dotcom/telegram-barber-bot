# web.py
from threading import Thread
from flask import Flask
import os
import time
import logging

# Import your bot main function (assumes barber_bot.py exposes main())
# Make sure barber_bot.main() does not call app.run_polling() on import,
# but only when main() is called.
import barber_bot

logging.basicConfig(level=logging.INFO)
app = Flask(__name__)

@app.route("/")
def index():
    return "OK - barber bot running"

@app.route("/healthz")
def healthz():
    return "healthy"

def run_bot():
    """Run your bot's main (blocking)."""
    logging.info("Starting bot thread...")
    try:
        barber_bot.main()
    except Exception as e:
        logging.exception("Bot terminated with exception: %s", e)

if __name__ == "__main__":
    # Start the bot in a background thread
    t = Thread(target=run_bot, daemon=True)
    t.start()
    # Start Flask on PORT (Render provides $PORT)
    port = int(os.environ.get("PORT", "5000"))
    logging.info("Starting web server on port %s", port)
    app.run(host="0.0.0.0", port=port)
