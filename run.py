"""Start DocFlow AI locally:  python run.py  ->  http://127.0.0.1:5000"""
import os

from docflow import create_app

app = create_app()

if __name__ == "__main__":
    # Debug stays off unless explicitly enabled - the debugger must never be exposed.
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", 5000)),
            debug=os.getenv("FLASK_DEBUG") == "1")
