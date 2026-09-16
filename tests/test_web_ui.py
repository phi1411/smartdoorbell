from dataclasses import replace
from pathlib import Path
import pytest
from smart_doorbell.app_factory import create_app
from smart_doorbell.config import AppConfig


@pytest.fixture
def web_client(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    base_config = AppConfig.from_env(base_dir=repo_root)
    config = replace(
        base_config,
        database_path=tmp_path / "test.db",
        event_image_dir=tmp_path / "events",
        face_data_dir=tmp_path / "faces",
    )
    app = create_app(config, start_services=False)
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def test_dashboard_page(web_client):
    res = web_client.get("/")
    assert res.status_code == 200
    assert "text/html" in res.content_type
    html = res.get_data(as_text=True)

    # Check brand and navigation
    assert "SMART DOORBELL" in html
    assert "Dashboard" in html
    assert "Khuôn mặt" in html
    assert "Lịch sử" in html
    assert "Cài đặt" in html

    # Check key dashboard element IDs
    assert 'id="cameraBadge"' in html
    assert 'id="cameraStream"' in html
    assert 'id="currentIdentity"' in html
    assert 'id="livenessStatus"' in html
    assert 'id="confidenceScore"' in html
    assert 'id="cameraStatus"' in html
    assert 'id="recognitionStatus"' in html
    assert 'id="telegramStatus"' in html
    assert 'id="antiStatus"' in html
    assert 'id="unknownWarningToggle"' in html
    assert 'id="unknownWarningHint"' in html
    assert 'id="registeredFacesCount"' in html
    assert 'id="unknownProgressBar"' in html
    assert 'id="bell"' in html
    assert 'id="events"' in html

    # Check shared accessible dialogs & toasts
    assert 'id="confirmModal"' in html
    assert 'id="lightboxModal"' in html
    assert 'id="toast"' in html
    assert 'id="topClock"' in html


def test_faces_page(web_client):
    res = web_client.get("/faces")
    assert res.status_code == 200
    assert "text/html" in res.content_type
    html = res.get_data(as_text=True)

    # Check faces elements
    assert 'id="enrollForm"' in html
    assert 'id="personName"' in html
    assert 'id="radarSweep"' in html
    assert 'id="scanningAlert"' in html
    assert 'id="enrollSubmitBtn"' in html
    assert 'id="faceList"' in html
    assert 'id="faceCount"' in html
    assert 'id="totalRegisteredBadge"' in html


def test_history_page(web_client):
    res = web_client.get("/history")
    assert res.status_code == 200
    assert "text/html" in res.content_type
    html = res.get_data(as_text=True)

    # Check history elements
    assert 'id="filters"' in html
    assert 'id="filterType"' in html
    assert 'id="filterDate"' in html
    assert 'id="filterSubmitBtn"' in html
    assert 'id="filterResetBtn"' in html
    assert 'id="exportLink"' in html
    assert 'id="historyRows"' in html
    assert 'id="historyCards"' in html


def test_settings_page(web_client):
    res = web_client.get("/settings")
    assert res.status_code == 200
    assert "text/html" in res.content_type
    html = res.get_data(as_text=True)

    # Check settings elements
    assert 'id="settingsForm"' in html
    assert 'name="doorbell_cooldown_seconds"' in html
    assert 'name="unknown_alert_seconds"' in html
    assert 'name="unknown_track_lost_tolerance_seconds"' in html
    assert 'name="unknown_warning_enabled"' in html
    assert 'name="anti_spoof_enabled"' in html
    assert 'name="face_match_threshold"' in html
    assert 'name="face_min_size"' in html
    assert 'name="recognition_every_n_frames"' in html
    assert 'name="telegram_enabled"' in html
    assert 'name="save_event_images"' in html
    assert 'id="dirtyIndicator"' in html
    assert 'id="saveSettingsBtn"' in html
    assert 'id="cancelSettingsBtn"' in html


def test_static_assets(web_client):
    assets = [
        "/static/css/app.css",
        "/static/js/common.js",
        "/static/js/dashboard.js",
        "/static/js/faces.js",
        "/static/js/history.js",
        "/static/js/settings.js",
    ]
    for path in assets:
        res = web_client.get(path)
        assert res.status_code == 200, f"Failed to load {path}"
        assert len(res.data) > 0, f"Empty file: {path}"


def test_ui_uses_runtime_cooldown_and_correct_storage_path(web_client):
    dashboard_js = web_client.get("/static/js/dashboard.js").get_data(as_text=True)
    settings_html = web_client.get("/settings").get_data(as_text=True)
    assert "startDoorbellCooldown(res.cooldown_seconds || doorbellCooldownSeconds)" in dashboard_js
    assert "Math.round(r.confidence * 100)" in dashboard_js
    assert "lastEventsHash = '';" in dashboard_js
    assert "unknown_warning_enabled: requested" in dashboard_js
    assert "Người chưa xác định lưu lại thời gian dài" in dashboard_js
    assert "data/events" in settings_html
    assert "data/event_images" not in settings_html


def test_json_keeps_vietnamese_characters(web_client):
    services = web_client.application.extensions["doorbell"]
    services["persons"].create_with_samples("Nguyễn Thị Ánh", [(b"sample", None)])
    response = web_client.get("/api/faces")
    assert response.status_code == 200
    assert "Nguyễn Thị Ánh" in response.get_data(as_text=True)


def test_dashboard_toggle_disables_and_resets_unknown_tracking(web_client):
    services = web_client.application.extensions["doorbell"]
    manager = services["event_manager"]
    manager._unknown_started_at = 10.0
    manager._unknown_last_seen_at = 11.0

    response = web_client.put(
        "/api/settings", json={"unknown_warning_enabled": False}
    )

    assert response.status_code == 200
    assert response.get_json()["unknown_warning_enabled"] is False
    assert manager._unknown_started_at is None
    assert manager.unknown_state()["state"] == "DISABLED"
