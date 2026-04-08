import os, io
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "topjeans-secret-2024")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///topjeans.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"] = "uploads"
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"

INVENTORY_SHEET_ID   = os.getenv("INVENTORY_SHEET_ID","1Vzcs4mUnOKk0VwkouBq3m87fdO3xAV7F9na88PVsS0o")
SALES_SHEET_ID       = os.getenv("SALES_SHEET_ID","11iPX3SHK-vt6DlN1rJeUXjXsFeHwSFXXCPNFc-AM4kA")
DRIVE_FOLDER_ID      = os.getenv("DRIVE_FOLDER_ID","")
SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE","credentials.json")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets","https://www.googleapis.com/auth/drive"]

def get_google_services():
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        creds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        return build("sheets","v4",credentials=creds), build("drive","v3",credentials=creds)
    except Exception as e:
        print(f"[Google] {e}"); return None, None

def sheets_get(svc, sid, rng):
    try: return svc.spreadsheets().values().get(spreadsheetId=sid,range=rng).execute().get("values",[])
    except Exception as e: print(f"[GET] {e}"); return []

def sheets_append(svc, sid, rng, vals):
    try: svc.spreadsheets().values().append(spreadsheetId=sid,range=rng,valueInputOption="USER_ENTERED",body={"values":vals}).execute(); return True
    except Exception as e: print(f"[APPEND] {e}"); return False

def sheets_update(svc, sid, rng, vals):
    try: svc.spreadsheets().values().update(spreadsheetId=sid,range=rng,valueInputOption="USER_ENTERED",body={"values":vals}).execute(); return True
    except Exception as e: print(f"[UPDATE] {e}"); return False

def deduct_stock(svc, sku):
    try:
        rows = sheets_get(svc, INVENTORY_SHEET_ID, "Inventory!A:B")
        for i,r in enumerate(rows):
            if r and r[0]==sku:
                return sheets_update(svc, INVENTORY_SHEET_ID, f"Inventory!B{i+1}", [["Sold Out"]])
        return False
    except: return False

def upload_slip(drive_svc, file_bytes, filename, mime_type):
    try:
        from googleapiclient.http import MediaIoBaseUpload
        meta = {"name": filename}
        if DRIVE_FOLDER_ID: meta["parents"] = [DRIVE_FOLDER_ID]
        media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=mime_type)
        f = drive_svc.files().create(body=meta,media_body=media,fields="id,webViewLink").execute()
        drive_svc.permissions().create(fileId=f["id"],body={"type":"anyone","role":"reader"}).execute()
        return f["id"], f.get("webViewLink","")
    except Exception as e: print(f"[Drive] {e}"); return None, ""

class User(UserMixin, db.Model):
    id       = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    role     = db.Column(db.String(20), default="staff")
    name     = db.Column(db.String(100), default="")

class SaleLog(db.Model):
    id            = db.Column(db.Integer, primary_key=True)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    staff_id      = db.Column(db.Integer, db.ForeignKey("user.id"))
    sku           = db.Column(db.String(30))
    product_name  = db.Column(db.String(200))
    customer      = db.Column(db.String(100))
    phone         = db.Column(db.String(30))
    price         = db.Column(db.Float)
    cost          = db.Column(db.Float)
    channel       = db.Column(db.String(50))
    slip_url      = db.Column(db.String(500))
    slip_drive_id = db.Column(db.String(200))
    notes         = db.Column(db.Text)
    staff         = db.relationship("User", backref="sales")

@login_manager.user_loader
def load_user(uid): return db.session.get(User, int(uid))

@app.route("/login", methods=["GET","POST"])
def login():
    if current_user.is_authenticated: return redirect(url_for("dashboard"))
    if request.method=="POST":
        u = User.query.filter_by(username=request.form["username"]).first()
        if u and check_password_hash(u.password, request.form["password"]):
            login_user(u); return redirect(url_for("dashboard"))
        flash("ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง","error")
    return render_template("login.html")

@app.route("/logout")
@login_required
def logout(): logout_user(); return redirect(url_for("login"))

@app.route("/")
@login_required
def dashboard():
    sheets,_ = get_google_services()
    inv   = sheets_get(sheets, INVENTORY_SHEET_ID, "Inventory!A2:V") if sheets else []
    sales = sheets_get(sheets, SALES_SHEET_ID, "Mar. 2026!A2:AZ") if sheets else []
    total   = sum(1 for r in inv if len(r)>1 and r[1])
    instock = sum(1 for r in inv if len(r)>1 and r[1]=="in Stock")
    sold    = sum(1 for r in inv if len(r)>1 and r[1]=="Sold Out")
    sales_total = 0
    for r in sales:
        if len(r)>11 and r[11]:
            try: sales_total += float(str(r[11]).replace(",",""))
            except: pass
    brands = {}
    for r in inv:
        if len(r)>2 and r[1]=="in Stock": b=r[2] or "Other"; brands[b]=brands.get(b,0)+1
    recent = SaleLog.query.order_by(SaleLog.created_at.desc()).limit(10).all()
    return render_template("dashboard.html", total=total, instock=instock, sold=sold,
        sales_total=sales_total, brand_count=brands, recent_sales=recent, google_ok=sheets is not None)

@app.route("/inventory")
@login_required
def inventory():
    sheets,_ = get_google_services()
    rows = sheets_get(sheets, INVENTORY_SHEET_ID, "Inventory!A2:AK") if sheets else []
    q=request.args.get("q","").lower(); status=request.args.get("status",""); brand=request.args.get("brand","")
    if q: rows=[r for r in rows if any(q in str(c).lower() for c in r)]
    if status: rows=[r for r in rows if len(r)>1 and r[1]==status]
    if brand:  rows=[r for r in rows if len(r)>2 and r[2]==brand]
    brands=sorted(set(r[2] for r in rows if len(r)>2 and r[2]))
    return render_template("inventory.html", rows=rows, brands=brands, q=q, status=status, brand=brand, google_ok=sheets is not None)

@app.route("/sales")
@login_required
def sales():
    sheets,_ = get_google_services()
    rows = sheets_get(sheets, SALES_SHEET_ID, "Mar. 2026!A2:AZ") if sheets else []
    local = SaleLog.query.order_by(SaleLog.created_at.desc()).all()
    return render_template("sales.html", rows=rows, local_sales=local, google_ok=sheets is not None)

@app.route("/new-sale", methods=["GET","POST"])
@login_required
def new_sale():
    sheets,drive = get_google_services()
    inv_items = []
    if sheets:
        raw = sheets_get(sheets, INVENTORY_SHEET_ID, "Inventory!A2:AK")
        inv_items = [r for r in raw if len(r)>1 and r[1]=="in Stock"]
    if request.method=="POST":
        sku=request.form.get("sku","").strip(); product_name=request.form.get("product_name","")
        customer=request.form.get("customer",""); phone=request.form.get("phone","")
        price=float(request.form.get("price",0) or 0); cost=float(request.form.get("cost",0) or 0)
        channel=request.form.get("channel",""); notes=request.form.get("notes","")
        slip_url=""; slip_drive_id=""
        slip_file=request.files.get("slip")
        if slip_file and slip_file.filename:
            fb=slip_file.read(); mime=slip_file.mimetype or "image/jpeg"
            sname=f"slip_{sku}_{datetime.now().strftime('%Y%m%d%H%M%S')}{os.path.splitext(slip_file.filename)[1]}"
            if drive: slip_drive_id,slip_url=upload_slip(drive,fb,sname,mime)
            with open(os.path.join(app.config["UPLOAD_FOLDER"],sname),"wb") as f: f.write(fb)
            if not slip_url: slip_url=url_for("uploaded_file",filename=sname,_external=True)
        stock_ok=deduct_stock(sheets,sku) if sheets and sku else False
        now=datetime.now().strftime("%d/%m/%Y")
        row=[now,current_user.name or current_user.username,channel,customer,phone,"","",
             product_name,sku,slip_url,"1",str(int(price)),"",str(int(price)),now,slip_url,
             "โอน/COD","","","","",now,"",str(int(cost)),str(int(price-cost)),"","","","",channel]
        sheets_ok=sheets_append(sheets,SALES_SHEET_ID,"Mar. 2026!A:A",[row]) if sheets else False
        log=SaleLog(staff_id=current_user.id,sku=sku,product_name=product_name,customer=customer,
            phone=phone,price=price,cost=cost,channel=channel,slip_url=slip_url,slip_drive_id=slip_drive_id,notes=notes)
        db.session.add(log); db.session.commit()
        msg=f"บันทึกการขาย {sku} เรียบร้อย"
        if stock_ok: msg+=" · ตัดสต๊อกแล้ว"
        if sheets_ok: msg+=" · บันทึกใน Google Sheets แล้ว"
        flash(msg,"success"); return redirect(url_for("sales"))
    return render_template("new_sale.html", inventory_items=inv_items, google_ok=sheets is not None)

@app.route("/api/sku-info/<sku>")
@login_required
def sku_info(sku):
    sheets,_ = get_google_services()
    if not sheets: return jsonify({"error":"Google API ไม่พร้อม"})
    rows = sheets_get(sheets, INVENTORY_SHEET_ID, "Inventory!A2:AK")
    for r in rows:
        if r and r[0]==sku:
            keys=["SKU","สถานะ","Brand","รุ่น","Tab","Color","Made in","Botton","ปี","ทรง","เนื้อผ้า","ขนาด","ตำหนิ","สภาพ","วันรับ","ต้นทุน","ทุนรวม","กำไร","Instock","ราคาขาย","สต๊อก","ช่องทาง","Link"]
            return jsonify({keys[i]:r[i] if i<len(r) else "" for i in range(len(keys))})
    return jsonify({"error":"ไม่พบ SKU"})

@app.route("/uploads/<filename>")
def uploaded_file(filename): return send_from_directory(app.config["UPLOAD_FOLDER"],filename)

@app.route("/admin/users", methods=["GET","POST"])
@login_required
def admin_users():
    if current_user.role!="admin": flash("เฉพาะ Admin","error"); return redirect(url_for("dashboard"))
    if request.method=="POST":
        a=request.form.get("action")
        if a=="create":
            un=request.form["username"].strip()
            if not User.query.filter_by(username=un).first():
                db.session.add(User(username=un,password=generate_password_hash(request.form["password"]),
                    role=request.form.get("role","staff"),name=request.form.get("name",""))); db.session.commit()
                flash(f"สร้าง account {un} แล้ว","success")
            else: flash("ชื่อผู้ใช้นี้มีอยู่แล้ว","error")
        elif a=="delete":
            uid=int(request.form["user_id"])
            if uid!=current_user.id: db.session.delete(db.session.get(User,uid)); db.session.commit(); flash("ลบแล้ว","success")
    return render_template("admin_users.html", users=User.query.all())

def init_db():
    with app.app_context():
        db.create_all()
        if not User.query.filter_by(username="admin").first():
            db.session.add(User(username="admin",password=generate_password_hash("topjeans2024"),role="admin",name="Admin"))
            db.session.commit(); print("✅ Created admin: admin / topjeans2024")

if __name__=="__main__":
    init_db()
    if __name__ == '__main__':
    app.run(debug=False)app.run(debug=False, port=5000)
