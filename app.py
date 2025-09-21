import os, json, io, smtplib
from email.message import EmailMessage
import streamlit as st
from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from sqlalchemy import create_engine, text

st.set_page_config(page_title="J&I Invoicing", page_icon="🧾", layout="centered")

# Show logo at top of the web app
try:
    st.image("logo.png", width=200)
except:
    st.write("⚠️ Logo not found. Place logo.png in the same folder as app.py")

st.title("🧾 J&I Invoicing — Proposals & Invoices")

# ---------- DB & Email Config ----------
DATABASE_URL = st.secrets.get("DATABASE_URL", os.getenv("DATABASE_URL", ""))
if not DATABASE_URL:
    st.error("DATABASE_URL not set in Secrets.")
    st.stop()
engine = create_engine(DATABASE_URL, pool_pre_ping=True)

FROM_EMAIL = st.secrets.get("FROM_EMAIL", "jiheatingcooling.homerepairs@gmail.com")
SMTP_SERVER = st.secrets.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(st.secrets.get("SMTP_PORT", 465))
APP_PASSWORD = st.secrets.get("APP_PASSWORD", "")

# ---------- Initialize Tables ----------
def init_db():
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS customers (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT,
                phone TEXT,
                address TEXT,
                city_state_zip TEXT
            );
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS proposals (
                id TEXT PRIMARY KEY,
                customer_id TEXT NOT NULL REFERENCES customers(id),
                project_name TEXT,
                project_location TEXT,
                items_json TEXT DEFAULT '[]',
                notes TEXT,
                status TEXT DEFAULT 'open',
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS invoices (
                id SERIAL PRIMARY KEY,
                invoice_no TEXT UNIQUE,
                customer_id TEXT NOT NULL REFERENCES customers(id),
                project_name TEXT,
                project_location TEXT,
                items_json TEXT DEFAULT '[]',
                deposit NUMERIC DEFAULT 0,
                check_number TEXT,
                total NUMERIC DEFAULT 0,
                paid BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """))
init_db()

# ---------- Helpers ----------
def compute_subtotal(items):
    return sum(float(r.get("Qty", 0)) * float(r.get("Unit Price", 0)) for r in items)

def build_pdf(invoice_no, cust_name, project_name, project_location, items,
              subtotal, deposit, grand_total, check_number,
              show_paid=False, notes=None, is_proposal=False):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=LETTER)
    width, height = LETTER

    # Company header with logo
    try:
        logo = ImageReader("logo.png")
        c.drawImage(logo, 1*inch, height-1.2*inch, width=120, preserveAspectRatio=True, mask='auto')
    except:
        pass  # Skip if logo missing

    c.setFont("Helvetica-Bold", 16)
    c.drawString(2.5*inch, height-1*inch, "J & I Heating and Cooling")
    c.setFont("Helvetica", 10)
    c.drawString(2.5*inch, height-1.25*inch, "2788 N. 48th Rd.")
    c.drawString(2.5*inch, height-1.45*inch, "Sandwich IL, 60548")
    c.drawString(2.5*inch, height-1.65*inch, "Phone (630) 849-0385")
    c.drawString(2.5*inch, height-1.85*inch, "Insured and Bonded")

    # Header info
    c.setFont("Helvetica", 12)
    if is_proposal:
        c.drawString(1*inch, height-2.2*inch, f"Proposal #: {invoice_no}")
    else:
        c.drawString(1*inch, height-2.2*inch, f"Invoice #: {invoice_no}")

    c.drawString(1*inch, height-2.4*inch, f"Customer: {cust_name}")
    c.drawString(1*inch, height-2.6*inch, f"Project: {project_name or ''}")
    c.drawString(1*inch, height-2.8*inch, f"Location: {project_location or ''}")

    # Line items
    y = height-3.2*inch
    c.setFont("Helvetica-Bold", 10)
    c.drawString(1*inch, y, "Description")
    c.drawString(4*inch, y, "Qty")
    c.drawString(5*inch, y, "Unit")
    c.drawString(6*inch, y, "Line Total")
    y -= 14
    c.setFont("Helvetica", 10)
    for row in items:
        if y < 1.5*inch:
            c.showPage(); y = height-1*inch
        c.drawString(1*inch, y, str(row.get("Description",""))[:80])
        c.drawString(4*inch, y, f"{float(row.get('Qty',0)):.2f}")
        c.drawString(5*inch, y, f"${float(row.get('Unit Price',0)):.2f}")
        c.drawString(6*inch, y, f"${float(row.get('Qty',0))*float(row.get('Unit Price',0)):.2f}")
        y -= 14

    # Totals (invoices only)
    if not is_proposal:
        y -= 8
        c.setFont("Helvetica-Bold", 11)
        c.drawString(5*inch, y, "Subtotal:")
        c.drawString(6*inch, y, f"${subtotal:,.2f}")
        y -= 15
        if deposit and float(deposit) > 0:
            c.drawString(5*inch, y, "Deposit:")
            c.drawString(6*inch, y, f"-${float(deposit):,.2f}")
            y -= 15
            c.drawString(5*inch, y, "Grand Total:")
            c.drawString(6*inch, y, f"${grand_total:,.2f}")
            y -= 15
        else:
            c.drawString(5*inch, y, "Total:")
            c.drawString(6*inch, y, f"${grand_total:,.2f}")
            y -= 15

        if check_number:
            c.setFont("Helvetica", 10)
            c.drawString(1*inch, y, f"Check #: {check_number}")
            y -= 15

    # Notes
    if notes:
        c.setFont("Helvetica-Oblique", 9)
        c.drawString(1*inch, y-20, f"Notes: {notes[:1000]}")

    # PAID stamp
    if show_paid and not is_proposal:
        c.setFont("Helvetica-Bold", 72)
        c.setFillColorRGB(1, 0, 0)
        c.drawCentredString(width/2, height/2, "PAID")

    c.save()
    buf.seek(0)
    return buf.getvalue()
# app.py (Invoicing App with Postgres)
# Full Streamlit code provided in chat previously
