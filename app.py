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

# ─── App Config ────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "topjeans-secret-2024-change-me")
database_url = os.environ.get("DATABASE_URL", "app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///topjeans.db")")
if database_url.startswith("postgres://"): database_url = database_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"] = "uploads"
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"

# ─── Google API Config ─────────────────────────────────────────────────────────
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
INVENTORY_SHEET_ID  = os.getenv("INVENTORY_SHEET_ID",  "1Vzcs4mUnOKk0VwkouBq3m87fdO3xAV7F9na88PVsS0o")
SALES_SHEET_ID      = os.getenv("SALES_SHEET_ID",      "11iPX3SHK-vt6DlN1rJeUXjXsFeHwSFXXCPNFc-AM4kA")
DRIVE_FOLDER_ID     = os.getenv("DRIVE_FOLDER_ID",     "")  # ใส่ ID โฟลเดอร์ Drive สำหรับเก็บสลิป
SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE", "credentials.json")

def get_google_services():
    """Build Google Sheets + Drive clients from service account."""
    try:
        creds = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        sheets = build("sheets", "v4", credentials=creds)
        drive  = build("drive",  "v3", credentials=creds)
        return sheets, drive
    except Exception as e:
        print(f"[Google API] {e}")
        return None, None

# ─── DB Models ─────────────────────────────────────────────────────────────────
class User(UserMixin, db.Model):
    id       = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    role     = db.Column(db.String(20), default="staff")   # admin / staff
    name     = db.Column(db.String(100), default="")

class SaleLog(db.Model):
    id           = db.Column(db.Integer, primary_key=True)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)
    staff_id     = db.Column(db.Integer, db.ForeignKey("user.id"))
    sku          = db.Column(db.String(30))
    product_name = db.Column(db.String(200))
    customer     = db.Column(db.String(100))
    phone        = db.Column(db.String(30))
    price        = db.Column(db.Float)
    cost         = db.Column(db.Float)
    channel      = db.Column(db.String(50))
    slip_url     = db.Column(db.String(500))
    slip_drive_id = db.Column(db.String(200))
    notes        = db.Column(db.Text)
    sheet_row    = db.Column(db.Integer)
    staff        = db.relationship("User", backref="sales")

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

# ─── Google Sheets Helpers ─────────────────────────────────────────────────────
def sheets_get(service, sheet_id, range_):
    try:
        res = service.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=range_).execute()
        return res.get("values", [])
    except Exception as e:
        print(f"[Sheets GET] {e}")
        return []

def sheets_append(service, sheet_id, range_, values):
    try:
        service.spreadsheets().values().append(
            spreadsheetId=sheet_id, range=range_,
            valueInputOption="USER_ENTERED",
            body={"values": values}).execute()
        return True
    except Exception as e:
        print(f"[Sheets APPEND] {e}")
        return False

def sheets_update(service, sheet_id, range_, values):
    try:
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id, range=range_,
            valueInputOption="USER_ENTERED",
            body={"values": values}).execute()
        return True
    except Exception as e:
        print(f"[Sheets UPDATE] {e}")
        return False

def find_sku_row(service, sku):
    """Return (row_index_1based, row_data) or (None, None)."""
    rows = sheets_get(service, INVENTORY_SHEET_ID, "Inventory!A:AK")
    for i, row in enumerate(rows):
        if row and row[0] == sku:
            return i + 1, row
    return None, None

def deduct_stock(service, sku):
    """Mark SKU as Sold Out in Inventory sheet."""
    row_num, row_data = find_sku_row(service, sku)
    if row_num is None:
        return False
    sheets_update(service, INVENTORY_SHEET_ID,
                  f"Inventory!B{row_num}", [["Sold Out"]])
    return True

def upload_slip_to_drive(drive_service, file_bytes, filename, mime_type):
    """Upload slip image to Google Drive and return (file_id, web_link)."""
    try:
        meta = {"name": filename}
        if DRIVE_FOLDER_ID:
            meta["parents"] = [DRIVE_FOLDER_ID]
        media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=mime_type)
        f = drive_service.files().create(
            body=meta, media_body=media, fields="id,webViewLink").execute()
        drive_service.permissions().create(
            fileId=f["id"],
            body={"type": "anyone", "role": "reader"}).execute()
        return f["id"], f.get("webViewLink", "")
    except Exception as e:
        print(f"[Drive Upload] {e}")
        return None, None

# ─── Routes: Auth ──────────────────────────────────────────────────────────────
@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        u = User.query.filter_by(username=request.form["username"]).first()
        if u and check_password_hash(u.password, request.form["password"]):
            login_user(u)
            return redirect(url_for("dashboard"))
        flash("ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง", "error")
    return render_template("login.html")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))

# ─── Routes: Dashboard ─────────────────────────────────────────────────────────
@app.route("/")
@login_required
def dashboard():
    sheets, drive = get_google_services()
    inv_rows, sales_rows = [], []
    if sheets:
        inv_rows   = sheets_get(sheets, INVENTORY_SHEET_ID,  "Inventory!A2:AK")
        sales_rows = sheets_get(sheets, SALES_SHEET_ID, "รายชื่อลูกค้า!A2:AZ")

    # KPI
    total  = sum(1 for r in inv_rows if len(r) > 1 and r[1])
    instock = sum(1 for r in inv_rows if len(r) > 1 and r[1] == "in Stock")
    sold   = sum(1 for r in inv_rows if len(r) > 1 and r[1] == "Sold Out")

    cost_total = 0
    for r in inv_rows:
        if len(r) > 15 and r[15]:
            try: cost_total += float(str(r[15]).replace(",",""))
            except: pass

    sales_total = 0
    for r in sales_rows:
        if len(r) > 13 and r[13]:
            try: sales_total += float(str(r[13]).replace(",",""))
            except: pass

    recent_sales = SaleLog.query.order_by(SaleLog.created_at.desc()).limit(10).all()

    # Brand distribution
    brand_count = {}
    for r in inv_rows:
        if len(r) > 2 and r[1] == "in Stock":
            b = r[2] if r[2] else "Other"
            brand_count[b] = brand_count.get(b, 0) + 1

    return render_template("dashboard.html",
        total=total, instock=instock, sold=sold,
        cost_total=cost_total, sales_total=sales_total,
        recent_sales=recent_sales, brand_count=brand_count,
        inv_rows=inv_rows[:50], google_ok=sheets is not None)

# ─── Routes: Inventory ─────────────────────────────────────────────────────────
@app.route("/inventory")
@login_required
def inventory():
    sheets, _ = get_google_services()
    rows = []
    if sheets:
        rows = sheets_get(sheets, INVENTORY_SHEET_ID, "Inventory!A2:AK")
    q      = request.args.get("q", "").lower()
    status = request.args.get("status", "")
    brand  = request.args.get("brand", "")
    if q:
        rows = [r for r in rows if any(q in str(c).lower() for c in r)]
    if status:
        rows = [r for r in rows if len(r)>1 and r[1]==status]
    if brand:
        rows = [r for r in rows if len(r)>2 and r[2]==brand]
    brands = sorted(set(r[2] for r in rows if len(r)>2 and r[2]))
    return render_template("inventory.html", rows=rows, brands=brands,
                           q=q, status=status, brand=brand,
                           google_ok=sheets is not None)

# ─── Routes: Sales ─────────────────────────────────────────────────────────────
@app.route("/sales")
@login_required
def sales():
    sheets, _ = get_google_services()
    rows = []
    if sheets:
        rows = sheets_get(sheets, SALES_SHEET_ID, "รายชื่อลูกค้า!A2:AZ")
    local_sales = SaleLog.query.order_by(SaleLog.created_at.desc()).all()
    return render_template("sales.html", rows=rows, local_sales=local_sales,
                           google_ok=sheets is not None)

# ─── Routes: New Sale ──────────────────────────────────────────────────────────
@app.route("/new-sale", methods=["GET", "POST"])
@login_required
def new_sale():
    sheets, drive = get_google_services()
    inventory_items = []
    if sheets:
        raw = sheets_get(sheets, INVENTORY_SHEET_ID, "Inventory!A2:AK")
        inventory_items = [r for r in raw if len(r)>1 and r[1]=="in Stock"]

    if request.method == "POST":
        sku          = request.form.get("sku", "").strip()
        product_name = request.form.get("product_name", "")
        customer     = request.form.get("customer", "")
        phone        = request.form.get("phone", "")
        price        = float(request.form.get("price", 0) or 0)
        cost         = float(request.form.get("cost", 0) or 0)
        channel      = request.form.get("channel", "")
        notes        = request.form.get("notes", "")
        slip_url     = ""
        slip_drive_id = ""

        # Handle slip upload
        slip_file = request.files.get("slip")
        if slip_file and slip_file.filename:
            file_bytes = slip_file.read()
            mime_type  = slip_file.mimetype or "image/jpeg"
            safe_name  = f"slip_{sku}_{datetime.now().strftime('%Y%m%d%H%M%S')}{os.path.splitext(slip_file.filename)[1]}"
            if drive:
                slip_drive_id, slip_url = upload_slip_to_drive(
                    drive, file_bytes, safe_name, mime_type)
            # Also save locally as fallback
            local_path = os.path.join(app.config["UPLOAD_FOLDER"], safe_name)
            with open(local_path, "wb") as f:
                f.write(file_bytes)
            if not slip_url:
                slip_url = url_for("uploaded_file", filename=safe_name, _external=True)

        # 1. Deduct stock in Inventory Sheet
        stock_ok = False
        if sheets and sku:
            stock_ok = deduct_stock(sheets, sku)

        # 2. Append to Sales Sheet
        now_str = datetime.now().strftime("%d/%m/%Y")
        sale_row = [
            now_str, current_user.name or current_user.username,
            channel, customer, phone, "", "", product_name,
            sku, slip_url, "1", str(int(price)), "",
            str(int(price)), now_str, slip_url, "โอน/COD",
            "", "", "", "", now_str, "", str(int(cost)),
            str(int(price - cost)), "", "ส่งแล้ว", "", "", channel
        ]
        sheets_ok = False
        if sheets:
            sheets_ok = sheets_append(sheets, SALES_SHEET_ID,
                                       "รายชื่อลูกค้า!A:A", [sale_row])

        # 3. Save to local DB
        log = SaleLog(
            staff_id=current_user.id, sku=sku,
            product_name=product_name, customer=customer,
            phone=phone, price=price, cost=cost, channel=channel,
            slip_url=slip_url, slip_drive_id=slip_drive_id, notes=notes)
        db.session.add(log)
        db.session.commit()

        msg = f"บันทึกการขาย {sku} เรียบร้อย"
        if stock_ok: msg += " · ตัดสต๊อกแล้ว"
        if sheets_ok: msg += " · บันทึกใน Google Sheets แล้ว"
        flash(msg, "success")
        return redirect(url_for("sales"))

    return render_template("new_sale.html", inventory_items=inventory_items,
                           google_ok=sheets is not None)

# ─── Routes: API ──────────────────────────────────────────────────────────────
@app.route("/api/sku-info/<sku>")
@login_required
def sku_info(sku):
    sheets, _ = get_google_services()
    if not sheets:
        return jsonify({"error": "Google API ไม่พร้อมใช้งาน"})
    _, row = find_sku_row(sheets, sku)
    if not row:
        return jsonify({"error": "ไม่พบ SKU"})
    headers = ["SKU","สถานะ","Brand","รุ่น","Tab","Color","Made in",
               "Botton Code","ผลิต-ปี","ทรงกางเกง","เนื้อผ้า","ขนาด",
               "ตำหนิ","สภาพ","วันที่รับ","ต้นทุน/ชิ้น","ทุนรวม","กำไรรวม",
               "Instock","ขาย/ชิ้น","สต๊อกคงเหลือ","ช่องทางขาย","Link-ภาพสินค้า"]
    data = {headers[i]: row[i] if i < len(row) else "" for i in range(len(headers))}
    return jsonify(data)

@app.route("/api/inventory-stats")
@login_required
def inventory_stats():
    sheets, _ = get_google_services()
    if not sheets:
        return jsonify({"error": "no google"})
    rows = sheets_get(sheets, INVENTORY_SHEET_ID, "Inventory!A2:V")
    instock = sum(1 for r in rows if len(r)>1 and r[1]=="in Stock")
    soldout = sum(1 for r in rows if len(r)>1 and r[1]=="Sold Out")
    brands = {}
    for r in rows:
        if len(r)>2 and r[1]=="in Stock":
            b = r[2] or "Other"
            brands[b] = brands.get(b,0)+1
    return jsonify({"instock":instock,"soldout":soldout,"brands":brands})

@app.route("/uploads/<filename>")
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)

# ─── Routes: Admin ────────────────────────────────────────────────────────────
@app.route("/admin/users", methods=["GET", "POST"])
@login_required
def admin_users():
    if current_user.role != "admin":
        flash("เฉพาะ Admin เท่านั้น", "error")
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        action = request.form.get("action")
        if action == "create":
            uname = request.form["username"].strip()
            if not User.query.filter_by(username=uname).first():
                u = User(username=uname,
                         password=generate_password_hash(request.form["password"]),
                         role=request.form.get("role","staff"),
                         name=request.form.get("name",""))
                db.session.add(u)
                db.session.commit()
                flash(f"สร้าง account '{uname}' แล้ว", "success")
            else:
                flash("ชื่อผู้ใช้นี้มีอยู่แล้ว", "error")
        elif action == "delete":
            uid = int(request.form["user_id"])
            if uid != current_user.id:
                db.session.delete(db.session.get(User, uid))
                db.session.commit()
                flash("ลบ account แล้ว", "success")
    users = User.query.all()
    return render_template("admin_users.html", users=users)

# ─── Init ─────────────────────────────────────────────────────────────────────
def init_db():
    with app.app_context():
        db.create_all()
        if not User.query.filter_by(username="admin").first():
            db.session.add(User(
                username="admin",
                password=generate_password_hash("topjeans2024"),
                role="admin", name="Admin"))
            db.session.commit()
            print("✅ Created default admin: admin / topjeans2024")

    init_db()

if __name__ == "__main__":
    app.run(debug=True, port=5000)

