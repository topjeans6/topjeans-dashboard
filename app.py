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
    slip_drive_id = db.Column(db.String(200))
    notes = db.Column(db.Text)
    sheet_row = db.Column(db.Integer)
    staff = db.relationship("User", backref="sales")

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

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

def get_sales_sheet_tabs(service):
    try:
        meta = service.spreadsheets().get(spreadsheetId=SALES_SHEET_ID).execute()
        tabs = [s["properties"]["title"] for s in meta.get("sheets", [])]
        import calendar
        months = list(calendar.month_name)[1:]
        result = []
        for tab in tabs:
            parts = tab.strip().split(" ")
            if len(parts) == 2 and parts[0] in months and parts[1].isdigit():
                result.append(tab)
        def sort_key(t):
            parts = t.split(" ")
            return (int(parts[1]), months.index(parts[0]))
        result.sort(key=sort_key, reverse=True)
        return result
    except Exception as e:
        print(f"[Sheets TABS] {e}")
        return []

def get_current_month_tab():
    now = datetime.now()
    return now.strftime("%B %Y")

def get_sales_rows(service, tab=None):
    if not tab:
        tab = get_current_month_tab()
    return sheets_get(service, SALES_SHEET_ID, f"{tab}!A2:AZ")

def get_all_sales_rows(service):
    tabs = get_sales_sheet_tabs(service)
    all_rows = []
    for tab in tabs:
        rows = sheets_get(service, SALES_SHEET_ID, f"{tab}!A2:AZ")
        all_rows.extend(rows)
    return all_rows

def get_net_profit(service, tab):
    try:
        res = service.spreadsheets().values().get(
            spreadsheetId=SALES_SHEET_ID,
            range=f"{tab}!AY2").execute()
        val = res.get("values", [[0]])[0][0]
        return float(str(val).replace(",", ""))
    except:
        return 0

def get_all_net_profit(service):
    tabs = get_sales_sheet_tabs(service)
    return sum(get_net_profit(service, tab) for tab in tabs)

def get_sales_total(service, tab):
    rows = get_sales_rows(service, tab)
    total = 0
    for r in rows:
        if len(r) > 12 and r[12]:
            try:
                total += float(str(r[12]).replace(",", ""))
            except:
                pass
    return total

def get_all_sales_total(service):
    tabs = get_sales_sheet_tabs(service)
    return sum(get_sales_total(service, tab) for tab in tabs)

def find_sku_row(service, sku):
    rows = sheets_get(service, INVENTORY_SHEET_ID, "Inventory!A:AK")
    for i, row in enumerate(rows):
        if row and row[0] == sku:
            return i + 1, row
    return None, None

def get_cost_from_inventory(service, sku):
    """ดึงต้นทุน/ชิ้น จาก Inventory คอลัมน์ P (index 15)"""
    row_num, row = find_sku_row(service, sku)
    if row and len(row) > 15 and row[15]:
        try:
            return str(row[15])
        except:
            pass
    return ""

def find_sales_row_by_sku(service, tab, sku):
    """หา row ใน Sales Sheet จาก SKU (คอลัมน์ J = index 9)"""
    rows = sheets_get(service, SALES_SHEET_ID, f"{tab}!A:AZ")
    for i, row in enumerate(rows):
        if len(row) > 9 and row[9] == sku:
            return i + 1, row
    return None, None

def find_sales_rows_by_status(service, status="เตรียมส่ง"):
    """หา rows ที่มีสถานะตามที่ระบุจากทุก tab"""
    tabs = get_sales_sheet_tabs(service)
    results = []
    for tab in tabs:
        rows = sheets_get(service, SALES_SHEET_ID, f"{tab}!A:AZ")
        for i, row in enumerate(rows):
            if i == 0:
                continue
            # AB = index 27 = สถานะการส่ง
            if len(row) > 27 and row[27] == status:
                results.append({"tab": tab, "row_num": i + 1, "row": row})
    return results

def deduct_stock(service, sku):
    row_num, row_data = find_sku_row(service, sku)
    if row_num is None:
        return False
    sheets_update(service, INVENTORY_SHEET_ID,
                  f"Inventory!B{row_num}", [["Sold Out"]])
    return True

def upload_slip_to_drive(drive_service, file_bytes, filename, mime_type):
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

@app.route("/")
@login_required
def dashboard():
    sheets, drive = get_google_services()
    inv_rows = []
    tabs = []
    selected_tab = request.args.get("month", "")
    sales_total = 0
    net_profit = 0

    if sheets:
        inv_rows = sheets_get(sheets, INVENTORY_SHEET_ID, "Inventory!A2:AK")
        tabs = get_sales_sheet_tabs(sheets)
        if selected_tab == "all":
            sales_total = get_all_sales_total(sheets)
            net_profit = get_all_net_profit(sheets)
        elif selected_tab and selected_tab in tabs:
            sales_total = get_sales_total(sheets, selected_tab)
            net_profit = get_net_profit(sheets, selected_tab)
        else:
            if tabs:
                selected_tab = tabs[0]
                sales_total = get_sales_total(sheets, selected_tab)
                net_profit = get_net_profit(sheets, selected_tab)

    total = sum(1 for r in inv_rows if len(r) > 1 and r[1])
    instock = sum(1 for r in inv_rows if len(r) > 1 and r[1] == "in Stock")
    sold = sum(1 for r in inv_rows if len(r) > 1 and r[1] == "Sold Out")
    cost_total = 0
    for r in inv_rows:
        if len(r) > 15 and r[15]:
            try:
                cost_total += float(str(r[15]).replace(",", ""))
            except:
                pass

    recent_sales = SaleLog.query.order_by(SaleLog.created_at.desc()).limit(10).all()
    brand_count = {}
    for r in inv_rows:
        if len(r) > 2 and r[1] == "in Stock":
            b = r[2] if r[2] else "Other"
            brand_count[b] = brand_count.get(b, 0) + 1

    return render_template("dashboard.html",
        total=total, instock=instock, sold=sold,
        cost_total=cost_total, sales_total=sales_total,
        recent_sales=recent_sales, brand_count=brand_count,
        inv_rows=inv_rows[:50], google_ok=sheets is not None,
        tabs=tabs, selected_tab=selected_tab,
        net_profit=net_profit)

@app.route("/inventory")
@login_required
def inventory():
    sheets, _ = get_google_services()
    rows = []
    if sheets:
        rows = sheets_get(sheets, INVENTORY_SHEET_ID, "Inventory!A2:AK")
    q = request.args.get("q", "").lower()
    status = request.args.get("status", "")
    brand = request.args.get("brand", "")
    if q:
        rows = [r for r in rows if any(q in str(c).lower() for c in r)]
    if status:
        rows = [r for r in rows if len(r) > 1 and r[1] == status]
    if brand:
        rows = [r for r in rows if len(r) > 2 and r[2] == brand]
    brands = sorted(set(r[2] for r in rows if len(r) > 2 and r[2]))
    return render_template("inventory.html", rows=rows, brands=brands,
                           q=q, status=status, brand=brand,
                           google_ok=sheets is not None)

@app.route("/sales")
@login_required
def sales():
    sheets, _ = get_google_services()
    rows = []
    tabs = []
    selected_tab = request.args.get("month", "")
    if sheets:
        tabs = get_sales_sheet_tabs(sheets)
        if not selected_tab and tabs:
            selected_tab = tabs[0]
        if selected_tab:
            rows = get_sales_rows(sheets, selected_tab)
    local_sales = SaleLog.query.order_by(SaleLog.created_at.desc()).all()
    return render_template("sales.html", rows=rows, local_sales=local_sales,
                           google_ok=sheets is not None,
                           tabs=tabs, selected_tab=selected_tab)

@app.route("/new-sale", methods=["GET", "POST"])
@login_required
def new_sale():
    sheets, drive = get_google_services()
    inventory_items = []
    if sheets:
        raw = sheets_get(sheets, INVENTORY_SHEET_ID, "Inventory!A2:AK")
        inventory_items = [r for r in raw if len(r) > 1 and r[1] == "in Stock"]

    if request.method == "POST":
        order_date     = request.form.get("order_date", "")
        username       = request.form.get("username", "")
        platform       = request.form.get("platform", "")
        customer       = request.form.get("customer", "")
        phone          = request.form.get("phone", "")
        address        = request.form.get("address", "")
        province       = request.form.get("province", "")
        postcode       = request.form.get("postcode", "")
        product_name   = request.form.get("product_name", "")
        sku            = request.form.get("sku", "").strip()
        link           = request.form.get("link", "")
        quantity       = request.form.get("quantity", "1")
        price_normal   = request.form.get("price_normal", "0")
        price_discount = request.form.get("price_discount", "0")
        price_transfer = request.form.get("price_transfer", "0")
        payment_date   = request.form.get("payment_date", "")
        sale_channel   = request.form.get("sale_channel", "")
        payment_method = request.form.get("payment_method", "โอนชำระ")
        cod_fee        = request.form.get("cod_fee", "0")
        cod_deposit    = request.form.get("cod_deposit", "0")
        cod_date       = request.form.get("cod_date", "")
        cod_amount     = request.form.get("cod_amount", "0")
        shipping_status = request.form.get("shipping_status", "เตรียมส่ง")
        notes          = request.form.get("notes", "")

        slip_url = ""
        slip_drive_id = ""
        product_image_url = ""

        # ดึงต้นทุนจาก Inventory คอลัมน์ P
        cost_per_item = ""
        if sheets and sku:
            cost_per_item = get_cost_from_inventory(sheets, sku)

        # อัพโหลดสลิป
        slip_file = request.files.get("slip")
        if slip_file and slip_file.filename:
            file_bytes = slip_file.read()
            mime_type = slip_file.mimetype or "image/jpeg"
            safe_name = f"slip_{sku}_{datetime.now().strftime('%Y%m%d%H%M%S')}{os.path.splitext(slip_file.filename)[1]}"
            if drive:
                slip_drive_id, slip_url = upload_slip_to_drive(drive, file_bytes, safe_name, mime_type)
            local_path = os.path.join(app.config["UPLOAD_FOLDER"], safe_name)
            with open(local_path, "wb") as f:
                f.write(file_bytes)
            if not slip_url:
                slip_url = url_for("uploaded_file", filename=safe_name, _external=True)

        # อัพโหลดภาพสินค้า
        product_image_file = request.files.get("product_image")
        if product_image_file and product_image_file.filename:
            file_bytes = product_image_file.read()
            mime_type = product_image_file.mimetype or "image/jpeg"
            safe_name = f"product_{sku}_{datetime.now().strftime('%Y%m%d%H%M%S')}{os.path.splitext(product_image_file.filename)[1]}"
            if drive:
                _, product_image_url = upload_slip_to_drive(drive, file_bytes, safe_name, mime_type)
            if not product_image_url:
                local_path = os.path.join(app.config["UPLOAD_FOLDER"], safe_name)
                with open(local_path, "wb") as f:
                    f.write(file_bytes)
                product_image_url = url_for("uploaded_file", filename=safe_name, _external=True)

        # ตัดสต๊อก
        stock_ok = False
        if sheets and sku:
            stock_ok = deduct_stock(sheets, sku)

        # A=วันที่สั่งซื้อ B=Username C=Platform D=ชื่อสกุล E=เบอร์โทร
        # F=ที่อยู่ G=จังหวัด H=รหัสไปรษณีย์ I=สินค้า J=SKU
        # K=Link L=จำนวน M=ราคาปกติ N=ราคาลด O=ยอดโอนเต็ม
        # P=วันที่ชำระ Q=สลิป R=รูปแบบชำระ S=รับค่าบริการCOD
        # T=มัดจำ U=วันที่รับเงินCOD V=จำนวนเงินCOD
        # W=วันที่ส่ง X=ค่าส่ง Y=ต้นทุน/ชิ้น Z=กำไร
        # AA=ว่าง AB=สถานะการส่ง AC=บริษัทขนส่ง AD=เลขพัสดุ
        # AE=ช่องทางที่ขายได้ AF=ภาพสินค้า
        sale_row = [
            order_date, username, platform, customer, phone,
            address, province, postcode, product_name, sku,
            link, quantity, price_normal, price_discount, price_transfer,
            payment_date, slip_url, payment_method, cod_fee,
            cod_deposit, cod_date, cod_amount,
            "", "", cost_per_item, "",
            "", shipping_status, "", "",
            sale_channel, product_image_url
        ]

        sheets_ok = False
        if sheets:
            current_tab = get_current_month_tab()
            sheets_ok = sheets_append(sheets, SALES_SHEET_ID,
                                      f"{current_tab}!A:A", [sale_row])

        try:
            price = float(price_normal)
        except:
            price = 0
        try:
            cost = float(cost_per_item)
        except:
            cost = 0
        log = SaleLog(
            staff_id=current_user.id, sku=sku,
            product_name=product_name, customer=customer,
            phone=phone, price=price, cost=cost, channel=platform,
            slip_url=slip_url, slip_drive_id=slip_drive_id, notes=notes)
        db.session.add(log)
        db.session.commit()

        msg = f"บันทึกการขาย {sku} เรียบร้อย"
        if stock_ok:
            msg += " · ตัดสต๊อกแล้ว"
        if sheets_ok:
            msg += " · บันทึกใน Google Sheets แล้ว"
        flash(msg, "success")
        return redirect(url_for("sales"))

    return render_template("new_sale.html", inventory_items=inventory_items,
                           google_ok=sheets is not None)

# ─── Routes: สถานะการจัดส่ง ───────────────────────────────────────────────────
@app.route("/shipping", methods=["GET", "POST"])
@login_required
def shipping():
    sheets, _ = get_google_services()
    items = []
    if sheets:
        items = find_sales_rows_by_status(sheets, "เตรียมส่ง")

    if request.method == "POST":
        tab        = request.form.get("tab", "")
        row_num    = request.form.get("row_num", "")
        ship_date  = request.form.get("ship_date", "")
        carrier    = request.form.get("carrier", "")
        tracking   = request.form.get("tracking", "")
        ship_cost  = request.form.get("ship_cost", "0")

        if sheets and tab and row_num:
            row_num = int(row_num)
            # W=วันที่ส่ง X=ค่าส่ง AB=สถานะ AC=บริษัทขนส่ง AD=เลขพัสดุ
            sheets_update(sheets, SALES_SHEET_ID,
                          f"{tab}!W{row_num}", [[ship_date]])
            sheets_update(sheets, SALES_SHEET_ID,
                          f"{tab}!X{row_num}", [[ship_cost]])
            sheets_update(sheets, SALES_SHEET_ID,
                          f"{tab}!AB{row_num}", [["ส่งแล้ว"]])
            sheets_update(sheets, SALES_SHEET_ID,
                          f"{tab}!AC{row_num}", [[carrier]])
            sheets_update(sheets, SALES_SHEET_ID,
                          f"{tab}!AD{row_num}", [[tracking]])
            flash(f"อัพเดทสถานะการจัดส่งเรียบร้อย", "success")
            return redirect(url_for("shipping"))

    return render_template("shipping.html", items=items,
                           google_ok=sheets is not None)

# ─── Routes: รับเงิน COD ──────────────────────────────────────────────────────
@app.route("/cod", methods=["GET", "POST"])
@login_required
def cod():
    sheets, _ = get_google_services()
    items = []
    if sheets:
        items = find_sales_rows_by_status(sheets, "ส่งแล้ว")

    if request.method == "POST":
        tab        = request.form.get("tab", "")
        row_num    = request.form.get("row_num", "")
        cod_date   = request.form.get("cod_date", "")
        cod_amount = request.form.get("cod_amount", "0")

        if sheets and tab and row_num:
            row_num = int(row_num)
            # U=วันที่รับเงินCOD V=จำนวนเงินCOD
            sheets_update(sheets, SALES_SHEET_ID,
                          f"{tab}!U{row_num}", [[cod_date]])
            sheets_update(sheets, SALES_SHEET_ID,
                          f"{tab}!V{row_num}", [[cod_amount]])
            flash("บันทึกการรับเงิน COD เรียบร้อย", "success")
            return redirect(url_for("cod"))

    return render_template("cod.html", items=items,
                           google_ok=sheets is not None)
# ─── Routes: ค่าใช้จ่ายในการดำเนินงาน ────────────────────────────────────────
@app.route("/expenses", methods=["GET", "POST"])
@login_required
def expenses():
    sheets, _ = get_google_services()
    items = []
    if sheets:
        items = find_sales_rows_by_status(sheets, "ส่งแล้ว")

    if request.method == "POST":
        tab         = request.form.get("tab", "")
        row_num     = request.form.get("row_num", "")
        bag_size    = request.form.get("bag_size", "")
        bag_qty     = request.form.get("bag_qty", "0")
        wrap_size   = request.form.get("wrap_size", "")
        wrap_qty    = request.form.get("wrap_qty", "0")
        label_size  = request.form.get("label_size", "")
        label_qty   = request.form.get("label_qty", "0")
        sticker_size = request.form.get("sticker_size", "")
        sticker_qty = request.form.get("sticker_qty", "0")
        fuel_cost   = request.form.get("fuel_cost", "0")

        if sheets and tab and row_num:
            row_num = int(row_num)
            sheets_update(sheets, SALES_SHEET_ID, f"{tab}!AJ{row_num}", [[bag_size]])
            sheets_update(sheets, SALES_SHEET_ID, f"{tab}!AL{row_num}", [[bag_qty]])
            sheets_update(sheets, SALES_SHEET_ID, f"{tab}!AM{row_num}", [[wrap_size]])
            sheets_update(sheets, SALES_SHEET_ID, f"{tab}!AO{row_num}", [[wrap_qty]])
            sheets_update(sheets, SALES_SHEET_ID, f"{tab}!AP{row_num}", [[label_size]])
            sheets_update(sheets, SALES_SHEET_ID, f"{tab}!AR{row_num}", [[label_qty]])
            sheets_update(sheets, SALES_SHEET_ID, f"{tab}!AS{row_num}", [[sticker_size]])
            sheets_update(sheets, SALES_SHEET_ID, f"{tab}!AU{row_num}", [[sticker_qty]])
            sheets_update(sheets, SALES_SHEET_ID, f"{tab}!AV{row_num}", [[fuel_cost]])
            flash("บันทึกค่าใช้จ่ายเรียบร้อย", "success")
            return redirect(url_for("expenses"))

    return render_template("expenses.html", items=items,
                           google_ok=sheets is not None)
@app.route("/api/sku-info/<sku>")
@login_required
def sku_info(sku):
    sheets, _ = get_google_services()
    if not sheets:
        return jsonify({"error": "Google API ไม่พร้อมใช้งาน"})
    _, row = find_sku_row(sheets, sku)
    if not row:
        return jsonify({"error": "ไม่พบ SKU"})
    headers = ["SKU", "สถานะ", "Brand", "รุ่น", "Tab", "Color", "Made in",
               "Botton Code", "ผลิต-ปี", "ทรงกางเกง", "เนื้อผ้า", "ขนาด",
               "ตำหนิ", "สภาพ", "วันที่รับ", "ต้นทุน/ชิ้น", "ทุนรวม", "กำไรรวม",
               "Instock", "ขาย/ชิ้น", "สต๊อกคงเหลือ", "ช่องทางขาย", "Link-ภาพสินค้า"]
    data = {headers[i]: row[i] if i < len(row) else "" for i in range(len(headers))}
    return jsonify(data)

@app.route("/api/inventory-stats")
@login_required
def inventory_stats():
    sheets, _ = get_google_services()
    if not sheets:
        return jsonify({"error": "no google"})
    rows = sheets_get(sheets, INVENTORY_SHEET_ID, "Inventory!A2:V")
    instock = sum(1 for r in rows if len(r) > 1 and r[1] == "in Stock")
    soldout = sum(1 for r in rows if len(r) > 1 and r[1] == "Sold Out")
    brands = {}
    for r in rows:
        if len(r) > 2 and r[1] == "in Stock":
            b = r[2] or "Other"
            brands[b] = brands.get(b, 0) + 1
    return jsonify({"instock": instock, "soldout": soldout, "brands": brands})

@app.route("/uploads/<filename>")
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)

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
                         role=request.form.get("role", "staff"),
                         name=request.form.get("name", ""))
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
