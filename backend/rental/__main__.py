import argparse
import logging
import os
import sqlite3
from threading import Event, Thread

from rental.app import DEVICE_ID, create_app, open_store
from rental.daily_report import start_daily_reports
from rental.telegram_controls import start_telegram_controls
from rental.worker import VisitWorker


def run_worker(worker, stop, app=None):
    previews = app.extensions['rental_live'][1] if app else None
    house_id = app.config.get('RENTAL_HOUSE_A_ID') if app else None
    start_telegram_controls(worker.store, worker.bot_token, stop, previews, house_id)
    start_daily_reports(worker.store, worker.bot_token, stop, house_id=house_id)
    description_worker = VisitWorker(worker.store)
    Thread(target=run_visit_jobs, args=(description_worker.run_description, stop),
           name="rental-description-worker", daemon=True).start()
    run_visit_jobs(worker.run_one, stop)


def run_visit_jobs(process, stop):
    while not stop.is_set():
        try:
            if process():
                continue
        except sqlite3.OperationalError:
            logging.warning("Rental database temporarily unavailable")
        stop.wait(1)


def main():
    parser = argparse.ArgumentParser(prog="python -m rental")
    commands = parser.add_subparsers(dest="command", required=True)
    web = commands.add_parser("web", help="Run the manager dashboard and device API")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=None)
    web.add_argument("--with-worker", action="store_true", help="Handle saved events on the same server")
    web.add_argument("--single-house", action="store_true", help="Scope portal to Dãy A only (default off)")
    commands.add_parser("worker", help="Process saved doorbell events")
    device = commands.add_parser("device", help="Create a device key for the shared entrance of a rental property")
    device.add_argument("--house", type=int, help="Rental property ID (defaults to Dãy A)")
    device.add_argument("device_id")
    args = parser.parse_args()
    store = open_store()

    if args.command == "web":
        from rental.live_transport import serve_web

        port = args.port if args.port is not None else int(os.getenv("PORT", "5050"))
        app = create_app(store, single_house=True if args.single_house else None)
        stop = Event()
        if args.with_worker:
            worker = VisitWorker(store)
            Thread(target=run_worker, args=(worker, stop, app), name="rental-worker", daemon=True).start()
        try:
            serve_web(app, args.host, port)
        finally:
            stop.set()
    elif args.command == "worker":
        logging.basicConfig(level=logging.INFO)
        run_worker(VisitWorker(store), Event())
    else:
        if not DEVICE_ID.fullmatch(args.device_id):
            parser.error("Invalid device ID (use 1–48 letters, digits, underscores or hyphens)")
        try:
            token = store.register_device(args.house if args.house is not None else store.ensure_house_a(), args.device_id)
        except sqlite3.IntegrityError:
            parser.error("Rental property does not exist or the device ID is already registered")
        print(f"Device ID: {args.device_id}\nDevice token (displayed once): {token}")


if __name__ == "__main__":
    main()
