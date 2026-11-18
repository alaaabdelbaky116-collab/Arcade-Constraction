"""نظام المقايسات وإدارة المشاريع (Flask + SQLite)

التشغيل:
    pip install flask
    python app.py
أول تشغيل: بيطلع "كود إعداد" في شاشة التشغيل وفي الملف setup-code.txt جنب قاعدة البيانات،
افتح الموقع وأنشئ حساب المدير بالكود ده.
"""
import logging
import math
import os
import re
import secrets
import sqlite3
import threading
import time
import zipfile
from datetime import date, datetime, timedelta
from functools import wraps
from logging.handlers import RotatingFileHandler

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template, request,
                   send_from_directory, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("BOQ_DB", os.path.join(BASE, "boq.db"))
DATA_DIR = os.path.dirname(os.path.abspath(DB_PATH))
UPLOAD_DIR = os.path.join(BASE, "static", "uploads")
BACKUP_DIR = os.environ.get("BOQ_BACKUP_DIR", os.path.join(DATA_DIR, "backups"))
BACKUP_KEEP = int(os.environ.get("BOQ_BACKUP_KEEP", "30"))   # عدد النسخ اللي بتفضل محفوظة
BACKUP_HOUR = int(os.environ.get("BOQ_BACKUP_HOUR", "1"))    # أقرب ساعة تبدأ فيها النسخة اليومية
SETUP_CODE_PATH = os.path.join(DATA_DIR, "setup-code.txt")

ROLES = {"admin": "مدير", "engineer": "مهندس", "purchasing": "مشتريات", "accountant": "حسابات", "viewer": "مشاهدة فقط"}
EDIT_ROLES = ("admin", "engineer")
STATUSES = ["عرض سعر", "تعاقد", "تحت التنفيذ", "تم التسليم", "ملغي"]
REV_STATUSES = ["صادرة", "مُرسلة للعميل", "معتمدة من العميل", "مرفوضة", "مستبدلة"]
REV_APPROVED = "معتمدة من العميل"
CO_STATUSES = ["مسودة", "مُرسلة للعميل", "معتمدة", "مرفوضة", "ملغاة"]
CO_DRAFT, CO_SENT, CO_APPROVED, CO_REJECTED, CO_CANCELLED = CO_STATUSES


def load_secret():
    env = os.environ.get("BOQ_SECRET")
    if env:
        return env
    path = os.path.join(DATA_DIR, "secret.key")
    try:
        with open(path, encoding="utf-8") as fh:
            value = fh.read().strip()
        if len(value) >= 32:
            return value
    except OSError:
        pass
    value = secrets.token_hex(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(value)
    return value


app = Flask(__name__)
app.config.update(
    SECRET_KEY=load_secret(),
    MAX_CONTENT_LENGTH=4 * 1024 * 1024,           # حد أقصى لحجم رفع اللوجو
    SESSION_COOKIE_NAME="boq_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("BOQ_HTTPS") == "1",   # فعّله لو الموقع شغال بـ HTTPS
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)

# أي خطأ داخلي بيتسجل بالتفصيل في error.log جنب قاعدة البيانات
_err_handler = RotatingFileHandler(
    os.path.join(DATA_DIR, "error.log"), maxBytes=500_000, backupCount=2, encoding="utf-8"
)
_err_handler.setLevel(logging.ERROR)
_err_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
app.logger.addHandler(_err_handler)

SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    sort INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER NOT NULL REFERENCES categories(id),
    name TEXT NOT NULL UNIQUE,
    unit TEXT NOT NULL,
    unit_price REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    client TEXT DEFAULT '',
    location TEXT DEFAULT '',
    area_sqm REAL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'عرض سعر',
    markup_pct REAL NOT NULL DEFAULT 15,
    vat_pct REAL NOT NULL DEFAULT 14,
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS boq_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    item_id INTEGER REFERENCES items(id) ON DELETE SET NULL,
    section TEXT NOT NULL DEFAULT 'أخرى',
    description TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT '',
    qty REAL NOT NULL DEFAULT 0,
    unit_price REAL NOT NULL DEFAULT 0,
    markup_pct REAL,
    sort INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    full_name TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'viewer',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    user_id INTEGER,
    username TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    entity TEXT NOT NULL DEFAULT '',
    entity_id INTEGER,
    details TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(id DESC);
CREATE TABLE IF NOT EXISTS quote_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    created_by INTEGER,
    created_by_name TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'صادرة',
    markup_pct REAL NOT NULL,
    vat_pct REAL NOT NULL,
    subtotal REAL NOT NULL DEFAULT 0,
    total REAL NOT NULL DEFAULT 0,
    validity_days TEXT NOT NULL DEFAULT '',
    p_name TEXT NOT NULL DEFAULT '',
    p_client TEXT NOT NULL DEFAULT '',
    p_location TEXT NOT NULL DEFAULT '',
    p_area REAL NOT NULL DEFAULT 0,
    p_notes TEXT NOT NULL DEFAULT '',
    UNIQUE(project_id, number)
);
CREATE TABLE IF NOT EXISTS quote_revision_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id INTEGER NOT NULL REFERENCES quote_revisions(id) ON DELETE CASCADE,
    section TEXT NOT NULL,
    description TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT '',
    qty REAL NOT NULL DEFAULT 0,
    unit_price REAL NOT NULL DEFAULT 0,
    markup_pct REAL,
    sort INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS change_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    title TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    requested_by TEXT NOT NULL DEFAULT '',
    request_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'مسودة',
    markup_pct REAL NOT NULL,
    vat_pct REAL NOT NULL,
    time_impact_days REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    created_by INTEGER,
    created_by_name TEXT NOT NULL DEFAULT '',
    sent_at TEXT,
    decided_at TEXT,
    decided_by_client TEXT NOT NULL DEFAULT '',
    decision_ref TEXT NOT NULL DEFAULT '',
    decision_note TEXT NOT NULL DEFAULT '',
    UNIQUE(project_id, number)
);
CREATE TABLE IF NOT EXISTS change_order_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    co_id INTEGER NOT NULL REFERENCES change_orders(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    description_en TEXT,
    unit TEXT NOT NULL DEFAULT '',
    qty REAL NOT NULL DEFAULT 0,
    unit_price REAL NOT NULL DEFAULT 0,
    markup_pct REAL,
    sort INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS purchase_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'مواد',
    description TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT '',
    qty REAL NOT NULL DEFAULT 1,
    boq_line_id INTEGER,
    needed_by TEXT NOT NULL DEFAULT '',
    priority TEXT NOT NULL DEFAULT 'عادي',
    status TEXT NOT NULL DEFAULT 'جديد',
    notes TEXT NOT NULL DEFAULT '',
    supplier TEXT NOT NULL DEFAULT '',
    expected_date TEXT NOT NULL DEFAULT '',
    requested_by INTEGER,
    requested_by_name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    unseen INTEGER NOT NULL DEFAULT 0,
    UNIQUE(project_id, number)
);
CREATE TABLE IF NOT EXISTS purchase_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    req_id INTEGER NOT NULL REFERENCES purchase_requests(id) ON DELETE CASCADE,
    ts TEXT NOT NULL,
    user_name TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS custody_categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    sort INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS custodies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    number INTEGER NOT NULL UNIQUE,
    holder_id INTEGER NOT NULL,
    holder_name TEXT NOT NULL DEFAULT '',
    project_id INTEGER,
    purpose TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'مفتوحة',
    created_at TEXT NOT NULL,
    created_by_name TEXT NOT NULL DEFAULT '',
    submitted_at TEXT,
    settled_at TEXT,
    settled_by_name TEXT NOT NULL DEFAULT '',
    settle_note TEXT NOT NULL DEFAULT '',
    final_received REAL, final_spent REAL, final_balance REAL
);
CREATE TABLE IF NOT EXISTS custody_advances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    custody_id INTEGER NOT NULL REFERENCES custodies(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    amount REAL NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS custody_expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    custody_id INTEGER NOT NULL REFERENCES custodies(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    category_id INTEGER NOT NULL REFERENCES custody_categories(id),
    description TEXT NOT NULL,
    amount REAL NOT NULL,
    receipt TEXT NOT NULL DEFAULT 'بدون مستند',
    receipt_no TEXT NOT NULL DEFAULT '',
    disallowed INTEGER NOT NULL DEFAULT 0,
    disallow_reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS custody_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    custody_id INTEGER NOT NULL REFERENCES custodies(id) ON DELETE CASCADE,
    ts TEXT NOT NULL,
    user_name TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_pr_project ON purchase_requests(project_id, status);
"""

SETTING_DEFAULTS = {
    "company_name": "اسم الشركة",
    "company_tagline": "",
    "address": "",
    "phones": "",
    "email": "",
    "tax_no": "",
    "commercial_reg": "",
    "validity_days": "30",
    "default_terms": "",
    "logo": "",
    # نسخة إنجليزي لعروض الأسعار الإنجليزي (لو فاضية بيستخدم العربي)
    "company_name_en": "",
    "company_tagline_en": "",
    "address_en": "",
    "default_terms_en": "",
}
LOGO_EXTS = {"png", "jpg", "jpeg", "webp"}  # من غير SVG عشان الأمان

# أسعار تجريبية للتوضيح فقط - لازم تتراجع وتتعدل من شاشة "مكتبة البنود"
SEED = {
    "أعمال هدم وتجهيز": [
        ("هدم أرضيات قديمة", "م2", 90), ("هدم حوائط طوب", "م3", 450),
        ("نقل مخلفات", "رحلة", 1200),
    ],
    "أعمال مدنية": [
        ("بناء طوب أحمر 12 سم", "م2", 260), ("بياض محارة", "م2", 150),
        ("عزل رطوبة للأرضيات", "م2", 180),
    ],
    "جبس بورد": [
        ("أسقف جبس بورد مسطحة", "م2", 380), ("أسقف جبس بورد بفتحات إضاءة", "م2", 480),
        ("حوائط جبس بورد", "م2", 420), ("بنوار ستائر جبس", "م.ط", 250),
    ],
    "دهانات": [
        ("دهان بلاستيك (معجون + 3 أوجه)", "م2", 120), ("دهان زيت للأخشاب والمعادن", "م2", 160),
        ("دهان ديكوري", "م2", 320),
    ],
    "أرضيات": [
        ("أرضيات بورسلين 60×60 (توريد وتركيب)", "م2", 750),
        ("أرضيات رخام (توريد وتركيب)", "م2", 1800),
        ("أرضيات باركيه", "م2", 900), ("وزرة بورسلين", "م.ط", 90),
    ],
    "كهرباء": [
        ("نقطة إنارة", "نقطة", 350), ("نقطة بريزة", "نقطة", 400),
        ("لوحة توزيع رئيسية", "عدد", 8500), ("كابل تغذية رئيسي", "م.ط", 380),
    ],
    "تكييف وتهوية": [
        ("تكييف سبليت 3 حصان (توريد وتركيب)", "عدد", 42000),
        ("مجاري هواء وفتحات تغذية", "م2", 650),
    ],
    "سباكة": [
        ("نقطة تغذية مياه", "نقطة", 450), ("نقطة صرف", "نقطة", 400),
    ],
    "نجارة وأبواب وواجهات": [
        ("باب خشب داخلي", "عدد", 9500), ("واجهة ألوميتال زجاج سكريت", "م2", 3200),
        ("باب زجاج سكريت", "عدد", 14000), ("كاونتر استقبال", "عدد", 22000),
    ],
    "لافتات وإضاءة ديكور": [
        ("لافتة واجهة إضاءة LED", "م2", 4500),
    ],
}

# أعمدة اتضافت بعد النسخ الأولى (بتتضاف تلقائي للقواعد القديمة وقت التشغيل)
MIGRATIONS = {
    "categories": {"name_en": "TEXT"},
    "items": {"name_en": "TEXT"},
    "projects": {"lang": "TEXT NOT NULL DEFAULT 'ar'"},
    "boq_lines": {"markup_pct": "REAL", "description_en": "TEXT"},
    "quote_revisions": {"lang": "TEXT NOT NULL DEFAULT 'ar'"},
    "quote_revision_lines": {"markup_pct": "REAL", "description_en": "TEXT", "section_en": "TEXT",
                             "line_id": "INTEGER"},
}

# ترجمة إنجليزي للبنود والأقسام التجريبية (راجعها وعدّلها من مكتبة البنود)
CAT_EN = {
    "أعمال هدم وتجهيز": "Demolition & Preparation Works",
    "أعمال مدنية": "Civil Works",
    "جبس بورد": "Gypsum Board Works",
    "دهانات": "Painting Works",
    "أرضيات": "Flooring",
    "كهرباء": "Electrical Works",
    "تكييف وتهوية": "HVAC",
    "سباكة": "Plumbing Works",
    "نجارة وأبواب وواجهات": "Carpentry, Doors & Storefronts",
    "لافتات وإضاءة ديكور": "Signage & Decorative Lighting",
}
ITEM_EN = {
    "هدم أرضيات قديمة": "Demolition of existing flooring",
    "هدم حوائط طوب": "Demolition of brick walls",
    "نقل مخلفات": "Debris removal",
    "بناء طوب أحمر 12 سم": "Red brick masonry, 12 cm",
    "بياض محارة": "Cement plastering",
    "عزل رطوبة للأرضيات": "Waterproofing for floors",
    "أسقف جبس بورد مسطحة": "Flat gypsum board ceiling",
    "أسقف جبس بورد بفتحات إضاءة": "Gypsum board ceiling with lighting coves",
    "حوائط جبس بورد": "Gypsum board partitions",
    "بنوار ستائر جبس": "Gypsum curtain pelmets",
    "دهان بلاستيك (معجون + 3 أوجه)": "Plastic paint (putty + 3 coats)",
    "دهان زيت للأخشاب والمعادن": "Oil paint for wood & metal",
    "دهان ديكوري": "Decorative paint finish",
    "أرضيات بورسلين 60×60 (توريد وتركيب)": "Porcelain flooring 60×60 (supply & install)",
    "أرضيات رخام (توريد وتركيب)": "Marble flooring (supply & install)",
    "أرضيات باركيه": "Parquet flooring",
    "وزرة بورسلين": "Porcelain skirting",
    "نقطة إنارة": "Lighting point",
    "نقطة بريزة": "Power socket point",
    "لوحة توزيع رئيسية": "Main distribution board",
    "كابل تغذية رئيسي": "Main feeder cable",
    "تكييف سبليت 3 حصان (توريد وتركيب)": "Split A/C unit 3 HP (supply & install)",
    "مجاري هواء وفتحات تغذية": "Air ducts & supply diffusers",
    "نقطة تغذية مياه": "Water supply point",
    "نقطة صرف": "Drainage point",
    "باب خشب داخلي": "Interior wooden door",
    "واجهة ألوميتال زجاج سكريت": "Aluminium & tempered glass storefront",
    "باب زجاج سكريت": "Tempered glass door",
    "كاونتر استقبال": "Reception counter",
    "لافتة واجهة إضاءة LED": "Illuminated LED facade sign",
}
# الوحدات في النسخة الإنجليزي (أي وحدة مش هنا بتفضل زي ما هي)
UNIT_EN = {
    "م2": "m²", "م٢": "m²", "م3": "m³", "م.ط": "l.m", "م": "m", "نقطة": "pt", "عدد": "no.",
    "رحلة": "trip", "كجم": "kg", "طن": "ton", "لتر": "L", "يوم": "day", "ساعة": "hr",
    "طقم": "set", "مقطوعية": "lump sum",
}
LABELS = {
    "ar": {
        "quotation": "عرض سعر", "quote_no": "رقم العرض", "date": "التاريخ", "valid": "سريان العرض: {n} يوم",
        "project": "المشروع", "client": "العميل", "location": "الموقع", "area": "المساحة", "sqm": "م2",
        "idx": "م", "desc": "بيان الأعمال", "unit": "الوحدة", "qty": "الكمية", "uprice": "سعر الوحدة",
        "total": "الإجمالي", "before_vat": "الإجمالي قبل القيمة المضافة", "vat": "القيمة المضافة",
        "grand": "الإجمالي النهائي (ج.م)", "terms": "ملاحظات وشروط", "for": "عن",
        "client_approval": "اعتماد العميل", "tax_no": "الرقم الضريبي", "reg_no": "السجل التجاري",
        "tel": "ت", "draft": "مسودة",
        "co": "أمر تغيير", "co_no": "رقم الأمر", "co_reason": "وصف التغيير", "co_by": "طلب التغيير",
        "co_time": "الأثر على مدة التنفيذ", "days": "يوم", "additions": "إجمالي الإضافات",
        "omissions": "إجمالي الخصومات (أعمال ملغاة)", "net_before_vat": "صافي التغيير قبل القيمة المضافة",
        "net_total": "صافي التغيير شامل القيمة المضافة (ج.م)", "approved": "اعتماد العميل",
        "approved_by": "اعتمده", "approval_ref": "المرجع", "approval_date": "تاريخ الاعتماد",
        "none": "لا يوجد",
    },
    "en": {
        "quotation": "Quotation", "quote_no": "Quotation No.", "date": "Date", "valid": "Valid for: {n} days",
        "project": "Project", "client": "Client", "location": "Location", "area": "Area", "sqm": "m²",
        "idx": "#", "desc": "Description of Works", "unit": "Unit", "qty": "Qty", "uprice": "Unit Price",
        "total": "Total", "before_vat": "Total before VAT", "vat": "VAT",
        "grand": "Grand Total (EGP)", "terms": "Notes & Terms", "for": "For",
        "client_approval": "Client Approval", "tax_no": "Tax Reg. No.", "reg_no": "Commercial Reg. No.",
        "tel": "Tel", "draft": "DRAFT",
        "co": "Change Order", "co_no": "Change Order No.", "co_reason": "Description of Change",
        "co_by": "Requested by", "co_time": "Time impact", "days": "days",
        "additions": "Total additions", "omissions": "Total omissions (credits)",
        "net_before_vat": "Net change before VAT", "net_total": "Net change incl. VAT (EGP)",
        "approved": "Client approval", "approved_by": "Approved by", "approval_ref": "Reference",
        "approval_date": "Approval date", "none": "None",
    },
}


def rget(r, key, default=None):
    try:
        v = r[key]
    except (KeyError, IndexError):
        return default
    return default if v is None else v


def backfill_english(db):
    """بيملا الأسماء الإنجليزي للبنود والأقسام التجريبية لو لسه فاضية (مابيلمسش أي اسم اتكتب بإيدك)."""
    for ar, en in CAT_EN.items():
        db.execute("UPDATE categories SET name_en = ? WHERE name = ? AND name_en IS NULL", (en, ar))
    for ar, en in ITEM_EN.items():
        db.execute("UPDATE items SET name_en = ? WHERE name = ? AND name_en IS NULL", (en, ar))


AUDIT_LABELS = {
    "cu_new": "عهدة جديدة", "cu_advance": "إضافة مبلغ لعهدة", "cu_expense": "مصروف عهدة", "cu_expense_edit": "تعديل مصروف عهدة",
    "cu_expense_delete": "حذف مصروف عهدة", "cu_disallow": "استبعاد/قبول بند عهدة", "cu_submit": "تقديم عهدة للتصفية",
    "cu_settle": "اعتماد تصفية عهدة", "cu_reject": "إرجاع عهدة للتعديل", "cu_reopen": "إعادة فتح عهدة",
    "cu_delete": "حذف عهدة", "cu_cat": "بنود العهد",
    "pr_new": "طلب شراء جديد", "pr_update": "تحديث طلب شراء", "pr_status": "قرار على طلب شراء",
    "pr_edit": "تعديل طلب شراء", "pr_delete": "حذف طلب شراء",
    "setup": "إنشاء أول مدير", "login": "تسجيل دخول", "login_failed": "محاولة دخول فاشلة",
    "logout": "تسجيل خروج", "password_change": "تغيير كلمة المرور",
    "user_new": "إضافة مستخدم", "user_update": "تعديل مستخدم", "user_password": "إعادة تعيين كلمة مرور",
    "project_new": "مشروع جديد", "project_update": "تعديل مشروع", "project_delete": "حذف مشروع",
    "project_duplicate": "نسخ مقايسة", "line_add": "إضافة بند", "line_update": "تعديل بند",
    "line_delete": "حذف بند", "item_add": "إضافة بند للمكتبة", "item_update": "تعديل بند بالمكتبة",
    "item_delete": "حذف بند من المكتبة", "settings": "تعديل إعدادات الشركة",
    "revision_new": "إصدار نسخة عرض سعر", "revision_status": "تغيير حالة نسخة",
    "revision_delete": "حذف نسخة عرض سعر", "backup_manual": "نسخة احتياطية يدوية",
    "backup_download": "تنزيل نسخة احتياطية",
    "co_new": "أمر تغيير جديد", "co_update": "تعديل أمر تغيير", "co_line_add": "إضافة بند لأمر تغيير",
    "co_line_update": "تعديل بند في أمر تغيير", "co_line_delete": "حذف بند من أمر تغيير",
    "co_status": "تغيير حالة أمر تغيير", "co_delete": "حذف أمر تغيير",
}


def safe_print(msg):
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode())


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    # ترقية قواعد البيانات القديمة بإضافة الأعمدة الجديدة
    for table, needed in MIGRATIONS.items():
        have = [r[1] for r in db.execute(f"PRAGMA table_info({table})")]
        for col, ddl in needed.items():
            if col not in have:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    if db.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0:
        for i, (cat, items) in enumerate(SEED.items()):
            cur = db.execute("INSERT INTO categories(name, sort) VALUES (?, ?)", (cat, i))
            for name, unit, price in items:
                db.execute(
                    "INSERT INTO items(category_id, name, unit, unit_price) VALUES (?,?,?,?)",
                    (cur.lastrowid, name, unit, price),
                )
    backfill_english(db)
    seed_custody_categories(db)
    db.commit()
    db.close()


def ensure_setup_code(announce=True):
    """أول تشغيل (مفيش مستخدمين): كود إعداد لازم يتكتب عند إنشاء أول مدير."""
    c = sqlite3.connect(DB_PATH)
    n = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    c.close()
    if n:
        if os.path.exists(SETUP_CODE_PATH):
            try:
                os.remove(SETUP_CODE_PATH)
            except OSError:
                pass
        return None
    if not os.path.exists(SETUP_CODE_PATH):
        with open(SETUP_CODE_PATH, "w", encoding="utf-8") as fh:
            fh.write(secrets.token_hex(4))
    with open(SETUP_CODE_PATH, encoding="utf-8") as fh:
        code = fh.read().strip()
    if announce:
        safe_print("=" * 60)
        safe_print(f"FIRST RUN - admin setup code: {code}")
        safe_print(f"(also saved in: {SETUP_CODE_PATH})")
        safe_print("=" * 60)
    return code


# ---------- حسابات ----------

def line_markup(r, default):
    """هامش البند الفعلي: هامشه الخاص لو متحدد، وإلا هامش المشروع الافتراضي."""
    try:
        v = r["markup_pct"]
    except (KeyError, IndexError):
        v = None
    return default if v is None else v


def mk_label(v):
    return "افتراضي" if v is None else f"{num(v)}%"


def calc_totals(rows, default_markup, vat_pct):
    subtotal = markup = 0.0
    n = 0
    for r in rows:
        cost = r["qty"] * r["unit_price"]
        subtotal += cost
        markup += cost * line_markup(r, default_markup) / 100
        n += 1
    before_vat = subtotal + markup
    vat = before_vat * vat_pct / 100
    return {
        "lines": n,
        "subtotal": subtotal,          # التكلفة
        "markup": markup,              # مجموع هوامش البنود (الربح والعمومي)
        "markup_avg": (markup / subtotal * 100) if subtotal else default_markup,
        "before_vat": before_vat,
        "vat": vat,
        "total": before_vat + vat,     # سعر العميل
    }


def project_totals(db, project):
    rows = db.execute(
        "SELECT qty, unit_price, markup_pct FROM boq_lines WHERE project_id = ?", (project["id"],)
    ).fetchall()
    return calc_totals(rows, project["markup_pct"], project["vat_pct"])


@app.template_filter("money")
def money(v):
    return f"{(v or 0):,.2f}"


@app.template_filter("num")
def num(v):
    v = v or 0
    return f"{v:,.2f}".rstrip("0").rstrip(".") if v != int(v) else f"{int(v):,}"


@app.template_filter("plain")
def plain(v):
    """رقم نظيف لحقول الإدخال: 100 مش 100.0، و12.5 مش 12.5000 (من غير فواصل آلاف)."""
    return f"{(v or 0):.4f}".rstrip("0").rstrip(".") or "0"


@app.template_filter("audit_label")
def audit_label(code):
    return AUDIT_LABELS.get(code, code)


def fnum(value, default=0.0):
    try:
        v = float(str(value).replace(",", "").strip() or default)
    except ValueError:
        return default
    return v if math.isfinite(v) else default  # يمنع nan / inf


# ---------- سجل التعديلات ----------

def audit(action, entity="", entity_id=None, details="", username=None):
    u = g.get("user")
    name = u["username"] if u else (username or "")
    get_db().execute(
        "INSERT INTO audit_log(ts, user_id, username, action, entity, entity_id, details)"
        " VALUES (?,?,?,?,?,?,?)",
        (now_str(), u["id"] if u else None, name, action, entity, entity_id, details[:500]),
    )


# ---------- المستخدمين والجلسات ----------

LOCK_AFTER, LOCK_SECONDS, FAIL_WINDOW = 5, 300, 900
_FAILS = {}
_FAILS_LOCK = threading.Lock()
DUMMY_HASH = generate_password_hash("dummy-password-for-timing", method="pbkdf2:sha256")


def lockout_remaining(key):
    with _FAILS_LOCK:
        rec = _FAILS.get(key)
    if rec and rec["until"] > time.time():
        return rec["until"] - time.time()
    return 0


def register_failure(key):
    now = time.time()
    with _FAILS_LOCK:
        rec = _FAILS.get(key)
        if not rec or now - rec["first"] > FAIL_WINDOW or (rec["until"] and rec["until"] < now):
            rec = {"count": 0, "first": now, "until": 0}
        rec["count"] += 1
        if rec["count"] >= LOCK_AFTER:
            rec["until"] = now + LOCK_SECONDS
        _FAILS[key] = rec


def clear_failures(key):
    with _FAILS_LOCK:
        _FAILS.pop(key, None)


def hash_pw(pw):
    return generate_password_hash(pw, method="pbkdf2:sha256")


def password_error(pw, username=""):
    if len(pw) < 8:
        return "كلمة المرور لازم تكون 8 حروف أو أرقام على الأقل."
    if username and pw.lower() == username.lower():
        return "كلمة المرور ماينفعش تبقى زي اسم المستخدم."
    return ""


def start_session(user_row):
    session.clear()
    session["uid"] = user_row["id"]
    session["pwh"] = user_row["password_hash"][-16:]
    session.permanent = True


def csrf_token():
    token = session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf"] = token
    return token


app.jinja_env.globals["csrf_token"] = csrf_token
PUBLIC_ENDPOINTS = {"login", "setup"}
# المشتريات يشوفوا طلبات الشراء بس (مفيش مقايسات أو أسعار)
PURCHASING_ENDPOINTS = {"pr_list", "pr_view", "pr_update", "account", "logout", "login", "static"}
ACCOUNTANT_ENDPOINTS = {"cu_list", "cu_view", "cu_advance", "cu_review", "cu_expense_disallow", "cu_print", "cu_categories",
                        "cu_new", "account", "logout", "login", "static"}


@app.before_request
def guard():
    g.user = None
    ep = request.endpoint
    if ep == "static":
        return None
    db = get_db()
    if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        if ep != "setup":
            return redirect(url_for("setup"))
    else:
        uid = session.get("uid")
        if uid:
            u = db.execute("SELECT * FROM users WHERE id = ? AND active = 1", (uid,)).fetchone()
            if u and session.get("pwh") == u["password_hash"][-16:]:
                g.user = u
            else:
                session.clear()
    if g.user is None and ep not in PUBLIC_ENDPOINTS:
        if request.is_json:
            return jsonify(error="login required"), 401
        nxt = request.full_path.rstrip("?") if request.method == "GET" else ""
        return redirect(url_for("login", next=nxt) if nxt and nxt != "/" else url_for("login"))
    if g.user is not None and g.user["role"] == "purchasing" and ep not in PURCHASING_ENDPOINTS:
        if request.method == "GET" and not request.is_json:
            return redirect(url_for("pr_list"))
        abort(403)
    if g.user is not None and g.user["role"] == "accountant" and ep not in ACCOUNTANT_ENDPOINTS:
        if request.method == "GET" and not request.is_json:
            return redirect(url_for("cu_list"))
        abort(403)
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
        expected = session.get("csrf", "")
        if not expected or not sent or not secrets.compare_digest(sent.encode(), expected.encode()):
            abort(400, description="csrf")
    return None


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    if request.endpoint != "static":
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


def roles_required(*roles):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if g.user is None or g.user["role"] not in roles:
                abort(403)
            return fn(*a, **kw)
        return wrapper
    return deco


admin_only = roles_required("admin")
editor_only = roles_required(*EDIT_ROLES)


def error_response(code, msg):
    if request.is_json:
        return jsonify(error=msg), code
    return render_template("error.html", code=code, message=msg), code


@app.errorhandler(400)
def err400(e):
    if getattr(e, "description", "") == "csrf":
        return error_response(400, "انتهت صلاحية الصفحة. حدّث الصفحة (F5) وحاول تاني.")
    return error_response(400, "طلب غير صالح.")


@app.errorhandler(403)
def err403(_e):
    return error_response(403, "مش مسموحلك بالعملية دي.")


@app.errorhandler(404)
def err404(_e):
    return error_response(404, "الصفحة اللي بتدور عليها مش موجودة.")


@app.errorhandler(413)
def err413(_e):
    return error_response(413, "الملف أكبر من الحد المسموح (4 ميجا).")


# ---------- إعدادات الشركة ----------

def load_settings(db):
    s = dict(SETTING_DEFAULTS)
    for row in db.execute("SELECT key, value FROM settings"):
        if row["key"] in s:
            s[row["key"]] = row["value"]
    return s


def set_setting(db, key, value):
    db.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value)
    )


@app.context_processor
def inject_context():
    u = g.get("user")
    ctx = {
        "user": u, "ROLES": ROLES, "REV_STATUSES": REV_STATUSES, "STATUSES": STATUSES,
        "CO_STATUSES": CO_STATUSES,
        "can_edit": bool(u and u["role"] in EDIT_ROLES),
        "is_admin": bool(u and u["role"] == "admin"),
        "is_purchasing": bool(u and u["role"] in ("admin", "purchasing")),
        "pr_badge": 0, "cu_badge": 0, "CU_STATUSES": CU_STATUSES, "PR_STATUSES": PR_STATUSES,
        "backup_alert": "",
    }
    try:
        db = get_db()
        ctx["company"] = load_settings(db)
        ctx["pr_badge"] = pr_badge_count(db, u)
        ctx["cu_badge"] = cu_badge_count(db, u)
        if ctx["is_admin"]:
            ctx["backup_alert"] = backup_alert(db)
    except sqlite3.Error:
        ctx["company"] = dict(SETTING_DEFAULTS)
    return ctx


@app.route("/settings", methods=["GET", "POST"])
@admin_only
def settings():
    db = get_db()
    saved = False
    error = ""
    if request.method == "POST":
        f = request.form
        for key in SETTING_DEFAULTS:
            if key != "logo" and key in f:
                set_setting(db, key, f.get(key, "").strip())
        up = request.files.get("logo")
        if up and up.filename:
            ext = up.filename.rsplit(".", 1)[-1].lower() if "." in up.filename else ""
            if ext not in LOGO_EXTS:
                error = "صيغة اللوجو لازم تكون PNG أو JPG أو WEBP."
            else:
                os.makedirs(UPLOAD_DIR, exist_ok=True)
                name = f"logo.{ext}"
                for old in os.listdir(UPLOAD_DIR):
                    if old.startswith("logo."):
                        os.remove(os.path.join(UPLOAD_DIR, old))
                up.save(os.path.join(UPLOAD_DIR, name))
                set_setting(db, "logo", name)
        if request.form.get("remove_logo") and not (up and up.filename):
            db.execute("DELETE FROM settings WHERE key = 'logo'")
            if os.path.isdir(UPLOAD_DIR):
                for old in os.listdir(UPLOAD_DIR):
                    if old.startswith("logo."):
                        os.remove(os.path.join(UPLOAD_DIR, old))
        audit("settings", "settings")
        db.commit()
        saved = not error
    return render_template("settings.html", saved=saved, error=error, s=load_settings(db))


# ---------- الدخول والخروج والإعداد الأول ----------

def safe_next(nxt):
    if nxt and nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt:
        return nxt
    return url_for("projects_list")


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("projects_list"))
    db = get_db()
    error = ""
    nxt = request.args.get("next", "")
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        key = (request.remote_addr, username)
        wait = lockout_remaining(key)
        if wait:
            error = f"محاولات غلط كتير. جرّب تاني بعد {math.ceil(wait / 60)} دقيقة."
        else:
            u = db.execute("SELECT * FROM users WHERE username = ? AND active = 1", (username,)).fetchone()
            ok = check_password_hash(u["password_hash"] if u else DUMMY_HASH, password) and u is not None
            if ok:
                clear_failures(key)
                start_session(u)
                g.user = u
                audit("login", "user", u["id"])
                db.commit()
                return redirect(safe_next(nxt))
            register_failure(key)
            audit("login_failed", "user", None, username, username=username)
            db.commit()
            error = "اسم المستخدم أو كلمة المرور غلط."
    return render_template("login.html", error=error, next=nxt)


@app.route("/logout", methods=["POST"])
def logout():
    if g.user:
        audit("logout", "user", g.user["id"])
        get_db().commit()
    session.clear()
    return redirect(url_for("login"))


@app.route("/setup", methods=["GET", "POST"])
def setup():
    db = get_db()
    if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0:
        return redirect(url_for("login"))
    ensure_setup_code(announce=False)
    error = ""
    if request.method == "POST":
        f = request.form
        try:
            with open(SETUP_CODE_PATH, encoding="utf-8") as fh:
                expected = fh.read().strip()
        except OSError:
            expected = ""
        username = f.get("username", "").strip().lower()
        pw, pw2 = f.get("password", ""), f.get("password2", "")
        if not expected or not secrets.compare_digest(f.get("code", "").strip().encode(), expected.encode()):
            error = "كود الإعداد غلط. هتلاقيه في شاشة تشغيل البرنامج أو في ملف setup-code.txt."
        elif not re.fullmatch(r"[\w.\-]{3,32}", username):
            error = "اسم المستخدم 3 إلى 32 حرف (حروف وأرقام ونقطة وشرطة)."
        elif pw != pw2:
            error = "كلمتين المرور مش متطابقتين."
        else:
            error = password_error(pw, username)
        if not error:
            cur = db.execute(
                "INSERT INTO users(username, full_name, password_hash, role, active, created_at)"
                " VALUES (?,?,?,?,1,?)",
                (username, f.get("full_name", "").strip()[:80], hash_pw(pw), "admin", now_str()),
            )
            audit("setup", "user", cur.lastrowid, username, username=username)
            db.commit()
            try:
                os.remove(SETUP_CODE_PATH)
            except OSError:
                pass
            u = db.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
            start_session(u)
            return redirect(url_for("projects_list"))
    return render_template("setup.html", error=error)


@app.route("/account", methods=["GET", "POST"])
def account():
    db = get_db()
    error, done = "", False
    if request.method == "POST":
        f = request.form
        u = g.user
        if not check_password_hash(u["password_hash"], f.get("current", "")):
            error = "كلمة المرور الحالية غلط."
        elif f.get("new", "") != f.get("new2", ""):
            error = "كلمتين المرور الجداد مش متطابقتين."
        else:
            error = password_error(f.get("new", ""), u["username"])
        if not error:
            db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_pw(f["new"]), u["id"]))
            audit("password_change", "user", u["id"])
            db.commit()
            fresh = db.execute("SELECT * FROM users WHERE id = ?", (u["id"],)).fetchone()
            start_session(fresh)
            done = True
    return render_template("account.html", error=error, done=done)


# ---------- إدارة المستخدمين ----------

def other_active_admins(db, uid):
    return db.execute(
        "SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = 1 AND id <> ?", (uid,)
    ).fetchone()[0]


@app.route("/users")
@admin_only
def users_list():
    rows = get_db().execute("SELECT * FROM users ORDER BY id").fetchall()
    return render_template("users.html", users=rows)


@app.route("/users/new", methods=["POST"])
@admin_only
def user_new():
    db = get_db()
    f = request.form
    username = f.get("username", "").strip().lower()
    role = f.get("role") if f.get("role") in ROLES else "viewer"
    pw = f.get("password", "")
    err = ""
    if not re.fullmatch(r"[\w.\-]{3,32}", username):
        err = "اسم المستخدم 3 إلى 32 حرف (حروف وأرقام ونقطة وشرطة)."
    elif db.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
        err = "اسم المستخدم ده موجود قبل كده."
    else:
        err = password_error(pw, username)
    if err:
        flash(err, "error")
    else:
        cur = db.execute(
            "INSERT INTO users(username, full_name, password_hash, role, active, created_at)"
            " VALUES (?,?,?,?,1,?)",
            (username, f.get("full_name", "").strip()[:80], hash_pw(pw), role, now_str()),
        )
        audit("user_new", "user", cur.lastrowid, f"{username} ({ROLES[role]})")
        db.commit()
        flash(f"تم إضافة المستخدم {username}.", "ok")
    return redirect(url_for("users_list"))


@app.route("/users/<int:uid>/update", methods=["POST"])
@admin_only
def user_update(uid):
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if u is None:
        abort(404)
    f = request.form
    role = f.get("role") if f.get("role") in ROLES else u["role"]
    active = 1 if f.get("active") else 0
    if u["role"] == "admin" and u["active"] and (role != "admin" or not active) \
            and other_active_admins(db, uid) == 0:
        flash("لازم يفضل مدير واحد نشط على الأقل.", "error")
        return redirect(url_for("users_list"))
    db.execute(
        "UPDATE users SET full_name = ?, role = ?, active = ? WHERE id = ?",
        (f.get("full_name", "").strip()[:80], role, active, uid),
    )
    audit("user_update", "user", uid, f"{u['username']}: {ROLES[role]}، {'نشط' if active else 'موقوف'}")
    db.commit()
    flash("تم الحفظ.", "ok")
    return redirect(url_for("users_list"))


@app.route("/users/<int:uid>/password", methods=["POST"])
@admin_only
def user_password(uid):
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if u is None:
        abort(404)
    pw = request.form.get("password", "")
    err = password_error(pw, u["username"])
    if err:
        flash(err, "error")
    else:
        db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_pw(pw), uid))
        audit("user_password", "user", uid, u["username"])
        db.commit()
        if uid == g.user["id"]:
            start_session(db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone())
        flash(f"تم تغيير كلمة مرور {u['username']}.", "ok")
    return redirect(url_for("users_list"))


@app.route("/audit")
@admin_only
def audit_view():
    q = request.args.get("q", "").strip()
    sql, params = "SELECT * FROM audit_log", []
    if q:
        sql += " WHERE username LIKE ? OR details LIKE ? OR action LIKE ?"
        params = [f"%{q}%"] * 3
    rows = get_db().execute(sql + " ORDER BY id DESC LIMIT 300", params).fetchall()
    return render_template("audit.html", rows=rows, q=q)


# ---------- المشاريع ----------

PROJECT_LABELS = {
    "name": "الاسم", "client": "العميل", "location": "الموقع", "area_sqm": "المساحة",
    "status": "الحالة", "markup_pct": "الهامش الافتراضي %", "vat_pct": "الضريبة %", "notes": "الملاحظات", "lang": "لغة العرض",
}


@app.route("/")
def projects_list():
    db = get_db()
    rows = db.execute(
        "SELECT p.*,"
        " (SELECT number FROM quote_revisions r WHERE r.project_id = p.id ORDER BY number DESC LIMIT 1) AS rev_no,"
        " (SELECT status FROM quote_revisions r WHERE r.project_id = p.id ORDER BY number DESC LIMIT 1) AS rev_status,"
        " (SELECT COUNT(*) FROM change_orders c WHERE c.project_id = p.id"
        "  AND c.status IN ('مسودة', 'مُرسلة للعميل')) AS co_open"
        " FROM projects p ORDER BY p.id DESC"
    ).fetchall()
    projects = [(p, project_totals(db, p)) for p in rows]
    return render_template("projects.html", projects=projects)


@app.route("/projects/new", methods=["POST"])
@editor_only
def project_new():
    db = get_db()
    f = request.form
    name = f.get("name", "").strip()
    if not name:
        return redirect(url_for("projects_list"))
    is_admin = g.user["role"] == "admin"
    cur = db.execute(
        "INSERT INTO projects(name, client, location, area_sqm, status, markup_pct, vat_pct, created_at, lang)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (name, f.get("client", "").strip(), f.get("location", "").strip(),
         fnum(f.get("area_sqm")), f.get("status") if f.get("status") in STATUSES else STATUSES[0],
         fnum(f.get("markup_pct"), 15) if is_admin else 15,
         fnum(f.get("vat_pct"), 14) if is_admin else 14, date.today().isoformat(),
         "en" if f.get("lang") == "en" else "ar"),
    )
    audit("project_new", "project", cur.lastrowid, name)
    db.commit()
    return redirect(url_for("project_view", pid=cur.lastrowid))


def get_project(db, pid):
    p = db.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
    if p is None:
        abort(404)
    return p


def live_lines(db, pid):
    return db.execute(
        "SELECT * FROM boq_lines WHERE project_id = ? ORDER BY section, sort, id", (pid,)
    ).fetchall()


def rev_lines(db, rid):
    return db.execute(
        "SELECT * FROM quote_revision_lines WHERE revision_id = ? ORDER BY section, sort, id", (rid,)
    ).fetchall()


def signature(rows, default_markup):
    """بصمة المقايسة: البنود بكمياتها وأسعارها وهامشها الفعلي (عشان أي تغيير في الهامش يتحس)."""
    return sorted(
        (r["section"], r["description"], r["unit"], round(r["qty"], 4), round(r["unit_price"], 4),
         round(line_markup(r, default_markup), 4), rget(r, "description_en") or "")
        for r in rows
    )


@app.route("/projects/<int:pid>")
def project_view(pid):
    db = get_db()
    project = get_project(db, pid)
    lines = live_lines(db, pid)
    sections, section_totals, section_sell = {}, {}, {}
    for ln in lines:
        sec = ln["section"]
        cost = ln["qty"] * ln["unit_price"]
        sections.setdefault(sec, []).append(ln)
        section_totals[sec] = section_totals.get(sec, 0) + cost
        section_sell[sec] = section_sell.get(sec, 0) + cost * (1 + line_markup(ln, project["markup_pct"]) / 100)
    items = db.execute(
        "SELECT i.*, c.name AS category FROM items i JOIN categories c ON c.id = i.category_id"
        " ORDER BY c.sort, i.name"
    ).fetchall()
    t = project_totals(db, project)
    revisions = db.execute(
        "SELECT * FROM quote_revisions WHERE project_id = ? ORDER BY number DESC", (pid,)
    ).fetchall()
    draft_state, delta_total, latest_no = "none", 0, None
    if revisions:
        latest = revisions[0]
        latest_no = latest["number"]
        same = (signature(lines, project["markup_pct"])
                == signature(rev_lines(db, latest["id"]), latest["markup_pct"])
                and abs(project["vat_pct"] - latest["vat_pct"]) < 1e-9)
        draft_state = "same" if same else "changed"
        delta_total = t["total"] - latest["total"]
    cos = [(c, co_totals(db, c))
           for c in db.execute("SELECT * FROM change_orders WHERE project_id = ? ORDER BY number", (pid,))]
    approved_rev = next((r for r in revisions if r["status"] == REV_APPROVED), None)
    original = approved_rev["total"] if approved_rev else t["total"]
    approved_sum = sum(ct["total"] for c, ct in cos if c["status"] == CO_APPROVED)
    contract = {
        "original": original, "from_rev": approved_rev["number"] if approved_rev else None,
        "approved": approved_sum,
        "pending": sum(ct["total"] for c, ct in cos if c["status"] in (CO_DRAFT, CO_SENT)),
        "revised": original + approved_sum,
    }
    return render_template(
        "project.html", p=project, sections=sections, section_totals=section_totals,
        section_sell=section_sell, items=items, t=t, revisions=revisions, draft_state=draft_state,
        delta_total=delta_total, latest_no=latest_no, cos=cos, contract=contract,
    )


@app.route("/projects/<int:pid>/update", methods=["POST"])
@editor_only
def project_update(pid):
    db = get_db()
    old = get_project(db, pid)
    f = request.form
    is_admin = g.user["role"] == "admin"
    new = {
        "name": f.get("name", "").strip() or "بدون اسم",
        "client": f.get("client", "").strip(),
        "location": f.get("location", "").strip(),
        "area_sqm": fnum(f.get("area_sqm")),
        "status": f.get("status") if f.get("status") in STATUSES else old["status"],
        # الهامش والضريبة للمدير فقط
        "markup_pct": fnum(f.get("markup_pct"), 15) if is_admin else old["markup_pct"],
        "vat_pct": fnum(f.get("vat_pct"), 14) if is_admin else old["vat_pct"],
        "notes": f.get("notes", "").strip(),
        "lang": f.get("lang") if f.get("lang") in ("ar", "en") else old["lang"],
    }
    changes = []
    for k, label in PROJECT_LABELS.items():
        if old[k] != new[k]:
            changes.append(f"{label}: اتغيّرت" if k == "notes" else f"{label}: {old[k]} ← {new[k]}")
    db.execute(
        "UPDATE projects SET name=?, client=?, location=?, area_sqm=?, status=?,"
        " markup_pct=?, vat_pct=?, notes=?, lang=? WHERE id=?",
        (new["name"], new["client"], new["location"], new["area_sqm"], new["status"],
         new["markup_pct"], new["vat_pct"], new["notes"], new["lang"], pid),
    )
    if changes:
        audit("project_update", "project", pid, f"{old['name']} | " + "؛ ".join(changes))
    db.commit()
    return redirect(url_for("project_view", pid=pid))


@app.route("/projects/<int:pid>/delete", methods=["POST"])
@admin_only
def project_delete(pid):
    db = get_db()
    p = get_project(db, pid)
    db.execute("DELETE FROM quote_revision_lines WHERE revision_id IN"
               " (SELECT id FROM quote_revisions WHERE project_id = ?)", (pid,))
    db.execute("DELETE FROM quote_revisions WHERE project_id = ?", (pid,))
    db.execute("DELETE FROM change_order_lines WHERE co_id IN"
               " (SELECT id FROM change_orders WHERE project_id = ?)", (pid,))
    db.execute("DELETE FROM change_orders WHERE project_id = ?", (pid,))
    db.execute("DELETE FROM purchase_events WHERE req_id IN"
               " (SELECT id FROM purchase_requests WHERE project_id = ?)", (pid,))
    db.execute("DELETE FROM purchase_requests WHERE project_id = ?", (pid,))
    db.execute("DELETE FROM boq_lines WHERE project_id = ?", (pid,))
    db.execute("DELETE FROM projects WHERE id = ?", (pid,))
    audit("project_delete", "project", pid, p["name"])
    db.commit()
    return redirect(url_for("projects_list"))


@app.route("/projects/<int:pid>/duplicate", methods=["POST"])
@editor_only
def project_duplicate(pid):
    """نسخ مقايسة مشروع كنقطة بداية لمشروع جديد مشابه."""
    db = get_db()
    p = get_project(db, pid)
    cur = db.execute(
        "INSERT INTO projects(name, client, location, area_sqm, status, markup_pct, vat_pct, notes, created_at, lang)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (p["name"] + " (نسخة)", "", "", p["area_sqm"], STATUSES[0], p["markup_pct"],
         p["vat_pct"], p["notes"], date.today().isoformat(), p["lang"]),
    )
    new_id = cur.lastrowid
    db.execute(
        "INSERT INTO boq_lines(project_id, item_id, section, description, description_en, unit, qty,"
        " unit_price, markup_pct, sort)"
        " SELECT ?, item_id, section, description, description_en, unit, qty, unit_price, markup_pct, sort"
        " FROM boq_lines WHERE project_id = ?",
        (new_id, pid),
    )
    audit("project_duplicate", "project", new_id, f"من: {p['name']}")
    db.commit()
    return redirect(url_for("project_view", pid=new_id))


# ---------- بنود المقايسة ----------

@app.route("/projects/<int:pid>/lines/add", methods=["POST"])
@editor_only
def line_add(pid):
    db = get_db()
    p = get_project(db, pid)
    f = request.form
    desc = f.get("description", "").strip()
    if not desc:
        return redirect(url_for("project_view", pid=pid))
    item = db.execute(
        "SELECT i.*, c.name AS category FROM items i JOIN categories c ON c.id = i.category_id"
        " WHERE i.name = ?", (desc,)
    ).fetchone()
    unit = f.get("unit", "").strip() or (item["unit"] if item else "")
    price = fnum(f.get("unit_price"), item["unit_price"] if item else 0)
    section = f.get("section", "").strip() or (item["category"] if item else "أخرى")
    qty = fnum(f.get("qty"), 1)
    # الهامش الخاص بالبند للمدير فقط. فاضي = هامش المشروع الافتراضي
    mk = None
    if g.user["role"] == "admin" and f.get("markup_pct", "").strip() != "":
        mk = max(0.0, fnum(f.get("markup_pct"), 0.0))
    desc_en = f.get("description_en", "").strip()[:300] or (rget(item, "name_en") if item else None)
    db.execute(
        "INSERT INTO boq_lines(project_id, item_id, section, description, description_en, unit, qty,"
        " unit_price, markup_pct) VALUES (?,?,?,?,?,?,?,?,?)",
        (pid, item["id"] if item else None, section, desc, desc_en, unit, qty, price, mk),
    )
    audit("line_add", "project", pid,
          f"{p['name']} | {desc} × {num(qty)} @ {num(price)}" + (f" | الهامش {mk_label(mk)}" if mk is not None else ""))
    db.commit()
    return redirect(url_for("project_view", pid=pid) + "#lines")


@app.route("/lines/<int:lid>/update", methods=["POST"])
@editor_only
def line_update(lid):
    db = get_db()
    ln = db.execute("SELECT * FROM boq_lines WHERE id = ?", (lid,)).fetchone()
    if ln is None:
        abort(404)
    data = request.get_json(silent=True) or {}
    qty = fnum(data.get("qty"), ln["qty"])
    price = fnum(data.get("unit_price"), ln["unit_price"])
    old_mk = ln["markup_pct"]
    new_mk = old_mk
    # الهامش الخاص بالبند للمدير فقط. فاضي = رجوع لهامش المشروع الافتراضي
    if "markup_pct" in data and g.user["role"] == "admin":
        raw = data.get("markup_pct")
        new_mk = None if raw is None or str(raw).strip() == "" else max(0.0, fnum(raw, 0.0))
    # تعديل صياغة البند (عربي / إنجليزي). الوصف العربي مايتفضّاش، والإنجليزي ممكن يتفضّى
    new_desc, new_en = ln["description"], ln["description_en"]
    if "description" in data and str(data.get("description") or "").strip():
        new_desc = str(data["description"]).strip()[:300]
    if "description_en" in data:
        new_en = str(data.get("description_en") or "").strip()[:300] or None
    db.execute("UPDATE boq_lines SET qty = ?, unit_price = ?, markup_pct = ?, description = ?,"
               " description_en = ? WHERE id = ?", (qty, price, new_mk, new_desc, new_en, lid))
    p = get_project(db, ln["project_id"])
    if (qty != ln["qty"] or price != ln["unit_price"] or new_mk != old_mk
            or new_desc != ln["description"] or new_en != ln["description_en"]):
        details = (f"{p['name']} | {ln['description']} | الكمية {num(ln['qty'])} ← {num(qty)}"
                   f" | السعر {num(ln['unit_price'])} ← {num(price)}")
        if new_mk != old_mk:
            details += f" | الهامش {mk_label(old_mk)} ← {mk_label(new_mk)}"
        if new_desc != ln["description"]:
            details += f" | الوصف: {ln['description']} ← {new_desc}"
        if new_en != ln["description_en"]:
            details += f" | English: {ln['description_en'] or '—'} ← {new_en or '—'}"
        audit("line_update", "project", p["id"], details)
    db.commit()
    t = project_totals(db, p)
    sec_rows = db.execute(
        "SELECT qty, unit_price, markup_pct FROM boq_lines WHERE project_id = ? AND section = ?",
        (p["id"], ln["section"]),
    ).fetchall()
    sec_cost = sum(r["qty"] * r["unit_price"] for r in sec_rows)
    sec_sell = sum(r["qty"] * r["unit_price"] * (1 + line_markup(r, p["markup_pct"]) / 100) for r in sec_rows)
    line_sell = qty * price * (1 + line_markup({"markup_pct": new_mk}, p["markup_pct"]) / 100)
    return jsonify(line_total=qty * price, line_sell=line_sell, section_total=sec_cost,
                   section_sell=sec_sell, section=ln["section"], totals=t,
                   description=new_desc, description_en=new_en or "")


@app.route("/lines/<int:lid>/delete", methods=["POST"])
@editor_only
def line_delete(lid):
    db = get_db()
    ln = db.execute("SELECT * FROM boq_lines WHERE id = ?", (lid,)).fetchone()
    if ln is None:
        abort(404)
    p = get_project(db, ln["project_id"])
    db.execute("DELETE FROM boq_lines WHERE id = ?", (lid,))
    audit("line_delete", "project", p["id"], f"{p['name']} | {ln['description']} × {num(ln['qty'])}")
    db.commit()
    return redirect(url_for("project_view", pid=ln["project_id"]) + "#lines")


# ---------- الطباعة ونسخ عرض السعر ----------

def cat_en_map(db):
    return {r["name"]: r["name_en"] for r in db.execute("SELECT name, name_en FROM categories") if r["name_en"]}


def unit_label(u, en):
    return UNIT_EN.get((u or "").strip(), u) if en else u


def company_block(s, en):
    def pick(ar_key, en_key):
        return (s.get(en_key) or s[ar_key]) if en else s[ar_key]
    return {"name": pick("company_name", "company_name_en"),
            "tagline": pick("company_tagline", "company_tagline_en"),
            "address": pick("address", "address_en")}


def default_terms(s, en):
    return (s.get("default_terms_en") or s["default_terms"]) if en else s["default_terms"]


def build_quote(rows, default_markup, vat_pct, lang="ar", sec_en=None):
    """سعر الوحدة للعميل = التكلفة × (1 + هامش البند)، والتكلفة والهامش الحقيقيين مش بيظهروا.
    lang=en: الوصف والقسم والوحدة بالإنجليزي (ولو مفيش ترجمة بيرجع للعربي)."""
    en = lang == "en"
    sec_en = sec_en or {}
    sections = {}
    for r in rows:
        price = r["unit_price"] * (1 + line_markup(r, default_markup) / 100)
        sec, desc = r["section"], r["description"]
        if en:
            sec = rget(r, "section_en") or sec_en.get(sec) or sec
            desc = rget(r, "description_en") or desc
        sections.setdefault(sec, []).append(
            {"description": desc, "unit": unit_label(r["unit"], en), "qty": r["qty"],
             "price": price, "total": price * r["qty"]}
        )
    return sections, calc_totals(rows, default_markup, vat_pct)


@app.route("/projects/<int:pid>/print")
def project_print(pid):
    """طباعة المسودة الحالية (بعلامة مسودة). النسخة الرسمية للعميل بتتطبع من نسخة صادرة."""
    db = get_db()
    p = get_project(db, pid)
    s = load_settings(db)
    en = p["lang"] == "en"
    sections, t = build_quote(live_lines(db, pid), p["markup_pct"], p["vat_pct"], p["lang"], cat_en_map(db))
    header = {"name": p["name"], "client": p["client"], "location": p["location"],
              "area_sqm": p["area_sqm"], "vat_pct": p["vat_pct"]}
    return render_template(
        "print.html", p=header, sections=sections, t=t, terms=p["notes"] or default_terms(s, en),
        qno=f"Q-{pid:04d}", qdate=date.today().isoformat(), draft=True, validity=s["validity_days"],
        en=en, L=LABELS["en" if en else "ar"], cb=company_block(s, en),
    )


@app.route("/projects/<int:pid>/revisions/new", methods=["POST"])
@editor_only
def revision_new(pid):
    db = get_db()
    p = get_project(db, pid)
    rows = live_lines(db, pid)
    if not rows:
        flash("المقايسة فاضية. ضيف بنود الأول وبعدين أصدر نسخة.", "error")
        return redirect(url_for("project_view", pid=pid))
    latest = db.execute(
        "SELECT * FROM quote_revisions WHERE project_id = ? ORDER BY number DESC LIMIT 1", (pid,)
    ).fetchone()
    if latest and signature(rows, p["markup_pct"]) == signature(rev_lines(db, latest["id"]), latest["markup_pct"]) \
            and abs(p["vat_pct"] - latest["vat_pct"]) < 1e-9:
        flash(f"مفيش أي تغيير من آخر نسخة (V{latest['number']}).", "error")
        return redirect(url_for("project_view", pid=pid))
    s = load_settings(db)
    en = p["lang"] == "en"
    terms = p["notes"] or default_terms(s, en)
    sec_en = cat_en_map(db)
    number = (latest["number"] if latest else 0) + 1
    t = calc_totals(rows, p["markup_pct"], p["vat_pct"])
    subtotal = t["subtotal"]
    cur = db.execute(
        "INSERT INTO quote_revisions(project_id, number, created_at, created_by, created_by_name, note,"
        " status, markup_pct, vat_pct, subtotal, total, validity_days, p_name, p_client, p_location,"
        " p_area, p_notes, lang) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, number, now_str(), g.user["id"], g.user["full_name"] or g.user["username"],
         request.form.get("note", "").strip()[:200], REV_STATUSES[0], p["markup_pct"], p["vat_pct"],
         subtotal, t["total"], s["validity_days"], p["name"], p["client"], p["location"],
         p["area_sqm"] or 0, terms, p["lang"]),
    )
    db.executemany(
        "INSERT INTO quote_revision_lines(revision_id, section, description, unit, qty, unit_price,"
        " markup_pct, sort, description_en, section_en, line_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        # الهامش والصياغة (عربي/إنجليزي) بيتثبّتوا في النسخة، فأي تعديل بعد كده مايلمسهاش
        [(cur.lastrowid, r["section"], r["description"], r["unit"], r["qty"], r["unit_price"],
          line_markup(r, p["markup_pct"]), r["sort"], rget(r, "description_en"),
          sec_en.get(r["section"]), r["id"])
         for r in rows],
    )
    audit("revision_new", "project", pid, f"{p['name']} | V{number} | الإجمالي {money(t['total'])}")
    db.commit()
    flash(f"تم إصدار النسخة V{number}. تقدر تطبعها من القايمة.", "ok")
    return redirect(url_for("project_view", pid=pid) + "#revisions")


def get_revision(db, rid):
    r = db.execute("SELECT * FROM quote_revisions WHERE id = ?", (rid,)).fetchone()
    if r is None:
        abort(404)
    return r


@app.route("/revisions/<int:rid>/print")
def revision_print(rid):
    db = get_db()
    rev = get_revision(db, rid)
    s = load_settings(db)
    en = rev["lang"] == "en"
    sections, t = build_quote(rev_lines(db, rid), rev["markup_pct"], rev["vat_pct"], rev["lang"])
    header = {"name": rev["p_name"], "client": rev["p_client"], "location": rev["p_location"],
              "area_sqm": rev["p_area"], "vat_pct": rev["vat_pct"]}
    return render_template(
        "print.html", p=header, sections=sections, t=t, terms=rev["p_notes"],
        qno=f"Q-{rev['project_id']:04d}-V{rev['number']}", qdate=rev["created_at"][:10],
        draft=False, validity=rev["validity_days"],
        en=en, L=LABELS["en" if en else "ar"], cb=company_block(s, en),
    )


@app.route("/revisions/<int:rid>/status", methods=["POST"])
@editor_only
def revision_status(rid):
    db = get_db()
    rev = get_revision(db, rid)
    status = request.form.get("status")
    if status not in REV_STATUSES:
        abort(400)
    if status != rev["status"]:
        db.execute("UPDATE quote_revisions SET status = ? WHERE id = ?", (status, rid))
        extra = ""
        if status == REV_APPROVED:
            # نسخة واحدة بس معتمدة: القديمة بتبقى «مستبدلة»، والمشروع بيتحول لتعاقد
            db.execute(
                "UPDATE quote_revisions SET status = 'مستبدلة' WHERE project_id = ? AND id <> ? AND status = ?",
                (rev["project_id"], rid, REV_APPROVED),
            )
            cur = db.execute("SELECT status FROM projects WHERE id = ?", (rev["project_id"],)).fetchone()
            if cur and cur["status"] == STATUSES[0]:
                db.execute("UPDATE projects SET status = ? WHERE id = ?", (STATUSES[1], rev["project_id"]))
                extra = " | حالة المشروع بقت «تعاقد»"
        audit("revision_status", "project", rev["project_id"],
              f"{rev['p_name']} | V{rev['number']}: {rev['status']} ← {status}{extra}")
        db.commit()
    return redirect(url_for("project_view", pid=rev["project_id"]) + "#revisions")


@app.route("/revisions/<int:rid>/delete", methods=["POST"])
@admin_only
def revision_delete(rid):
    db = get_db()
    rev = get_revision(db, rid)
    if rev["status"] != REV_STATUSES[0]:
        flash("تقدر تمسح النسخة بس لو لسه حالتها «صادرة» (ماتبعتتش للعميل).", "error")
    else:
        db.execute("DELETE FROM quote_revision_lines WHERE revision_id = ?", (rid,))
        db.execute("DELETE FROM quote_revisions WHERE id = ?", (rid,))
        audit("revision_delete", "project", rev["project_id"], f"{rev['p_name']} | V{rev['number']}")
        db.commit()
        flash(f"تم حذف النسخة V{rev['number']}.", "ok")
    return redirect(url_for("project_view", pid=rev["project_id"]) + "#revisions")


def side_data(db, pid, ref):
    if ref == "current":
        p = get_project(db, pid)
        return {"label": "المسودة الحالية",
                "rows": resolve_markup(live_lines(db, pid), p["markup_pct"], live=True),
                "markup": p["markup_pct"], "vat": p["vat_pct"]}
    try:
        rid = int(ref)
    except (TypeError, ValueError):
        abort(404)
    rev = db.execute("SELECT * FROM quote_revisions WHERE id = ? AND project_id = ?", (rid, pid)).fetchone()
    if rev is None:
        abort(404)
    return {"label": f"V{rev['number']}", "rows": resolve_markup(rev_lines(db, rid), rev["markup_pct"]),
            "markup": rev["markup_pct"], "vat": rev["vat_pct"]}


def resolve_markup(rows, default, live=False):
    out = []
    for r in rows:
        d = dict(r)
        d["markup_pct"] = line_markup(r, default)
        d["_lid"] = d["id"] if live else d.get("line_id")
        out.append(d)
    return out


def index_lines(rows):
    """المطابقة بين الجانبين: بالرقم الأصلي للبند لو موجود (فتعديل الصياغة يظهر كتعديل مش حذف وإضافة)،
    وإلا بالقسم والوصف والوحدة."""
    seen, out = {}, {}
    for r in rows:
        if r.get("_lid"):
            out[("id", r["_lid"])] = r
            continue
        k = (r["section"], r["description"], r["unit"])
        n = seen.get(k, 0)
        seen[k] = n + 1
        out[("k", k, n)] = r
    return out


def diff_sides(a, b):
    ia, ib = index_lines(a["rows"]), index_lines(b["rows"])
    out = []
    for key in list(ia) + [k for k in ib if k not in ia]:
        ra, rb = ia.get(key), ib.get(key)
        ref = rb or ra
        old_desc, reworded = None, False
        if ra and not rb:
            status = "removed"
        elif rb and not ra:
            status = "added"
        else:
            reworded = (ra["description"] != rb["description"]
                        or (ra.get("description_en") or "") != (rb.get("description_en") or ""))
            changed = (abs(ra["qty"] - rb["qty"]) > 1e-9 or abs(ra["unit_price"] - rb["unit_price"]) > 1e-9
                       or abs(ra["markup_pct"] - rb["markup_pct"]) > 1e-9 or reworded
                       or ra["unit"] != rb["unit"] or ra["section"] != rb["section"])
            status = "changed" if changed else "same"
            if ra["description"] != rb["description"]:
                old_desc = ra["description"]
        # الإجمالي هنا بسعر العميل قبل الضريبة (بعد هامش البند)
        a_total = ra["qty"] * ra["unit_price"] * (1 + ra["markup_pct"] / 100) if ra else 0.0
        b_total = rb["qty"] * rb["unit_price"] * (1 + rb["markup_pct"] / 100) if rb else 0.0
        out.append({
            "section": ref["section"], "description": ref["description"], "old_description": old_desc,
            "reworded": reworded, "unit": ref["unit"], "status": status,
            "a_qty": ra["qty"] if ra else None, "b_qty": rb["qty"] if rb else None,
            "a_price": ra["unit_price"] if ra else None, "b_price": rb["unit_price"] if rb else None,
            "a_markup": ra["markup_pct"] if ra else None, "b_markup": rb["markup_pct"] if rb else None,
            "a_total": a_total, "b_total": b_total, "diff": b_total - a_total,
        })
    return sorted(out, key=lambda r: r["section"])


@app.route("/projects/<int:pid>/compare")
def compare(pid):
    db = get_db()
    p = get_project(db, pid)
    revs = db.execute(
        "SELECT * FROM quote_revisions WHERE project_id = ? ORDER BY number DESC", (pid,)
    ).fetchall()
    if not revs:
        flash("لسه ماتصدرتش أي نسخة للمقارنة.", "error")
        return redirect(url_for("project_view", pid=pid))
    a_ref = request.args.get("a") or str(revs[0]["id"])
    b_ref = request.args.get("b") or "current"
    a, b = side_data(db, pid, a_ref), side_data(db, pid, b_ref)
    rows = diff_sides(a, b)
    ta = calc_totals(a["rows"], a["markup"], a["vat"])
    tb = calc_totals(b["rows"], b["markup"], b["vat"])
    delta = tb["total"] - ta["total"]
    pct = (delta / ta["total"] * 100) if ta["total"] else None
    show_all = request.args.get("all") == "1"
    refs = [("current", "المسودة الحالية")] + [
        (str(r["id"]), f"V{r['number']} · {r['created_at'][:10]} · {r['status']}") for r in revs
    ]
    counts = {k: sum(1 for r in rows if r["status"] == k) for k in ("added", "removed", "changed", "same")}
    return render_template(
        "compare.html", p=p, a=a, b=b, a_ref=a_ref, b_ref=b_ref, refs=refs, ta=ta, tb=tb,
        delta=delta, pct=pct, rows=rows if show_all else [r for r in rows if r["status"] != "same"],
        show_all=show_all, counts=counts,
    )


# ---------- مكتبة البنود ----------

@app.route("/library")
def library():
    db = get_db()
    cats = db.execute("SELECT * FROM categories ORDER BY sort, name").fetchall()
    items = db.execute(
        "SELECT i.*, c.name AS category FROM items i JOIN categories c ON c.id = i.category_id"
        " ORDER BY c.sort, i.name"
    ).fetchall()
    grouped = {c["name"]: [] for c in cats}
    for it in items:
        grouped[it["category"]].append(it)
    return render_template("library.html", cats=cats, grouped=grouped, cat_by_name={c["name"]: c for c in cats})


@app.route("/library/items/add", methods=["POST"])
@editor_only
def item_add():
    db = get_db()
    f = request.form
    name = f.get("name", "").strip()
    name_en = f.get("name_en", "").strip()[:300] or None
    new_cat = f.get("new_category", "").strip()
    if new_cat:
        db.execute("INSERT OR IGNORE INTO categories(name, sort, name_en) VALUES (?, 99, ?)",
                   (new_cat, f.get("new_category_en", "").strip()[:120] or None))
        cat_id = db.execute("SELECT id FROM categories WHERE name = ?", (new_cat,)).fetchone()[0]
    else:
        cat_id = int(f.get("category_id") or 0)
    if name and cat_id:
        price = fnum(f.get("unit_price"))
        cur = db.execute(
            "INSERT OR IGNORE INTO items(category_id, name, unit, unit_price, name_en) VALUES (?,?,?,?,?)",
            (cat_id, name, f.get("unit", "").strip() or "م2", price, name_en),
        )
        if cur.rowcount:
            audit("item_add", "item", cur.lastrowid, f"{name} @ {num(price)}")
        db.commit()
    return redirect(url_for("library"))


@app.route("/library/items/<int:iid>/update", methods=["POST"])
@admin_only
def item_update(iid):
    db = get_db()
    data = request.get_json(silent=True) or {}
    it = db.execute("SELECT * FROM items WHERE id = ?", (iid,)).fetchone()
    if it is None:
        abort(404)
    unit = (data.get("unit") or it["unit"]).strip()
    price = fnum(data.get("unit_price"), it["unit_price"])
    name_en = it["name_en"]
    if "name_en" in data:
        name_en = str(data.get("name_en") or "").strip()[:300] or None
    db.execute("UPDATE items SET unit = ?, unit_price = ?, name_en = ? WHERE id = ?", (unit, price, name_en, iid))
    if price != it["unit_price"] or unit != it["unit"] or name_en != it["name_en"]:
        details = f"{it['name']} | السعر {num(it['unit_price'])} ← {num(price)} | الوحدة {it['unit']} ← {unit}"
        if name_en != it["name_en"]:
            details += f" | English: {it['name_en'] or '—'} ← {name_en or '—'}"
        audit("item_update", "item", iid, details)
    db.commit()
    return jsonify(ok=True)


@app.route("/library/categories/<int:cid>/update", methods=["POST"])
@admin_only
def category_update(cid):
    """الاسم الإنجليزي للقسم (بيظهر كعنوان القسم في عروض الأسعار الإنجليزي)."""
    db = get_db()
    c = db.execute("SELECT * FROM categories WHERE id = ?", (cid,)).fetchone()
    if c is None:
        abort(404)
    data = request.get_json(silent=True) or {}
    name_en = str(data.get("name_en") or "").strip()[:120] or None
    db.execute("UPDATE categories SET name_en = ? WHERE id = ?", (name_en, cid))
    if name_en != c["name_en"]:
        audit("item_update", "category", cid, f"{c['name']} | English: {c['name_en'] or '—'} ← {name_en or '—'}")
    db.commit()
    return jsonify(ok=True)


@app.route("/library/items/<int:iid>/delete", methods=["POST"])
@admin_only
def item_delete(iid):
    db = get_db()
    it = db.execute("SELECT * FROM items WHERE id = ?", (iid,)).fetchone()
    if it:
        db.execute("DELETE FROM items WHERE id = ?", (iid,))
        audit("item_delete", "item", iid, it["name"])
        db.commit()
    return redirect(url_for("library"))


# ---------- أوامر التغيير (Change Orders) ----------
# أمر التغيير = شغل إضافي أو ملغي بيطلبه العميل بعد التعاقد، بسعره وموافقته، منفصل عن المقايسة الأصلية.
# الكمية السالبة = خصم (عمل ملغي). الدورة: مسودة ← مُرسلة للعميل ← معتمدة / مرفوضة، أو ملغاة.

CO_LABELS = {
    "title": "العنوان", "reason": "الوصف", "requested_by": "طالب التغيير", "request_date": "تاريخ الطلب",
    "time_impact_days": "الأثر على المدة (يوم)", "markup_pct": "الهامش", "vat_pct": "الضريبة",
}


def co_code(co):
    return f"CO-{co['project_id']:04d}-{co['number']:02d}"


app.jinja_env.globals["co_code"] = co_code


def get_co(db, cid):
    co = db.execute("SELECT * FROM change_orders WHERE id = ?", (cid,)).fetchone()
    if co is None:
        abort(404)
    return co


def co_lines(db, cid):
    return db.execute("SELECT * FROM change_order_lines WHERE co_id = ? ORDER BY sort, id", (cid,)).fetchall()


def co_totals(db, co):
    return calc_totals(co_lines(db, co["id"]), co["markup_pct"], co["vat_pct"])


def valid_date(value, default):
    try:
        return date.fromisoformat((value or "").strip()).isoformat()
    except ValueError:
        return default


def require_draft(co):
    if co["status"] != CO_DRAFT:
        abort(409)


@app.errorhandler(409)
def err409(_e):
    return error_response(409, "أمر التغيير مقفول: التعديل متاح وهو في حالة «مسودة» بس.")


@app.route("/change-orders")
def co_list():
    db = get_db()
    status = request.args.get("status", "")
    rows = db.execute(
        "SELECT c.*, p.name AS project_name FROM change_orders c JOIN projects p ON p.id = c.project_id"
        " ORDER BY c.id DESC"
    ).fetchall()
    out, sums = [], {s: 0.0 for s in CO_STATUSES}
    for c in rows:
        t = co_totals(db, c)
        sums[c["status"]] = sums.get(c["status"], 0.0) + t["total"]
        if not status or c["status"] == status:
            out.append((c, t))
    return render_template("change_orders.html", rows=out, status=status, sums=sums)


@app.route("/projects/<int:pid>/change-orders/new", methods=["POST"])
@editor_only
def co_new(pid):
    db = get_db()
    p = get_project(db, pid)
    f = request.form
    title = f.get("title", "").strip()[:200]
    if not title:
        flash("اكتب عنوان لأمر التغيير.", "error")
        return redirect(url_for("project_view", pid=pid) + "#change-orders")
    number = db.execute(
        "SELECT COALESCE(MAX(number), 0) FROM change_orders WHERE project_id = ?", (pid,)
    ).fetchone()[0] + 1
    cur = db.execute(
        "INSERT INTO change_orders(project_id, number, title, reason, requested_by, request_date, status,"
        " markup_pct, vat_pct, time_impact_days, created_at, created_by, created_by_name)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, number, title, f.get("reason", "").strip()[:2000], f.get("requested_by", "").strip()[:120],
         valid_date(f.get("request_date"), date.today().isoformat()), CO_DRAFT,
         p["markup_pct"], p["vat_pct"], 0, now_str(), g.user["id"], g.user["full_name"] or g.user["username"]),
    )
    audit("co_new", "change_order", cur.lastrowid, f"{p['name']} | CO-{number:02d} | {title}")
    db.commit()
    return redirect(url_for("co_view", cid=cur.lastrowid))


@app.route("/change-orders/<int:cid>")
def co_view(cid):
    db = get_db()
    co = get_co(db, cid)
    p = get_project(db, co["project_id"])
    lines = co_lines(db, cid)
    items = db.execute(
        "SELECT i.*, c.name AS category FROM items i JOIN categories c ON c.id = i.category_id"
        " ORDER BY c.sort, i.name"
    ).fetchall()
    return render_template("change_order.html", co=co, p=p, lines=lines, items=items,
                           t=calc_totals(lines, co["markup_pct"], co["vat_pct"]))


@app.route("/change-orders/<int:cid>/update", methods=["POST"])
@editor_only
def co_update(cid):
    db = get_db()
    co = get_co(db, cid)
    require_draft(co)
    f = request.form
    is_admin = g.user["role"] == "admin"
    new = {
        "title": f.get("title", "").strip()[:200] or co["title"],
        "reason": f.get("reason", "").strip()[:2000],
        "requested_by": f.get("requested_by", "").strip()[:120],
        "request_date": valid_date(f.get("request_date"), co["request_date"]),
        "time_impact_days": fnum(f.get("time_impact_days"), 0.0),
        # الهامش والضريبة للمدير فقط
        "markup_pct": max(0.0, fnum(f.get("markup_pct"), co["markup_pct"])) if is_admin else co["markup_pct"],
        "vat_pct": max(0.0, fnum(f.get("vat_pct"), co["vat_pct"])) if is_admin else co["vat_pct"],
    }
    changes = []
    for k, label in CO_LABELS.items():
        if co[k] != new[k]:
            changes.append(f"{label}: اتغيّر" if k == "reason" else f"{label}: {co[k]} ← {new[k]}")
    db.execute(
        "UPDATE change_orders SET title=?, reason=?, requested_by=?, request_date=?, time_impact_days=?,"
        " markup_pct=?, vat_pct=? WHERE id=?",
        (new["title"], new["reason"], new["requested_by"], new["request_date"], new["time_impact_days"],
         new["markup_pct"], new["vat_pct"], cid),
    )
    if changes:
        audit("co_update", "change_order", cid, f"{co_code(co)} | " + "؛ ".join(changes))
    db.commit()
    return redirect(url_for("co_view", cid=cid))


@app.route("/change-orders/<int:cid>/lines/add", methods=["POST"])
@editor_only
def co_line_add(cid):
    db = get_db()
    co = get_co(db, cid)
    require_draft(co)
    f = request.form
    desc = f.get("description", "").strip()[:300]
    if not desc:
        return redirect(url_for("co_view", cid=cid))
    item = db.execute("SELECT * FROM items WHERE name = ?", (desc,)).fetchone()
    unit = f.get("unit", "").strip() or (item["unit"] if item else "")
    price = fnum(f.get("unit_price"), item["unit_price"] if item else 0)
    qty = fnum(f.get("qty"), 1)
    desc_en = f.get("description_en", "").strip()[:300] or (rget(item, "name_en") if item else None)
    mk = None
    if g.user["role"] == "admin" and f.get("markup_pct", "").strip() != "":
        mk = max(0.0, fnum(f.get("markup_pct"), 0.0))
    sort = db.execute("SELECT COALESCE(MAX(sort), 0) FROM change_order_lines WHERE co_id = ?", (cid,)).fetchone()[0] + 1
    db.execute(
        "INSERT INTO change_order_lines(co_id, description, description_en, unit, qty, unit_price, markup_pct, sort)"
        " VALUES (?,?,?,?,?,?,?,?)", (cid, desc, desc_en, unit, qty, price, mk, sort),
    )
    audit("co_line_add", "change_order", cid, f"{co_code(co)} | {desc} × {num(qty)} @ {num(price)}")
    db.commit()
    return redirect(url_for("co_view", cid=cid) + "#lines")


@app.route("/change-order-lines/<int:lid>/update", methods=["POST"])
@editor_only
def co_line_update(lid):
    db = get_db()
    ln = db.execute("SELECT * FROM change_order_lines WHERE id = ?", (lid,)).fetchone()
    if ln is None:
        abort(404)
    co = get_co(db, ln["co_id"])
    require_draft(co)
    data = request.get_json(silent=True) or {}
    qty = fnum(data.get("qty"), ln["qty"])
    price = fnum(data.get("unit_price"), ln["unit_price"])
    old_mk = new_mk = ln["markup_pct"]
    if "markup_pct" in data and g.user["role"] == "admin":
        raw = data.get("markup_pct")
        new_mk = None if raw is None or str(raw).strip() == "" else max(0.0, fnum(raw, 0.0))
    new_desc, new_en = ln["description"], ln["description_en"]
    if "description" in data and str(data.get("description") or "").strip():
        new_desc = str(data["description"]).strip()[:300]
    if "description_en" in data:
        new_en = str(data.get("description_en") or "").strip()[:300] or None
    db.execute(
        "UPDATE change_order_lines SET qty = ?, unit_price = ?, markup_pct = ?, description = ?,"
        " description_en = ? WHERE id = ?", (qty, price, new_mk, new_desc, new_en, lid))
    if (qty != ln["qty"] or price != ln["unit_price"] or new_mk != old_mk
            or new_desc != ln["description"] or new_en != ln["description_en"]):
        details = (f"{co_code(co)} | {ln['description']} | الكمية {num(ln['qty'])} ← {num(qty)}"
                   f" | السعر {num(ln['unit_price'])} ← {num(price)}")
        if new_mk != old_mk:
            details += f" | الهامش {mk_label(old_mk)} ← {mk_label(new_mk)}"
        if new_desc != ln["description"]:
            details += f" | الوصف: {ln['description']} ← {new_desc}"
        if new_en != ln["description_en"]:
            details += f" | English: {ln['description_en'] or '—'} ← {new_en or '—'}"
        audit("co_line_update", "change_order", co["id"], details)
    db.commit()
    line_sell = qty * price * (1 + line_markup({"markup_pct": new_mk}, co["markup_pct"]) / 100)
    return jsonify(line_total=qty * price, line_sell=line_sell, totals=co_totals(db, co),
                   description=new_desc, description_en=new_en or "")


@app.route("/change-order-lines/<int:lid>/delete", methods=["POST"])
@editor_only
def co_line_delete(lid):
    db = get_db()
    ln = db.execute("SELECT * FROM change_order_lines WHERE id = ?", (lid,)).fetchone()
    if ln is None:
        abort(404)
    co = get_co(db, ln["co_id"])
    require_draft(co)
    db.execute("DELETE FROM change_order_lines WHERE id = ?", (lid,))
    audit("co_line_delete", "change_order", co["id"], f"{co_code(co)} | {ln['description']} × {num(ln['qty'])}")
    db.commit()
    return redirect(url_for("co_view", cid=co["id"]) + "#lines")


@app.route("/change-orders/<int:cid>/status", methods=["POST"])
@editor_only
def co_status(cid):
    db = get_db()
    co = get_co(db, cid)
    f = request.form
    new, old = f.get("status"), co["status"]
    if new not in CO_STATUSES:
        abort(400)
    back = redirect(url_for("co_view", cid=cid))
    if new == old:
        return back
    is_admin = g.user["role"] == "admin"
    n_lines = db.execute("SELECT COUNT(*) FROM change_order_lines WHERE co_id = ?", (cid,)).fetchone()[0]

    if new == CO_DRAFT:
        # الرجوع لمسودة: من «مُرسلة» لأي حد، ومن أي حالة تانية (اعتماد/رفض/إلغاء) للمدير بس
        if old != CO_SENT and not is_admin:
            flash("إعادة فتح أمر اتعتمد أو اترفض للمدير فقط.", "error")
            return back
        db.execute("UPDATE change_orders SET status = ?, sent_at = NULL, decided_at = NULL,"
                   " decided_by_client = '', decision_ref = '', decision_note = '' WHERE id = ?", (CO_DRAFT, cid))
        audit("co_status", "change_order", cid, f"{co_code(co)} | {old} ← {new}")
        db.commit()
        flash("اتفتح الأمر تاني كمسودة.", "ok")
        return back

    if old not in (CO_DRAFT, CO_SENT):
        flash("الأمر ده اتقفل. المدير بس يقدر يعيد فتحه.", "error")
        return back
    if new in (CO_SENT, CO_APPROVED) and n_lines == 0:
        flash("ضيف بنود للأمر الأول.", "error")
        return back

    today = date.today().isoformat()
    if new == CO_SENT:
        db.execute("UPDATE change_orders SET status = ?, sent_at = ? WHERE id = ?", (new, now_str(), cid))
        details = f"{co_code(co)} | {old} ← {new}"
    elif new in (CO_APPROVED, CO_REJECTED):
        client_name = f.get("client_name", "").strip()[:120]
        ref = f.get("ref", "").strip()[:200]
        note = f.get("note", "").strip()[:500]
        decided = valid_date(f.get("decision_date"), today)
        db.execute(
            "UPDATE change_orders SET status = ?, decided_at = ?, decided_by_client = ?, decision_ref = ?,"
            " decision_note = ?, sent_at = COALESCE(sent_at, ?) WHERE id = ?",
            (new, decided, client_name, ref, note, now_str(), cid))
        details = f"{co_code(co)} | {old} ← {new} | بتاريخ {decided}"
        if client_name:
            details += f" | العميل: {client_name}"
        if ref:
            details += f" | المرجع: {ref}"
        if new == CO_APPROVED:
            details += f" | الصافي شامل الضريبة {money(co_totals(db, co)['total'])}"
    else:  # ملغاة
        db.execute("UPDATE change_orders SET status = ?, decision_note = ? WHERE id = ?",
                   (new, f.get("note", "").strip()[:500], cid))
        details = f"{co_code(co)} | {old} ← {new}"
    audit("co_status", "change_order", cid, details)
    db.commit()
    return back


@app.route("/change-orders/<int:cid>/delete", methods=["POST"])
@admin_only
def co_delete(cid):
    db = get_db()
    co = get_co(db, cid)
    if co["status"] != CO_DRAFT:
        flash("تقدر تمسح الأمر وهو مسودة بس. غير كده حوّله «ملغاة».", "error")
        return redirect(url_for("co_view", cid=cid))
    db.execute("DELETE FROM change_order_lines WHERE co_id = ?", (cid,))
    db.execute("DELETE FROM change_orders WHERE id = ?", (cid,))
    audit("co_delete", "change_order", cid, f"{co_code(co)} | {co['title']}")
    db.commit()
    return redirect(url_for("project_view", pid=co["project_id"]) + "#change-orders")


@app.route("/change-orders/<int:cid>/print")
def co_print(cid):
    db = get_db()
    co = get_co(db, cid)
    p = get_project(db, co["project_id"])
    s = load_settings(db)
    en = p["lang"] == "en"
    rows, additions, omissions = [], 0.0, 0.0
    for r in co_lines(db, cid):
        price = r["unit_price"] * (1 + line_markup(r, co["markup_pct"]) / 100)
        total = price * r["qty"]
        rows.append({"description": (r["description_en"] if en and r["description_en"] else r["description"]),
                     "unit": unit_label(r["unit"], en), "qty": r["qty"], "price": price, "total": total})
        if total >= 0:
            additions += total
        else:
            omissions += total
    t = co_totals(db, co)
    return render_template(
        "co_print.html", co=co, p=p, rows=rows, t=t, additions=additions, omissions=omissions,
        code=co_code(co), draft=co["status"] == CO_DRAFT,
        en=en, L=LABELS["en" if en else "ar"], cb=company_block(s, en),
    )


# ---------- النسخ الاحتياطي ----------

BACKUP_RE = re.compile(r"^boq-backup-\d{8}-\d{6}\.zip$")
_backup_lock = threading.Lock()


def list_backups():
    if not os.path.isdir(BACKUP_DIR):
        return []
    out = []
    for name in os.listdir(BACKUP_DIR):
        if BACKUP_RE.match(name):
            st = os.stat(os.path.join(BACKUP_DIR, name))
            out.append({"name": name, "size": st.st_size, "mtime": datetime.fromtimestamp(st.st_mtime)})
    return sorted(out, key=lambda f: f["name"], reverse=True)


def make_backup():
    """نسخة متسقة من قاعدة البيانات (حتى والبرنامج شغال) + اللوجو، في ملف zip واحد."""
    with _backup_lock:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        tmp_db = os.path.join(BACKUP_DIR, f".tmp-{stamp}.db")
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(tmp_db)
        try:
            src.backup(dst)
            check = dst.execute("PRAGMA integrity_check").fetchone()[0]
            if check != "ok":
                raise RuntimeError(f"فحص سلامة النسخة فشل: {check}")
        finally:
            src.close()
            dst.close()
        final = os.path.join(BACKUP_DIR, f"boq-backup-{stamp}.zip")
        try:
            with zipfile.ZipFile(final + ".part", "w", zipfile.ZIP_DEFLATED) as z:
                z.write(tmp_db, "boq.db")
                if os.path.isdir(UPLOAD_DIR):
                    for fn in os.listdir(UPLOAD_DIR):
                        z.write(os.path.join(UPLOAD_DIR, fn), "uploads/" + fn)
            os.replace(final + ".part", final)
        finally:
            for leftover in (tmp_db, final + ".part"):
                if os.path.exists(leftover):
                    os.remove(leftover)
        for old in list_backups()[BACKUP_KEEP:]:
            try:
                os.remove(os.path.join(BACKUP_DIR, old["name"]))
            except OSError:
                pass
        return final


def set_status_kv(key, value):
    c = sqlite3.connect(DB_PATH)
    set_setting(c, key, value)
    c.commit()
    c.close()


def run_backup():
    try:
        path = make_backup()
        set_status_kv("last_backup_error", "")
        return path, None
    except Exception as exc:  # noqa: BLE001 - لازم نسجل أي سبب
        app.logger.exception("backup failed")
        set_status_kv("last_backup_error", f"{type(exc).__name__}: {exc}"[:300])
        return None, exc


def backup_due():
    now = datetime.now()
    if now.hour < BACKUP_HOUR:
        return False
    today = now.strftime("%Y%m%d")
    return not any(f["name"].startswith(f"boq-backup-{today}") for f in list_backups())


def backup_loop():
    while True:
        failed = False
        try:
            if backup_due():
                failed = run_backup()[1] is not None
        except Exception:  # noqa: BLE001
            app.logger.exception("backup loop error")
            failed = True
        time.sleep(3600 if failed else 600)


def start_backup_thread():
    if os.environ.get("BOQ_DISABLE_BACKUP") == "1":
        return
    threading.Thread(target=backup_loop, name="backup", daemon=True).start()


def backup_alert(db):
    err = db.execute("SELECT value FROM settings WHERE key = 'last_backup_error'").fetchone()
    if err and err["value"]:
        return "آخر نسخة احتياطية فشلت: " + err["value"]
    files = list_backups()
    if not files:
        return "لسه مفيش أي نسخة احتياطية. خد نسخة دلوقتي من صفحة «النسخ الاحتياطي»."
    age = datetime.now() - files[0]["mtime"]
    if age > timedelta(days=3):
        return f"آخر نسخة احتياطية من {age.days} يوم."
    return ""


@app.route("/backups")
@admin_only
def backups():
    return render_template(
        "backups.html", files=list_backups(), folder=BACKUP_DIR, keep=BACKUP_KEEP, hour=BACKUP_HOUR,
        auto=os.environ.get("BOQ_DISABLE_BACKUP") != "1",
    )


@app.route("/backups/run", methods=["POST"])
@admin_only
def backup_run():
    path, exc = run_backup()
    if exc:
        flash(f"فشلت النسخة الاحتياطية: {exc}", "error")
    else:
        audit("backup_manual", "backup", None, os.path.basename(path))
        get_db().commit()
        flash("تم أخذ نسخة احتياطية.", "ok")
    return redirect(url_for("backups"))


@app.route("/backups/download/<name>")
@admin_only
def backup_download(name):
    if not BACKUP_RE.match(name) or not os.path.exists(os.path.join(BACKUP_DIR, name)):
        abort(404)
    audit("backup_download", "backup", None, name)
    get_db().commit()
    return send_from_directory(BACKUP_DIR, name, as_attachment=True)


# ---------- المشتريات: طلبات المواد والعينات ----------

PR_KINDS = ["مواد", "عينة"]
PR_NEW, PR_PROC, PR_ORDERED, PR_TRANSIT, PR_ARRIVED = (
    "جديد", "قيد التوريد", "تم الطلب من المورد", "في الطريق", "وصل الموقع")
PR_RECEIVED, PR_APPROVED, PR_RETURNED, PR_CANCELLED = "تم الاستلام", "معتمدة", "مرتجع", "ملغي"
PR_STATUSES = [PR_NEW, PR_PROC, PR_ORDERED, PR_TRANSIT, PR_ARRIVED, PR_RECEIVED, PR_APPROVED, PR_RETURNED, PR_CANCELLED]
PR_STEPS = [PR_PROC, PR_ORDERED, PR_TRANSIT, PR_ARRIVED]           # اللي المشتريات تحدّثها
PR_DONE = (PR_RECEIVED, PR_APPROVED, PR_CANCELLED)                   # حالات نهائية
PR_PRIORITIES = ["عادي", "مستعجل"]
PR_FILTERS = {"open": "المفتوحة", "new": "جديدة", "progress": "جاري التوريد", "decide": "منتظرة قرار المهندس",
              "returned": "مرتجعة", "late": "متأخرة", "done": "المنتهية", "all": "الكل"}


def pr_code(r):
    return f"PR-{r['project_id']:04d}-{r['number']:03d}"


app.jinja_env.globals["pr_code"] = pr_code


def pr_is_late(r, today=None):
    today = today or date.today().isoformat()
    return bool(r["needed_by"]) and r["needed_by"] < today and r["status"] not in (PR_ARRIVED,) + PR_DONE


def pr_match(r, f, today):
    s = r["status"]
    if f == "all":
        return True
    if f == "open":
        return s not in PR_DONE
    if f == "new":
        return s == PR_NEW
    if f == "progress":
        return s in (PR_PROC, PR_ORDERED, PR_TRANSIT)
    if f == "decide":
        return s == PR_ARRIVED
    if f == "returned":
        return s == PR_RETURNED
    if f == "late":
        return pr_is_late(r, today)
    if f == "done":
        return s in PR_DONE
    return s not in PR_DONE


def pr_done_label(kind):
    return PR_APPROVED if kind == "عينة" else PR_RECEIVED


def pr_event(db, rid, status, note):
    u = g.user
    db.execute(
        "INSERT INTO purchase_events(req_id, ts, user_name, role, status, note) VALUES (?,?,?,?,?,?)",
        (rid, now_str(), u["full_name"] or u["username"], u["role"], status, note[:500]))


def get_pr(db, rid):
    r = db.execute(
        "SELECT r.*, p.name AS pname, p.location AS plocation FROM purchase_requests r"
        " JOIN projects p ON p.id = r.project_id WHERE r.id = ?", (rid,)).fetchone()
    if r is None:
        abort(404)
    return r


def pr_badge_count(db, u):
    if not u:
        return 0
    if u["role"] == "engineer" or u["role"] == "admin":
        mine = db.execute(
            "SELECT COUNT(*) FROM purchase_requests WHERE requested_by = ? AND (status = ? OR unseen = 1)",
            (u["id"], PR_ARRIVED)).fetchone()[0]
        if u["role"] == "engineer":
            return mine
        return mine + db.execute("SELECT COUNT(*) FROM purchase_requests WHERE status IN (?,?)",
                                 (PR_NEW, PR_RETURNED)).fetchone()[0]
    if u["role"] == "purchasing":
        return db.execute("SELECT COUNT(*) FROM purchase_requests WHERE status IN (?,?)",
                          (PR_NEW, PR_RETURNED)).fetchone()[0]
    return 0


purchasing_only = roles_required("admin", "purchasing")


@app.route("/purchasing")
def pr_list():
    db = get_db()
    u = g.user
    f = request.args.get("f", "open")
    if f not in PR_FILTERS:
        f = "open"
    kind = request.args.get("kind", "")
    if kind not in PR_KINDS:
        kind = ""
    mine_arg = request.args.get("mine")
    mine = (mine_arg == "1") if mine_arg is not None else (u["role"] == "engineer")
    pid = request.args.get("project", type=int)
    sql = ("SELECT r.*, p.name AS pname, p.location AS plocation FROM purchase_requests r"
           " JOIN projects p ON p.id = r.project_id WHERE 1=1")
    params = []
    if pid:
        sql += " AND r.project_id = ?"; params.append(pid)
    if kind:
        sql += " AND r.kind = ?"; params.append(kind)
    if mine:
        sql += " AND r.requested_by = ?"; params.append(u["id"])
    rows = db.execute(sql, params).fetchall()
    today = date.today().isoformat()
    counts = {k: sum(1 for r in rows if pr_match(r, k, today)) for k in PR_FILTERS}
    shown = [r for r in rows if pr_match(r, f, today)]
    shown.sort(key=lambda r: (r["pname"], r["priority"] != "مستعجل", r["needed_by"] or "9999", r["id"]))
    groups = {}
    for r in shown:
        groups.setdefault((r["project_id"], r["pname"], r["plocation"]), []).append(r)
    projects = db.execute("SELECT id, name FROM projects ORDER BY name").fetchall()
    unseen = [r for r in rows if r["requested_by"] == u["id"] and r["unseen"]]
    return render_template(
        "pr_list.html", groups=list(groups.items()), counts=counts, f=f, kind=kind, mine=mine, pid=pid,
        projects=projects, filters=PR_FILTERS, today=today, unseen=len(unseen), total=len(shown))


@app.route("/purchasing/new", methods=["GET", "POST"])
@editor_only
def pr_new():
    db = get_db()
    pid = request.values.get("project", type=int) or request.values.get("project_id", type=int)
    projects = db.execute("SELECT id, name FROM projects ORDER BY name").fetchall()
    p = db.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone() if pid else None
    if request.method == "POST" and p is not None:
        f = request.form
        desc = f.get("description", "").strip()
        kind = f.get("kind") if f.get("kind") in PR_KINDS else "مواد"
        qty = fnum(f.get("qty"), 0)
        if not desc or qty <= 0:
            flash("اكتب وصف الطلب وكمية أكبر من صفر.", "error")
        else:
            lid = f.get("boq_line_id", type=int)
            if lid and not db.execute("SELECT 1 FROM boq_lines WHERE id = ? AND project_id = ?", (lid, pid)).fetchone():
                lid = None
            number = (db.execute("SELECT MAX(number) FROM purchase_requests WHERE project_id = ?", (pid,)).fetchone()[0] or 0) + 1
            u = g.user
            cur = db.execute(
                "INSERT INTO purchase_requests(project_id, number, kind, description, unit, qty, boq_line_id, needed_by,"
                " priority, status, notes, requested_by, requested_by_name, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pid, number, kind, desc[:300], f.get("unit", "").strip()[:30], qty, lid,
                 valid_date(f.get("needed_by"), ""), f.get("priority") if f.get("priority") in PR_PRIORITIES else "عادي",
                 PR_NEW, f.get("notes", "").strip()[:1000], u["id"], u["full_name"] or u["username"], now_str(), now_str()))
            rid = cur.lastrowid
            pr_event(db, rid, PR_NEW, f"طلب {kind}: {desc[:80]}")
            audit("pr_new", "purchase", rid, f"{p['name']} | {kind}: {desc[:60]} × {qty:g}")
            db.commit()
            return redirect(url_for("pr_view", rid=rid))
    lines = live_lines(db, pid) if p else []
    return render_template("pr_new.html", p=p, projects=projects, lines=lines, kinds=PR_KINDS,
                           priorities=PR_PRIORITIES, f=request.form if request.method == "POST" else {})


@app.route("/purchasing/<int:rid>")
def pr_view(rid):
    db = get_db()
    r = get_pr(db, rid)
    u = g.user
    if r["unseen"] and r["requested_by"] == u["id"]:
        db.execute("UPDATE purchase_requests SET unseen = 0 WHERE id = ?", (rid,))
        db.commit()
    events = db.execute("SELECT * FROM purchase_events WHERE req_id = ? ORDER BY id DESC", (rid,)).fetchall()
    return render_template("pr_view.html", r=r, events=events, steps=PR_STEPS, kinds=PR_KINDS,
                           priorities=PR_PRIORITIES, late=pr_is_late(r), done_label=pr_done_label(r["kind"]))


@app.route("/purchasing/<int:rid>/update", methods=["POST"])
@purchasing_only
def pr_update(rid):
    """المشتريات: تحديث الحالة والمورد وتاريخ التوريد المتوقع."""
    db = get_db()
    r = get_pr(db, rid)
    f = request.form
    if r["status"] in PR_DONE:
        abort(409)
    new = f.get("status", "").strip()
    note = f.get("note", "").strip()
    is_admin = g.user["role"] == "admin"
    if new and new != r["status"]:
        if new not in PR_STEPS:
            abort(400)
        if r["status"] == PR_ARRIVED and not is_admin:
            abort(409)          # بعد الوصول القرار للمهندس
    else:
        new = r["status"]
    supplier = f.get("supplier", "").strip()[:120]
    expected = valid_date(f.get("expected_date"), "")
    changed = (new != r["status"] or supplier != (r["supplier"] or "") or expected != (r["expected_date"] or "") or note)
    if not changed:
        return redirect(url_for("pr_view", rid=rid))
    unseen = 1 if g.user["id"] != r["requested_by"] else r["unseen"]
    db.execute("UPDATE purchase_requests SET status=?, supplier=?, expected_date=?, updated_at=?, unseen=? WHERE id=?",
               (new, supplier, expected, now_str(), unseen, rid))
    extra = []
    if supplier != (r["supplier"] or ""):
        extra.append(f"المورد: {supplier or '—'}")
    if expected != (r["expected_date"] or ""):
        extra.append(f"التوريد المتوقع: {expected or '—'}")
    pr_event(db, rid, new, " · ".join(extra + ([note] if note else [])))
    audit("pr_update", "purchase", rid, f"{pr_code(r)}: {r['status']} ← {new}" if new != r["status"] else pr_code(r))
    db.commit()
    return redirect(url_for("pr_view", rid=rid))


@app.route("/purchasing/<int:rid>/decide", methods=["POST"])
@editor_only
def pr_decide(rid):
    """المهندس: اعتماد/استلام، أو إرجاع، أو إلغاء."""
    db = get_db()
    r = get_pr(db, rid)
    action = request.form.get("action")
    note = request.form.get("note", "").strip()
    if r["status"] in PR_DONE:
        abort(409)
    if action == "accept":
        if r["status"] != PR_ARRIVED:
            abort(409)
        new = pr_done_label(r["kind"])
    elif action == "return":
        if r["status"] != PR_ARRIVED:
            abort(409)
        if not note:
            flash("اكتب سبب الإرجاع علشان المشتريات يعرفوا المطلوب.", "error")
            return redirect(url_for("pr_view", rid=rid))
        new = PR_RETURNED
    elif action == "cancel":
        new = PR_CANCELLED
    else:
        abort(400)
    db.execute("UPDATE purchase_requests SET status=?, updated_at=? WHERE id=?", (new, now_str(), rid))
    pr_event(db, rid, new, note)
    audit("pr_status", "purchase", rid, f"{pr_code(r)}: {r['status']} ← {new}")
    db.commit()
    return redirect(url_for("pr_view", rid=rid))


@app.route("/purchasing/<int:rid>/edit", methods=["POST"])
@editor_only
def pr_edit(rid):
    db = get_db()
    r = get_pr(db, rid)
    if r["status"] != PR_NEW and g.user["role"] != "admin":
        abort(409)
    if r["status"] in PR_DONE:
        abort(409)
    f = request.form
    desc = f.get("description", "").strip() or r["description"]
    qty = fnum(f.get("qty"), r["qty"])
    if qty <= 0:
        qty = r["qty"]
    db.execute(
        "UPDATE purchase_requests SET description=?, unit=?, qty=?, needed_by=?, priority=?, notes=?, updated_at=? WHERE id=?",
        (desc[:300], f.get("unit", "").strip()[:30], qty, valid_date(f.get("needed_by"), ""),
         f.get("priority") if f.get("priority") in PR_PRIORITIES else r["priority"], f.get("notes", "").strip()[:1000],
         now_str(), rid))
    pr_event(db, rid, r["status"], "تعديل بيانات الطلب")
    audit("pr_edit", "purchase", rid, pr_code(r))
    db.commit()
    return redirect(url_for("pr_view", rid=rid))


@app.route("/purchasing/<int:rid>/delete", methods=["POST"])
@admin_only
def pr_delete(rid):
    db = get_db()
    r = get_pr(db, rid)
    if r["status"] not in (PR_NEW, PR_CANCELLED):
        abort(409)
    db.execute("DELETE FROM purchase_events WHERE req_id = ?", (rid,))
    db.execute("DELETE FROM purchase_requests WHERE id = ?", (rid,))
    audit("pr_delete", "purchase", rid, f"{pr_code(r)}: {r['description'][:60]}")
    db.commit()
    return redirect(url_for("pr_list"))


# ---------- العهد وتصفيتها ----------

CU_OPEN, CU_SUBMITTED, CU_SETTLED = "مفتوحة", "مقدّمة للتصفية", "تمت التصفية"
CU_STATUSES = [CU_OPEN, CU_SUBMITTED, CU_SETTLED]
CU_RECEIPTS = ["فاتورة", "إيصال", "بدون مستند"]
CU_DEFAULT_CATEGORIES = ["أجور عمال", "مشتريات ومستلزمات", "مواصلات وانتقالات", "وجبات وضيافة", "إكراميات", "اتصالات وكروت شحن", "أخرى"]
accounts_only = roles_required("admin", "accountant")


def cu_code(c):
    return f"CU-{c['number']:04d}"


app.jinja_env.globals["cu_code"] = cu_code


def seed_custody_categories(db):
    if db.execute("SELECT COUNT(*) FROM custody_categories").fetchone()[0] == 0:
        for i, n in enumerate(CU_DEFAULT_CATEGORIES):
            db.execute("INSERT INTO custody_categories(name, sort, active) VALUES (?,?,1)", (n, i))
        db.commit()


def get_cu(db, cid):
    c = db.execute(
        "SELECT c.*, p.name AS pname FROM custodies c LEFT JOIN projects p ON p.id = c.project_id WHERE c.id = ?",
        (cid,)).fetchone()
    if c is None:
        abort(404)
    u = g.user
    if u["role"] == "viewer" or (u["role"] == "engineer" and c["holder_id"] != u["id"]):
        abort(403)
    return c


def cu_event(db, cid, action, note=""):
    u = g.user
    db.execute("INSERT INTO custody_events(custody_id, ts, user_name, action, note) VALUES (?,?,?,?,?)",
               (cid, now_str(), u["full_name"] or u["username"], action, note[:500]))


def cu_totals(db, c):
    rec = db.execute("SELECT COALESCE(SUM(amount),0) FROM custody_advances WHERE custody_id = ?", (c["id"],)).fetchone()[0]
    sp = db.execute("SELECT COALESCE(SUM(amount),0) FROM custody_expenses WHERE custody_id = ? AND disallowed = 0",
                    (c["id"],)).fetchone()[0]
    dis = db.execute("SELECT COALESCE(SUM(amount),0) FROM custody_expenses WHERE custody_id = ? AND disallowed = 1",
                     (c["id"],)).fetchone()[0]
    rec, sp, dis = round(rec, 2), round(sp, 2), round(dis, 2)
    return {"received": rec, "spent": sp, "disallowed": dis, "balance": round(rec - sp, 2)}


def cu_groups(db, cid):
    """البنود الثابتة (الأقسام) وتحتها مصروفاتها المتغيرة، مع إجمالي كل قسم."""
    exps = db.execute(
        "SELECT e.*, k.name AS cname FROM custody_expenses e JOIN custody_categories k ON k.id = e.category_id"
        " WHERE e.custody_id = ? ORDER BY k.sort, k.id, e.date, e.id", (cid,)).fetchall()
    groups = {}
    for e in exps:
        gp = groups.setdefault(e["category_id"], {"name": e["cname"], "rows": [], "total": 0.0})
        gp["rows"].append(e)
        if not e["disallowed"]:
            gp["total"] += e["amount"]
    for gp in groups.values():
        gp["total"] = round(gp["total"], 2)
    return list(groups.values())


def cu_can_work(c):
    """صاحب العهدة (أو المدير) يضيف ويعدّل المصروفات طول ما العهدة مفتوحة."""
    u = g.user
    return c["status"] == CU_OPEN and (u["role"] == "admin" or (u["role"] == "engineer" and c["holder_id"] == u["id"]))


def cu_badge_count(db, u):
    if not u:
        return 0
    if u["role"] in ("admin", "accountant"):
        return db.execute("SELECT COUNT(*) FROM custodies WHERE status = ?", (CU_SUBMITTED,)).fetchone()[0]
    return 0


def parse_amount(v):
    a = round(fnum(v, 0), 2)
    return a if a > 0 else None


@app.route("/custody")
def cu_list():
    db = get_db()
    u = g.user
    if u["role"] == "viewer":
        abort(403)
    f = request.args.get("f", "open")
    sql = ("SELECT c.*, p.name AS pname FROM custodies c LEFT JOIN projects p ON p.id = c.project_id WHERE 1=1")
    params = []
    if u["role"] == "engineer":
        sql += " AND c.holder_id = ?"; params.append(u["id"])
    rows = db.execute(sql + " ORDER BY c.id DESC", params).fetchall()
    items = []
    for c in rows:
        t = cu_totals(db, c)
        items.append((c, t))
    counts = {"open": sum(1 for c, _ in items if c["status"] != CU_SETTLED),
              "submitted": sum(1 for c, _ in items if c["status"] == CU_SUBMITTED),
              "settled": sum(1 for c, _ in items if c["status"] == CU_SETTLED), "all": len(items)}
    show = {"open": lambda c: c["status"] != CU_SETTLED, "submitted": lambda c: c["status"] == CU_SUBMITTED,
            "settled": lambda c: c["status"] == CU_SETTLED, "all": lambda c: True}.get(f, lambda c: True)
    items = [(c, t) for c, t in items if show(c)]
    holders = db.execute("SELECT id, username, full_name FROM users WHERE active = 1 AND role IN ('engineer','admin')"
                         " ORDER BY full_name, username").fetchall()
    projects = db.execute("SELECT id, name FROM projects ORDER BY name").fetchall()
    outstanding = round(sum(t["balance"] for c, t in items if c["status"] != CU_SETTLED), 2)
    return render_template("custody_list.html", items=items, f=f, counts=counts, holders=holders, projects=projects,
                           outstanding=outstanding)


@app.route("/custody/new", methods=["POST"])
@accounts_only
def cu_new():
    db = get_db()
    f = request.form
    holder = db.execute("SELECT * FROM users WHERE id = ? AND active = 1 AND role IN ('engineer','admin')",
                        (f.get("holder_id", type=int),)).fetchone()
    amount = parse_amount(f.get("amount"))
    if holder is None or amount is None:
        flash("اختار صاحب العهدة واكتب مبلغ أكبر من صفر.", "error")
        return redirect(url_for("cu_list"))
    pid = f.get("project_id", type=int)
    if pid and not db.execute("SELECT 1 FROM projects WHERE id = ?", (pid,)).fetchone():
        pid = None
    number = (db.execute("SELECT MAX(number) FROM custodies").fetchone()[0] or 0) + 1
    day = valid_date(f.get("date"), date.today().isoformat())
    cur = db.execute(
        "INSERT INTO custodies(number, holder_id, holder_name, project_id, purpose, status, created_at, created_by_name)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (number, holder["id"], holder["full_name"] or holder["username"], pid, f.get("purpose", "").strip()[:200],
         CU_OPEN, now_str(), g.user["full_name"] or g.user["username"]))
    cid = cur.lastrowid
    db.execute("INSERT INTO custody_advances(custody_id, date, amount, note) VALUES (?,?,?,?)",
               (cid, day, amount, "العهدة الأساسية"))
    cu_event(db, cid, "إنشاء العهدة", f"{amount:,.2f} لـ {holder['full_name'] or holder['username']}")
    audit("cu_new", "custody", cid, f"CU-{number:04d} | {holder['username']} | {amount:,.2f}")
    db.commit()
    return redirect(url_for("cu_view", cid=cid))


@app.route("/custody/<int:cid>")
def cu_view(cid):
    db = get_db()
    c = get_cu(db, cid)
    advances = db.execute("SELECT * FROM custody_advances WHERE custody_id = ? ORDER BY date, id", (cid,)).fetchall()
    events = db.execute("SELECT * FROM custody_events WHERE custody_id = ? ORDER BY id DESC", (cid,)).fetchall()
    cats = db.execute("SELECT * FROM custody_categories WHERE active = 1 ORDER BY sort, id").fetchall()
    return render_template(
        "custody_view.html", c=c, t=cu_totals(db, c), groups=cu_groups(db, cid), advances=advances, events=events,
        cats=cats, receipts=CU_RECEIPTS, can_work=cu_can_work(c), today=date.today().isoformat(),
        is_accounts=g.user["role"] in ("admin", "accountant"))


@app.route("/custody/<int:cid>/advance", methods=["POST"])
@accounts_only
def cu_advance(cid):
    db = get_db()
    c = get_cu(db, cid)
    if c["status"] == CU_SETTLED:
        abort(409)
    amount = parse_amount(request.form.get("amount"))
    if amount is None:
        flash("اكتب مبلغ أكبر من صفر.", "error")
        return redirect(url_for("cu_view", cid=cid))
    db.execute("INSERT INTO custody_advances(custody_id, date, amount, note) VALUES (?,?,?,?)",
               (cid, valid_date(request.form.get("date"), date.today().isoformat()), amount,
                request.form.get("note", "").strip()[:200]))
    cu_event(db, cid, "إضافة مبلغ للعهدة", f"{amount:,.2f}")
    audit("cu_advance", "custody", cid, f"{cu_code(c)}: +{amount:,.2f}")
    db.commit()
    return redirect(url_for("cu_view", cid=cid))


def _exp_fields(db, f):
    cat = db.execute("SELECT * FROM custody_categories WHERE id = ?", (f.get("category_id", type=int),)).fetchone()
    amount = parse_amount(f.get("amount"))
    desc = f.get("description", "").strip()[:200]
    receipt = f.get("receipt") if f.get("receipt") in CU_RECEIPTS else "بدون مستند"
    return cat, amount, desc, receipt


@app.route("/custody/<int:cid>/expense/add", methods=["POST"])
@editor_only
def cu_expense_add(cid):
    db = get_db()
    c = get_cu(db, cid)
    if not cu_can_work(c):
        abort(409)
    f = request.form
    cat, amount, desc, receipt = _exp_fields(db, f)
    if cat is None or not cat["active"] or amount is None or not desc:
        flash("اختار البند واكتب البيان ومبلغ أكبر من صفر.", "error")
        return redirect(url_for("cu_view", cid=cid))
    cur = db.execute(
        "INSERT INTO custody_expenses(custody_id, date, category_id, description, amount, receipt, receipt_no)"
        " VALUES (?,?,?,?,?,?,?)",
        (cid, valid_date(f.get("date"), date.today().isoformat()), cat["id"], desc, amount, receipt,
         f.get("receipt_no", "").strip()[:40]))
    audit("cu_expense", "custody", cid, f"{cu_code(c)}: {cat['name']} - {desc[:40]} - {amount:,.2f}")
    db.commit()
    return redirect(url_for("cu_view", cid=cid) + "#exp")


def get_exp(db, eid):
    e = db.execute("SELECT * FROM custody_expenses WHERE id = ?", (eid,)).fetchone()
    if e is None:
        abort(404)
    return e, get_cu(db, e["custody_id"])


@app.route("/custody/expense/<int:eid>/update", methods=["POST"])
@editor_only
def cu_expense_update(eid):
    db = get_db()
    e, c = get_exp(db, eid)
    if not cu_can_work(c):
        abort(409)
    f = request.form
    cat, amount, desc, receipt = _exp_fields(db, f)
    if cat is None or amount is None or not desc:
        flash("البيانات ناقصة: البند والبيان ومبلغ أكبر من صفر.", "error")
        return redirect(url_for("cu_view", cid=c["id"]))
    db.execute("UPDATE custody_expenses SET date=?, category_id=?, description=?, amount=?, receipt=?, receipt_no=?,"
               " disallowed=0, disallow_reason='' WHERE id=?",
               (valid_date(f.get("date"), e["date"]), cat["id"], desc, amount, receipt,
                f.get("receipt_no", "").strip()[:40], eid))
    audit("cu_expense_edit", "custody", c["id"], f"{cu_code(c)}: {e['description'][:30]} {e['amount']:,.2f} ← {amount:,.2f}")
    db.commit()
    return redirect(url_for("cu_view", cid=c["id"]) + "#exp")


@app.route("/custody/expense/<int:eid>/delete", methods=["POST"])
@editor_only
def cu_expense_delete(eid):
    db = get_db()
    e, c = get_exp(db, eid)
    if not cu_can_work(c):
        abort(409)
    db.execute("DELETE FROM custody_expenses WHERE id = ?", (eid,))
    audit("cu_expense_delete", "custody", c["id"], f"{cu_code(c)}: {e['description'][:40]} {e['amount']:,.2f}")
    db.commit()
    return redirect(url_for("cu_view", cid=c["id"]) + "#exp")


@app.route("/custody/expense/<int:eid>/disallow", methods=["POST"])
@accounts_only
def cu_expense_disallow(eid):
    """الحسابات بتستبعد بند وقت المراجعة (بيتحمّله صاحب العهدة)، أو بترجّعه."""
    db = get_db()
    e, c = get_exp(db, eid)
    if c["status"] != CU_SUBMITTED:
        abort(409)
    if e["disallowed"]:
        db.execute("UPDATE custody_expenses SET disallowed = 0, disallow_reason = '' WHERE id = ?", (eid,))
        cu_event(db, c["id"], "إعادة قبول بند", e["description"][:60])
    else:
        reason = request.form.get("reason", "").strip()
        if not reason:
            flash("اكتب سبب استبعاد البند.", "error")
            return redirect(url_for("cu_view", cid=c["id"]))
        db.execute("UPDATE custody_expenses SET disallowed = 1, disallow_reason = ? WHERE id = ?", (reason[:200], eid))
        cu_event(db, c["id"], "استبعاد بند", f"{e['description'][:40]} ({e['amount']:,.2f}): {reason}")
    audit("cu_disallow", "custody", c["id"], f"{cu_code(c)}: {e['description'][:40]}")
    db.commit()
    return redirect(url_for("cu_view", cid=c["id"]) + "#exp")


@app.route("/custody/<int:cid>/submit", methods=["POST"])
@editor_only
def cu_submit(cid):
    db = get_db()
    c = get_cu(db, cid)
    if not cu_can_work(c):
        abort(409)
    if db.execute("SELECT COUNT(*) FROM custody_expenses WHERE custody_id = ?", (cid,)).fetchone()[0] == 0:
        flash("مفيش مصروفات مسجلة. سجّل المصروفات الأول.", "error")
        return redirect(url_for("cu_view", cid=cid))
    db.execute("UPDATE custodies SET status = ?, submitted_at = ? WHERE id = ?", (CU_SUBMITTED, now_str(), cid))
    cu_event(db, cid, "تقديم للتصفية", request.form.get("note", "").strip())
    audit("cu_submit", "custody", cid, cu_code(c))
    db.commit()
    return redirect(url_for("cu_view", cid=cid))


@app.route("/custody/<int:cid>/review", methods=["POST"])
@accounts_only
def cu_review(cid):
    db = get_db()
    c = get_cu(db, cid)
    if c["status"] != CU_SUBMITTED:
        abort(409)
    action = request.form.get("action")
    note = request.form.get("note", "").strip()
    if action == "settle":
        t = cu_totals(db, c)
        db.execute("UPDATE custodies SET status=?, settled_at=?, settled_by_name=?, settle_note=?, final_received=?,"
                   " final_spent=?, final_balance=? WHERE id=?",
                   (CU_SETTLED, date.today().isoformat(), g.user["full_name"] or g.user["username"], note[:300],
                    t["received"], t["spent"], t["balance"], cid))
        cu_event(db, cid, "اعتماد التصفية", f"المتبقي {t['balance']:,.2f}" + (f" · {note}" if note else ""))
        audit("cu_settle", "custody", cid, f"{cu_code(c)}: الرصيد {t['balance']:,.2f}")
    elif action == "reject":
        if not note:
            flash("اكتب سبب الإرجاع علشان صاحب العهدة يعدّل.", "error")
            return redirect(url_for("cu_view", cid=cid))
        db.execute("UPDATE custodies SET status = ? WHERE id = ?", (CU_OPEN, cid))
        cu_event(db, cid, "إرجاع للتعديل", note)
        audit("cu_reject", "custody", cid, cu_code(c))
    else:
        abort(400)
    db.commit()
    return redirect(url_for("cu_view", cid=cid))


@app.route("/custody/<int:cid>/reopen", methods=["POST"])
@admin_only
def cu_reopen(cid):
    db = get_db()
    c = get_cu(db, cid)
    if c["status"] != CU_SETTLED:
        abort(409)
    db.execute("UPDATE custodies SET status=?, settled_at=NULL, settled_by_name='', settle_note='',"
               " final_received=NULL, final_spent=NULL, final_balance=NULL WHERE id=?", (CU_OPEN, cid))
    cu_event(db, cid, "إعادة فتح العهدة", request.form.get("note", "").strip())
    audit("cu_reopen", "custody", cid, cu_code(c))
    db.commit()
    return redirect(url_for("cu_view", cid=cid))


@app.route("/custody/<int:cid>/delete", methods=["POST"])
@admin_only
def cu_delete(cid):
    db = get_db()
    c = get_cu(db, cid)
    if c["status"] != CU_OPEN or db.execute("SELECT COUNT(*) FROM custody_expenses WHERE custody_id = ?", (cid,)).fetchone()[0]:
        abort(409)
    for tbl in ("custody_events", "custody_advances"):
        db.execute(f"DELETE FROM {tbl} WHERE custody_id = ?", (cid,))
    db.execute("DELETE FROM custodies WHERE id = ?", (cid,))
    audit("cu_delete", "custody", cid, cu_code(c))
    db.commit()
    return redirect(url_for("cu_list"))


@app.route("/custody/<int:cid>/print")
def cu_print(cid):
    db = get_db()
    c = get_cu(db, cid)
    s = load_settings(db)
    advances = db.execute("SELECT * FROM custody_advances WHERE custody_id = ? ORDER BY date, id", (cid,)).fetchall()
    return render_template(
        "custody_print.html", c=c, t=cu_totals(db, c), groups=cu_groups(db, cid), advances=advances,
        code=cu_code(c), draft=c["status"] != CU_SETTLED, en=False, L=LABELS["ar"], cb=company_block(s, False))


@app.route("/custody/categories", methods=["GET", "POST"])
@accounts_only
def cu_categories():
    db = get_db()
    if request.method == "POST":
        f = request.form
        act = f.get("action")
        if act == "add":
            name = f.get("name", "").strip()[:60]
            if name and not db.execute("SELECT 1 FROM custody_categories WHERE name = ?", (name,)).fetchone():
                mx = db.execute("SELECT COALESCE(MAX(sort),0) FROM custody_categories").fetchone()[0]
                db.execute("INSERT INTO custody_categories(name, sort, active) VALUES (?,?,1)", (name, mx + 1))
                audit("cu_cat", "custody", None, f"إضافة بند: {name}")
            else:
                flash("اكتب اسم بند جديد (مش موجود قبل كده).", "error")
        elif act in ("rename", "toggle"):
            k = db.execute("SELECT * FROM custody_categories WHERE id = ?", (f.get("id", type=int),)).fetchone()
            if k:
                if act == "rename":
                    name = f.get("name", "").strip()[:60]
                    dup = db.execute("SELECT 1 FROM custody_categories WHERE name = ? AND id != ?", (name, k["id"])).fetchone()
                    if name and not dup:
                        db.execute("UPDATE custody_categories SET name = ? WHERE id = ?", (name, k["id"]))
                        audit("cu_cat", "custody", None, f"تعديل بند: {k['name']} ← {name}")
                    else:
                        flash("الاسم فاضي أو مكرر.", "error")
                else:
                    db.execute("UPDATE custody_categories SET active = 1 - active WHERE id = ?", (k["id"],))
                    audit("cu_cat", "custody", None, f"{'إيقاف' if k['active'] else 'تفعيل'} بند: {k['name']}")
        db.commit()
        return redirect(url_for("cu_categories"))
    cats = db.execute(
        "SELECT k.*, (SELECT COUNT(*) FROM custody_expenses e WHERE e.category_id = k.id) AS used"
        " FROM custody_categories k ORDER BY k.sort, k.id").fetchall()
    return render_template("custody_cats.html", cats=cats)


init_db()
ensure_setup_code()
start_backup_thread()

if __name__ == "__main__":
    # للتشغيل الدائم على السيرفر استخدم waitress: waitress-serve --port=8000 app:app
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=False)
