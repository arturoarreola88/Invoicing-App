import os, json, io, ssl, smtplib
from email.message import EmailMessage
import streamlit as st
from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas
from reportlab.lib.units import inch
from sqlalchemy import create_engine, text

st.set_page_config(page_title="J&I Invoicing", page_icon="🧾", layout="centered")
st.title("🧾 J&I Invoicing — Postgres Edition")

# Database connection
DATABASE_URL = st.secrets.get("DATABASE_URL", os.getenv("DATABASE_URL", ""))
if not DATABASE_URL:
    st.error("DATABASE_URL not set. Add it in Streamlit Secrets.")
    st.stop()
engine = create_engine(DATABASE_URL, pool_pre_ping=True)

# Email config
FROM_EMAIL = st.secrets.get("FROM_EMAIL", "jiheatingcooling.homerepairs@gmail.com")
SMTP_SERVER = st.secrets.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(st.secrets.get("SMTP_PORT", 465))
APP_PASSWORD = st.secrets.get("APP_PASSWORD", "")

# Ensure tables exist
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
            CREATE TABLE IF NOT EXISTS invoices (
                id SERIAL PRIMARY KEY,
                invoice_no TEXT UNIQUE,
                customer_id TEXT NOT NULL REFERENCES customers(id),
                project TEXT,
                items_json TEXT DEFAULT '[]',
                total NUMERIC DEFAULT 0,
                paid BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """))
init_db()

# Add customer
st.subheader("Customers")
with st.form("cust_form"):
    cid = st.text_input("Customer ID")
    cname = st.text_input("Name")
    cemail = st.text_input("Email")
    cphone = st.text_input("Phone")
    caddr = st.text_input("Address")
    ccity = st.text_input("City/State/Zip")
    submitted = st.form_submit_button("Save Customer")
    if submitted and cid and cname:
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO customers(id,name,email,phone,address,city_state_zip)
                VALUES(:id,:name,:email,:phone,:addr,:csz)
                ON CONFLICT(id) DO UPDATE
                SET name=EXCLUDED.name, email=EXCLUDED.email,
                    phone=EXCLUDED.phone, address=EXCLUDED.address,
                    city_state_zip=EXCLUDED.city_state_zip
            """), dict(id=cid, name=cname, email=cemail, phone=cphone, addr=caddr, csz=ccity))
        st.success(f"Saved {cname}")

# Fetch customers
with engine.begin() as conn:
    customers = conn.execute(text("SELECT * FROM customers ORDER BY name")).mappings().all()

if not customers:
    st.info("No customers yet")
    st.stop()

# Select customer
st.subheader("Invoice Builder")
cust = st.selectbox("Select Customer", customers, format_func=lambda c: c['name'])
invoice_no = st.text_input("Invoice #", "1001")
project = st.text_input("Project / Job", "")
show_paid = st.toggle("Show PAID Stamp", value=False)

# Items
items = []
st.write("Line Items")
for i in range(5):
    cols = st.columns([5,1,2,2])
    desc = cols[0].text_input(f"Description {i+1}")
    qty = cols[1].number_input(f"Qty {i+1}", 0, 100, 1)
    unit = cols[2].number_input(f"Unit Price {i+1}", 0.0, 10000.0, 0.0)
    total = qty * unit
    cols[3].write(f"${total:,.2f}")
    if desc.strip():
        items.append({"Description":desc,"Qty":qty,"Unit Price":unit})
total_amount = sum(r["Qty"]*r["Unit Price"] for r in items)
st.write(f"**Total: ${total_amount:,.2f}**")

# PDF builder
def build_pdf():
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=LETTER)
    width, height = LETTER
    c.setFont("Helvetica-Bold", 16)
    c.drawString(1*inch, height-1*inch, "J & I Heating and Cooling")
    c.setFont("Helvetica", 10)
    c.drawString(1*inch, height-1.2*inch, "Invoice")
    c.setFont("Helvetica", 12)
    c.drawString(1*inch, height-1.6*inch, f"Invoice #: {invoice_no}")
    c.drawString(1*inch, height-1.9*inch, f"Customer: {cust['name']}")
    c.drawString(1*inch, height-2.2*inch, f"Project: {project}")
    y = height-2.7*inch
    c.setFont("Helvetica-Bold", 10)
    c.drawString(1*inch, y, "Description")
    c.drawString(4*inch, y, "Qty")
    c.drawString(5*inch, y, "Unit")
    c.drawString(6*inch, y, "Line Total")
    y -= 14
    c.setFont("Helvetica", 10)
    for row in items:
        c.drawString(1*inch, y, row["Description"])
        c.drawString(4*inch, y, str(row["Qty"]))
        c.drawString(5*inch, y, f"${row['Unit Price']:.2f}")
        c.drawString(6*inch, y, f"${row['Qty']*row['Unit Price']:.2f}")
        y -= 14
    c.setFont("Helvetica-Bold", 11)
    c.drawString(5*inch, y-10, "Total:")
    c.drawString(6*inch, y-10, f"${total_amount:,.2f}")
    if show_paid:
        c.setFont("Helvetica-Bold", 72)
        c.setFillColorRGB(1,0,0)
        c.drawCentredString(width/2, height/2, "PAID")
    c.save()
    buf.seek(0)
    return buf.getvalue()

col1,col2,col3 = st.columns(3)
with col1:
    if st.button("Download PDF"):
        st.download_button("Save PDF", build_pdf(), file_name=f"Invoice_{invoice_no}.pdf")
with col2:
    if st.button("Email Invoice"):
        if not APP_PASSWORD:
            st.error("APP_PASSWORD missing")
        else:
            msg = EmailMessage()
            msg["From"] = FROM_EMAIL
            msg["To"] = cust["email"]
            msg["Subject"] = f"Invoice {invoice_no}"
            msg.set_content("Please find attached invoice.")
            msg.add_attachment(build_pdf(), maintype="application", subtype="pdf", filename=f"Invoice_{invoice_no}.pdf")
            with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
                server.login(FROM_EMAIL, APP_PASSWORD)
                server.send_message(msg)
            st.success("Invoice emailed")
with col3:
    if st.button("Save Invoice"):
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO invoices(invoice_no,customer_id,project,items_json,total,paid)
                VALUES(:inv,:cid,:proj,:items,:total,:paid)
                ON CONFLICT(invoice_no) DO UPDATE
                SET customer_id=EXCLUDED.customer_id, project=EXCLUDED.project,
                    items_json=EXCLUDED.items_json, total=EXCLUDED.total, paid=EXCLUDED.paid
            """), dict(inv=invoice_no, cid=cust["id"], proj=project, items=json.dumps(items),
                       total=total_amount, paid=show_paid))
        st.success("Invoice saved")
# app.py (Invoicing App with Postgres)
# Full Streamlit code provided in chat previously
