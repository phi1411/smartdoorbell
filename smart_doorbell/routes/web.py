from __future__ import annotations

from flask import Blueprint, render_template

web = Blueprint("web", __name__)


@web.get("/")
def dashboard():
    return render_template("dashboard.html")


@web.get("/faces")
def faces():
    return render_template("faces.html")


@web.get("/history")
def history():
    return render_template("history.html")


@web.get("/settings")
def settings():
    return render_template("settings.html")

