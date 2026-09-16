from __future__ import annotations

import csv
import io
from pathlib import Path

from flask import Blueprint, Response, current_app, jsonify, request, send_file

from smart_doorbell.services.enrollment_service import EnrollmentError

api = Blueprint("api", __name__, url_prefix="/api")


def _services() -> dict:
    return current_app.extensions["doorbell"]


@api.get("/status")
def status():
    svc = _services()
    recognition = svc["recognition"].current()
    return jsonify({
        "camera": {"connected": svc["camera"].connected, "error": svc["camera"].last_error},
        "recognition": {
            "running": svc["recognizer"].available,
            "trained": svc["recognizer"].trained,
            "current": recognition,
        },
        "anti_spoof": {"enabled": bool(svc["settings"].get("anti_spoof_enabled", True))},
        "telegram": svc["telegram"].state(),
        "registered_faces": svc["persons"].count(),
        "unknown": svc["event_manager"].unknown_state(),
    })


@api.post("/doorbell")
def doorbell():
    svc = _services()
    frame, _ = svc["camera"].get_latest_frame()
    recognition = svc["recognition"].analyze(frame) if frame is not None else svc["recognition"].current()
    result = svc["event_manager"].doorbell(frame, recognition)
    return jsonify(result), (202 if result["accepted"] else 429 if result["reason"] == "cooldown" else 503)


@api.get("/faces")
def list_faces():
    return jsonify({"items": _services()["persons"].list()})


@api.post("/faces/enroll")
def enroll_face():
    payload = request.get_json(silent=True) or request.form
    try:
        result = _services()["enrollment"].enroll(
            str(payload.get("name", "")), int(payload.get("sample_count", 5))
        )
        return jsonify(result), 201
    except EnrollmentError as exc:
        return jsonify({"error": str(exc)}), 422


@api.post("/faces/<int:person_id>/enroll")
def reenroll_face(person_id: int):
    payload = request.get_json(silent=True) or request.form
    try:
        result = _services()["enrollment"].enroll(
            "", int(payload.get("sample_count", 5)), person_id=person_id
        )
        return jsonify(result)
    except LookupError as exc:
        return jsonify({"error": str(exc)}), 404
    except EnrollmentError as exc:
        return jsonify({"error": str(exc)}), 422


@api.delete("/faces/<int:person_id>")
def delete_face(person_id: int):
    svc = _services()
    try:
        paths = svc["persons"].delete(person_id)
    except LookupError as exc:
        return jsonify({"error": str(exc)}), 404
    face_root = svc["config"].face_data_dir.resolve()
    for raw_path in paths:
        path = Path(raw_path).resolve()
        if path.is_relative_to(face_root):
            path.unlink(missing_ok=True)
            try:
                path.parent.rmdir()
            except OSError:
                pass
    svc["recognizer"].reload()
    return Response(status=204)


@api.get("/events")
def list_events():
    svc = _services()
    rows = svc["events"].list(
        limit=request.args.get("limit", 50, type=int),
        event_type=request.args.get("type"),
        date=request.args.get("date"),
    )
    if request.args.get("format") == "csv":
        output = io.StringIO()
        fields = list(rows[0].keys()) if rows else ["id", "event_type", "created_at"]
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
        return Response(
            output.getvalue(), mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=doorbell-events.csv"},
        )
    return jsonify({"items": rows})


@api.get("/events/<int:event_id>")
def get_event(event_id: int):
    event = _services()["events"].get(event_id)
    return (jsonify(event), 200) if event else (jsonify({"error": "Event not found"}), 404)


@api.get("/events/<int:event_id>/image")
def event_image(event_id: int):
    event = _services()["events"].get(event_id)
    if not event or not event.get("image_path"):
        return jsonify({"error": "Image not found"}), 404
    path = Path(event["image_path"])
    root = _services()["config"].event_image_dir.resolve()
    if not path.resolve().is_relative_to(root) or not path.is_file():
        return jsonify({"error": "Image not found"}), 404
    return send_file(path, mimetype="image/jpeg", conditional=True)


@api.get("/settings")
def get_settings():
    return jsonify(_services()["settings"].all())


@api.put("/settings")
def update_settings():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "A JSON object is required"}), 400
    try:
        svc = _services()
        updated = svc["settings"].update(payload)
        if payload.get("unknown_warning_enabled") in (False, "false"):
            svc["event_manager"].reset_unknown_tracking()
        return jsonify(updated)
    except (ValueError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 422


@api.get("/camera/stream")
def camera_stream():
    svc = _services()
    return Response(
        svc["camera"].mjpeg_stream(svc["recognition"].annotate),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@api.get("/camera/frame")
def camera_frame():
    svc = _services()
    frame, _ = svc["camera"].get_latest_frame()
    if frame is None:
        return jsonify({"error": "Camera unavailable"}), 503
    frame = svc["recognition"].annotate(frame)
    return Response(svc["images"].jpeg_bytes(frame), mimetype="image/jpeg")
