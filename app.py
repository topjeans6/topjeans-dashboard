import os
import json
import base64
import io
from datetime import datetime
from functools import wraps

from flask import (Flask, render_template, request, redirect, url_for,
                   session, flash, jsonify, send_from_directory)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (LoginManager, UserMixin, login_user, logout_user,
                          login_required, current_user)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "topjeans-secret-2024-change-me")
database_url = os.getenv("DATABASE_URL", "sqlite:///topjeans.db")
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"] = "uploads"
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
INVENTORY_SHEET_ID = os.getenv("INVENTORY_SHEET_ID", "1Vzcs4mUnOKk0VwkouBq3m87fdO3xAV7F9na88PVsS0o")
SALES_SHEET_ID = os.getenv("SALES_SHEET_ID", "11iPX3SHK-vt6DlN1rJeUXjXsFeHwSFXXCPNFc-AM4kA")
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID", "")
SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE", "credentials.json")

def get_google_services():
    try:
        creds = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        sheets = build("sheets", "v4", credentials=creds)
        drive = build("drive", "v3", credentials=creds)
        return sheets, drive
    except Exception as e:
        print(f"[Google API] {e}")
        return None, None

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), default="staff")
    name = db.Column(db.String(100), default="")

class SaleLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    staff_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    sku = db.Column(db.String(30))
    product_name = db.Column(db.String(200))
    customer = db.Column(db.String(100))
    phone = db.Column(db.String(30))
    price = db.Column(db.Float)
    cost = db.Column(db.Float)
    channel = db.Column(db.String(50))
    slip_url = db.Column(db.String(500))
    slip_drive_id = d
