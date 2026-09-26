"""Start the SQL Assistant:  python run.py   (then open http://localhost:8000)"""
import os

from app.server import create_app

app = create_app()

if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 8000)),
            debug=os.environ.get("FLASK_DEBUG") == "1", threaded=True)
