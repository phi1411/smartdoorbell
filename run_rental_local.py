"""Run the dashboard, event worker and face model in one process."""

import argparse
import os
import secrets
import sys
from pathlib import Path
from threading import Event, Thread

from waitress import create_server

from face_service.face_api import MAX_BYTES, create_app as create_face_app
from face_service.faceid import face_core


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render", action="store_true", help="Serve HTTPS traffic proxied by Render")
    parser.add_argument("--single-house", action="store_true", help="Scope portal to Dãy A only")
    args = parser.parse_args()
    host, port = "127.0.0.1", 5050
    if args.render:
        host, port = "0.0.0.0", int(os.environ["PORT"])
        data_dir = Path("/var/data")
        if not data_dir.is_dir():
            raise RuntimeError("Attach a persistent disk at /var/data before starting the rental service.")
        os.environ["RENTAL_DB_PATH"] = str(data_dir / "rental.db")
        os.environ["RENTAL_COOKIE_SECURE"] = "1"
        os.environ.setdefault("RENTAL_SINGLE_HOUSE", "1")

    backend = Path(__file__).resolve().parent / "backend"
    sys.path.insert(0, str(backend))
    from rental.__main__ import run_worker
    from rental.app import create_app, open_store
    from rental.live_transport import serve_web
    from rental.worker import VisitWorker

    token = secrets.token_urlsafe(32)
    face = create_server(create_face_app(token), host="127.0.0.1", port=0, threads=1,
                         max_request_body_size=MAX_BYTES)
    os.environ["RENTAL_FACE_API_KEY"] = token
    os.environ["RENTAL_FACE_EMBED_URL"] = f"http://127.0.0.1:{face.effective_port}/internal/embeddings"
    os.environ["RENTAL_FACE_PAD_URL"] = f"http://127.0.0.1:{face.effective_port}/internal/visit-analysis"
    database = open_store()
    web = create_app(database, single_house=True if args.single_house else None)
    worker = VisitWorker(database)
    face_core.warm()
    stop = Event()
    Thread(target=face.run, name="face-api", daemon=True).start()
    Thread(target=run_worker, args=(worker, stop, web), name="rental-worker", daemon=True).start()
    print(f"Rental service listening on {host}:{port}", flush=True)
    try:
        serve_web(web, host, port)
    finally:
        stop.set()
        face.close()


if __name__ == "__main__":
    main()
