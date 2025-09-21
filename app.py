import os, json, io, smtplib, textwrap
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
                items_json TEXT DEFAULT '[]',
                status TEXT DEFAULT 'open',
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """))
        conn.execute(text("ALTER TABLE proposals ADD COLUMN IF NOT EXISTS project_name TEXT;"))
        conn.execute(text("ALTER TABLE proposals ADD COLUMN IF NOT EXISTS project_location TEXT;"))
        conn.execute(text("ALTER TABLE proposals ADD COLUMN IF NOT EXISTS notes TEXT;"))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS invoices (
                id SERIAL PRIMARY KEY,
                invoice_no TEXT UNIQUE,
                customer_id TEXT NOT NULL REFERENCES customers(id),
                items_json TEXT DEFAULT '[]',
                total NUMERIC DEFAULT 0,
                paid BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """))
        conn.execute(text("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS project_name TEXT;"))
        conn.execute(text("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS project_location TEXT;"))
        conn.execute(text("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS deposit NUMERIC DEFAULT 0;"))
        conn.execute(text("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS check_number TEXT;"))

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
    page_num = 1

    # --- Footer ---
    def draw_footer(canv, pnum):
        canv.setFont("Helvetica-Oblique", 9)
        canv.setFillColorRGB(0.3, 0.3, 0.3)
        footer_text = f"Thank you for your business!  •  J & I Heating and Cooling  •  (630) 849-0385  •  Sandwich, IL   |   Page {pnum}"
        canv.drawCentredString(width/2, 0.5*inch, footer_text)

    # --- Header ---
    def draw_header():
        try:
            logo = ImageReader("logo.png")
            logo_width = 120
            logo_x = (width - logo_width) / 2
            c.drawImage(logo, logo_x, height-1.2*inch, width=logo_width,
                        preserveAspectRatio=True, mask='auto')
        except:
            pass
        c.setFont("Helvetica-Bold", 16)
        c.drawString(1*inch, height-2.0*inch, "J & I Heating and Cooling")
        c.setFont("Helvetica", 10)
        c.drawString(1*inch, height-2.25*inch, "2788 N. 48th Rd.")
        c.drawString(1*inch, height-2.45*inch, "Sandwich IL, 60548")
        c.drawString(1*inch, height-2.65*inch, "Phone (630) 849-0385")
        c.drawString(1*inch, height-2.85*inch, "Insured and Bonded")

    def new_page(pnum):
        draw_header()
        draw_footer(c, pnum)

    # First page
    new_page(page_num)

    # Header info
    c.setFont("Helvetica", 12)
    c.drawString(1*inch, height-3.1*inch, f"{'Proposal' if is_proposal else 'Invoice'} #: {invoice_no}")
    c.drawString(1*inch, height-3.3*inch, f"Customer: {cust_name}")
    c.drawString(1*inch, height-3.5*inch, f"Project: {project_name or ''}")
    c.drawString(1*inch, height-3.7*inch, f"Location: {project_location or ''}")

    # Line items
    y = height-4.1*inch
    c.setFont("Helvetica-Bold", 10)
    c.drawString(1*inch, y, "Description")
    c.drawString(4*inch, y, "Qty")
    c.drawString(5*inch, y, "Unit")
    c.drawString(6*inch, y, "Line Total")
    y -= 14
    c.setFont("Helvetica", 10)
    for row in items:
        desc_text = str(row.get("Description", ""))
        wrapped_desc = textwrap.wrap(desc_text, width=70)
        for j, line in enumerate(wrapped_desc):
            if y < 1.5*inch:
                draw_footer(c, page_num)
                c.showPage()
                page_num += 1
                new_page(page_num)
                y = height-1*inch
                c.setFont("Helvetica", 10)
            c.drawString(1*inch, y, line)
            if j == 0:
                c.drawString(4*inch, y, f"{float(row.get('Qty',0)):.2f}")
                c.drawString(5*inch, y, f"${float(row.get('Unit Price',0)):.2f}")
                c.drawString(6*inch, y, f"${float(row.get('Qty',0))*float(row.get('Unit Price',0)):.2f}")
            y -= 12

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

    # Notes (wrapped)
    if notes:
        c.setFont("Helvetica-Oblique", 9)
        wrapped = textwrap.wrap(notes, width=90)
        for line in wrapped:
            if y < 1*inch:
                draw_footer(c, page_num)
                c.showPage()
                page_num += 1
                new_page(page_num)
                y = height - 1*inch
                c.setFont("Helvetica-Oblique", 9)
            c.drawString(1*inch, y, line)
            y -= 12

    # PAID stamp
    if show_paid and not is_proposal:
        c.setFont("Helvetica-Bold", 72)
        c.setFillColorRGB(1, 0, 0)
        c.drawCentredString(width/2, height/2, "PAID")

    draw_footer(c, page_num)
    c.save()
    buf.seek(0)
    return buf.getvalue()

# ---------- Dynamic Line Items ----------
if "line_count" not in st.session_state:
    st.session_state.line_count = 5
def add_line():
    st.session_state.line_count += 1

# ---------- Mode Switch ----------
mode = st.radio("Choose Mode", ["Proposal", "Invoice"], horizontal=True)

# ---------- Load Customers ----------
with engine.begin() as conn:
    customers = conn.execute(text("SELECT * FROM customers ORDER BY name")).mappings().all()
if not customers:
    st.warning("No customers yet.")
    st.stop()

cust = st.selectbox("Select Customer", customers, format_func=lambda c: c["name"])

# ---------- Project fields ----------
project_name = st.text_input("Project Name", "")
project_location = st.text_input("Project Location (Address)", "")

# ---------- Line items ----------
items = []
st.write("Line Items")
for i in range(st.session_state.line_count):
    c1, c2, c3, c4 = st.columns([5,1.5,2,2])
    desc = c1.text_input(f"Description {i+1}", "")
    qty = c2.number_input(f"Qty {i+1}", min_value=0.0, value=1.0, step=1.0)
    unit = c3.number_input(f"Unit Price {i+1}", min_value=0.0, value=0.0, step=10.0)
    c4.write(f"${qty*unit:,.2f}")
    if str(desc).strip():
        items.append({"Description": desc, "Qty": qty, "Unit Price": unit})
st.button("➕ Add Line Item", on_click=add_line)

subtotal = compute_subtotal(items)

# ---------- Proposal Mode ----------
if mode == "Proposal":
    pid = st.text_input("Proposal ID", "P-1001")
    proposal_notes = (
        "By signing, the signee agrees to pay the full balance upon project completion, "
        "acknowledges that additional work outside the scope will incur extra charges on the final invoice, "
        "and understands that all manufacturer details are outlined in the product owner’s manual."
    )
    st.text_area("Proposal Terms", proposal_notes, disabled=True)

    pdf_data = build_pdf(pid, cust["name"], project_name, project_location,
                         items, subtotal, 0, subtotal, None,
                         show_paid=False, notes=proposal_notes, is_proposal=True)

    c1,c2 = st.columns(2)
    with c1:
        st.download_button("📄 Download Proposal PDF", pdf_data, file_name=f"Proposal_{pid}.pdf", mime="application/pdf")
    with c2:
        if st.button("📧 Email Proposal"):
            if not APP_PASSWORD:
                st.error("APP_PASSWORD missing in Secrets")
            else:
                msg = EmailMessage()
                msg["From"] = FROM_EMAIL
                msg["To"] = cust.get("email") or ""
                msg["Subject"] = f"Proposal {pid}"
                msg.set_content("Please find attached proposal.")
                msg.add_attachment(pdf_data, maintype="application", subtype="pdf", filename=f"Proposal_{pid}.pdf")
                try:
                    with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
                        server.login(FROM_EMAIL, APP_PASSWORD)
                        server.send_message(msg)
                    st.success("Proposal emailed")
                except Exception as e:
                    st.error(f"Email failed: {e}")

    if st.button("💾 Save Proposal"):
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO proposals (id, customer_id, project_name, project_location, items_json, notes, status)
                VALUES (:id, :cid, :pname, :ploc, :items, :notes, 'open')
                ON CONFLICT (id) DO UPDATE
                SET customer_id=EXCLUDED.customer_id,
                    project_name=EXCLUDED.project_name,
                    project_location=EXCLUDED.project_location,
                    items_json=EXCLUDED.items_json,
                    notes=EXCLUDED.notes,
                    status='open'
            """), dict(id=pid, cid=cust["id"], pname=project_name, ploc=project_location,
                       items=json.dumps(items), notes=proposal_notes))
        st.success(f"Proposal {pid} saved!")

# ---------- Invoice Mode ----------
if mode == "Invoice":
    invoice_no = st.text_input("Invoice #", "1001")
    deposit = st.number_input("Deposit Amount", min_value=0.0, value=0.0, step=50.0)
    check_number = st.text_input("Check Number (if paying by check)", "")
    show_paid = st.toggle("Show PAID Stamp", value=False)

    grand_total = max(0.0, subtotal - deposit)
    st.write(f"**Subtotal: ${subtotal:,.2f}**")
    if deposit > 0:
        st.write(f"**Deposit: -${deposit:,.2f}**")
    st.write(f"**Grand Total: ${grand_total:,.2f}**")

    pdf_data = build_pdf(invoice_no, cust["name"], project_name, project_location,
                         items, subtotal, deposit, grand_total, check_number,
                         show_paid=show_paid, notes=None, is_proposal=False)

    c1,c2,c3 = st.columns(3)
    with c1:
        st.download_button("📄 Download Invoice PDF", pdf_data, file_name=f"Invoice_{invoice_no}.pdf", mime="application/pdf")
    with c2:
        if st.button("📧 Email Invoice"):
            if not APP_PASSWORD:
                st.error("APP_PASSWORD missing in Secrets")
            else:
                msg = EmailMessage()
                msg["From"] = FROM_EMAIL
                msg["To"] = cust.get("email") or ""
                msg["Subject"] = f"Invoice {invoice_no}"
                msg.set_content("Please find attached invoice.")
                msg.add_attachment(pdf_data, maintype="application", subtype="pdf", filename=f"Invoice_{invoice_no}.pdf")
                try:
                    with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
                        server.login(FROM_EMAIL, APP_PASSWORD)
                        server.send_message(msg)
                    st.success("Invoice emailed")
                except Exception as e:
                    st.error(f"Email failed: {e}")
    with c3:
        if st.button("💾 Save Invoice"):
            with engine.begin() as conn:
                conn.execute(text("""
                    INSERT INTO invoices (invoice_no, customer_id, project_name, project_location, items_json, deposit, check_number, total, paid)
                    VALUES (:inv, :cid, :pname, :ploc, :items, :dep, :chk, :total, :paid)
                    ON CONFLICT (invoice_no) DO UPDATE
                    SET customer_id=EXCLUDED.customer_id,
                        project_name=EXCLUDED.project_name,
                        project_location=EXCLUDED.project_location,
                        items_json=EXCLUDED.items_json,
                        deposit=EXCLUDED.deposit,
                        check_number=EXCLUDED.check_number,
                        total=EXCLUDED.total,
                        paid=EXCLUDED.paid
                """), dict(inv=invoice_no, cid=cust["id"], pname=project_name, ploc=project_location,
                           items=json.dumps(items), dep=float(deposit), chk=check_number,
                           total=grand_total, paid=bool(show_paid)))
            st.success(f"Invoice {invoice_no} saved!")
# app.py (Invoicing App with Postgres)
# Full Streamlit code provided in chat previously
