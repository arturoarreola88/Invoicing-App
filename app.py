# =========================
# J & I — Proposals & Invoices (Streamlit)
# =========================
# Features:
# - Customers table
# - Proposals: create, sign, PDF, email, save, active proposals (convert/close/view)
# - Invoices: create, sign, PDF, email, save, mark paid/unpaid, recent invoices (view/download)
# - Auto-increment IDs; proposal→invoice keeps same number (P-#### -> INV-####)
# - PDF: logo top-right, aligned columns, PAID stamp with date
# - Email: greets by first name + website link
# - Safe reset: only clears customer selection, project name/location & line items
# - Optional “heavy” session_state keys scaffold (commented out)

import os, io, json, base64, textwrap, smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage

import streamlit as st
from sqlalchemy import create_engine, text
from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from streamlit_drawable_canvas import st_canvas
from PIL import Image

st.set_page_config(page_title="J&I Proposals & Invoices", page_icon="🧾", layout="centered")

# =========================
# Header + Branding
# =========================
try:
    st.image("logo.png", width=220)
except:
    st.info("Tip: place a 'logo.png' next to app.py to show it in the header + PDFs.")
st.title("🧾 J & I — Proposals & Invoices")

# =========================
# Secrets / Environment
# =========================
DATABASE_URL = st.secrets.get("DATABASE_URL", os.getenv("DATABASE_URL", ""))
FROM_EMAIL   = st.secrets.get("FROM_EMAIL",   os.getenv("FROM_EMAIL",   "jiheatingcooling.homerepairs@gmail.com"))
SMTP_SERVER  = st.secrets.get("SMTP_SERVER",  os.getenv("SMTP_SERVER",  "smtp.gmail.com"))
SMTP_PORT    = int(st.secrets.get("SMTP_PORT", os.getenv("SMTP_PORT", 465)))
APP_PASSWORD = st.secrets.get("APP_PASSWORD", os.getenv("APP_PASSWORD", ""))

if not DATABASE_URL:
    st.error("DATABASE_URL not set in Secrets.")
    st.stop()

engine = create_engine(DATABASE_URL, pool_pre_ping=True)

# =========================
# DB Init + Migration
# =========================
def init_db():
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS customers(
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              email TEXT,
              phone TEXT,
              address TEXT,
              city_state_zip TEXT
            );
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS proposals(
              id TEXT PRIMARY KEY,      -- e.g., P-1001
              number INTEGER,           -- numeric part (1001)
              customer_id TEXT NOT NULL REFERENCES customers(id),
              project_name TEXT,
              project_location TEXT,
              items_json TEXT DEFAULT '[]',
              notes TEXT,
              status TEXT DEFAULT 'open',  -- open | converted | closed
              created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS invoices(
              id SERIAL PRIMARY KEY,
              invoice_no TEXT UNIQUE,   -- e.g., INV-1001
              number INTEGER,           -- numeric part matches proposal
              customer_id TEXT NOT NULL REFERENCES customers(id),
              project_name TEXT,
              project_location TEXT,
              items_json TEXT DEFAULT '[]',
              total NUMERIC DEFAULT 0,
              deposit NUMERIC DEFAULT 0,
              check_number TEXT,
              paid BOOLEAN DEFAULT FALSE,
              created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """))
init_db()

def migrate_db():
    with engine.begin() as conn:
        # proposals.number
        res = conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name='proposals' AND column_name='number'
        """)).fetchone()
        if not res:
            conn.execute(text("ALTER TABLE proposals ADD COLUMN number INTEGER"))
            conn.execute(text("""
                UPDATE proposals
                SET number = CAST(REGEXP_REPLACE(id, '\\D', '', 'g') AS INTEGER)
                WHERE number IS NULL
            """))
        # invoices.number
        res = conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name='invoices' AND column_name='number'
        """)).fetchone()
        if not res:
            conn.execute(text("ALTER TABLE invoices ADD COLUMN number INTEGER"))
            conn.execute(text("""
                UPDATE invoices
                SET number = CAST(REGEXP_REPLACE(invoice_no, '\\D', '', 'g') AS INTEGER)
                WHERE number IS NULL
            """))
migrate_db()

# =========================
# Session State + Helpers
# =========================
ss = st.session_state
ss.setdefault("line_count", 5)

# --- Optional “heavy” states (uncomment if you want explicit control across reruns) ---
# ss.setdefault("project_name_value", "")
# ss.setdefault("project_location_value", "")
# ss.setdefault("prefill_items", [])
# ss.setdefault("prefill_customer_id", None)
# ss.setdefault("prefill_proposal_number", None)
# ss.setdefault("prefill_proposal_id", None)

def add_line():
    ss.line_count += 1

def compute_subtotal(items):
    return sum(float(r.get("Qty", 0)) * float(r.get("Unit Price", 0)) for r in items)

def _max_existing_number(conn):
    r1 = conn.execute(text("SELECT COALESCE(MAX(number), 0) FROM proposals")).scalar() or 0
    r2 = conn.execute(text("SELECT COALESCE(MAX(number), 0) FROM invoices")).scalar() or 0
    return max(r1, r2)

def format_prop_id(n): return f"P-{n:04d}"
def format_inv_id(n):  return f"INV-{n:04d}"

def show_pdf_newtab(pdf_bytes):
    b64 = base64.b64encode(pdf_bytes).decode("utf-8")
    st.markdown(
        f'<a href="data:application/pdf;base64,{b64}" target="_blank">📄 Open PDF in New Tab</a>',
        unsafe_allow_html=True
    )

def get_customers():
    with engine.begin() as conn:
        return conn.execute(text("SELECT * FROM customers ORDER BY name")).mappings().all()

# =========================
# Email
# =========================
def build_email_body(cust_name, is_proposal, ref_no):
    first = (cust_name or "Customer").split()[0]
    kind = "proposal" if is_proposal else "invoice"
    return f"""
    <p>Hi {first},</p>
    <p>Attached is the {kind} ({ref_no}) that had been requested. Please review at
    your earliest convenience and
    contact me with any questions comments or concerns.</p>
    <p>Thank you for choosing J & I Heating and Cooling.</p>
    <hr>
    <p>
      Best regards,<br>
      <b>Arturo Arreola</b><br>
      Owner<br>
      Direct: (630) 849-0385<br>
      <a href="https://jihchr.com">Click here for our website</a>
    </p>
    """

def send_email(pdf_bytes, to_email, subject, html_body, filename):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = FROM_EMAIL, to_email or "", subject
    msg.add_alternative(html_body, subtype="html")
    msg.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename=filename)
    with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
        server.login(FROM_EMAIL, APP_PASSWORD)
        server.send_message(msg)

# =========================
# PDF Builder
# =========================
def build_pdf(ref_no, cust_name, project_name, project_location, items,
              subtotal, deposit, grand_total, check_number,
              show_paid=False, notes=None, is_proposal=False,
              signature_png_bytes=None, signature_date_text=None):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=LETTER)
    width, height = LETTER

    # Header
    try:
        logo = ImageReader("logo.png")
        c.drawImage(logo, width - 120, height - 80, width=100, height=60, mask='auto')
    except:
        pass
    c.setFont("Helvetica-Bold", 16)
    c.drawString(1*inch, height-1*inch, "J & I Heating and Cooling")
    c.setFont("Helvetica", 10)
    c.drawString(1*inch, height-1.25*inch, "2788 N. 48th Rd.")
    c.drawString(1*inch, height-1.45*inch, "Sandwich IL, 60548")
    c.drawString(1*inch, height-1.65*inch, "Phone (630) 849-0385")
    c.drawString(1*inch, height-1.85*inch, "Insured and Bonded")

    issue_date = datetime.now().date()
    heading = "Proposal" if is_proposal else "Invoice"
    terms_text = (f"Valid until: {(issue_date + timedelta(days=15)).strftime('%m/%d/%Y')}"
                  if is_proposal else f"Due Date: {issue_date.strftime('%m/%d/%Y')}")

    c.setFont("Helvetica", 12)
    c.drawString(1*inch, height-2.3*inch, f"{heading} #: {ref_no}")
    c.drawString(1*inch, height-2.5*inch, f"Customer: {cust_name}")
    c.drawString(1*inch, height-2.7*inch, f"Project: {project_name or ''}")
    c.drawString(1*inch, height-2.9*inch, f"Location: {project_location or ''}")

    rx = width - 2.5*inch
    c.setFont("Helvetica", 10)
    c.drawString(rx, height-2.3*inch, f"Date: {issue_date.strftime('%m/%d/%Y')}")
    c.drawString(rx, height-2.6*inch, terms_text)

    # Paid stamp
    if show_paid and not is_proposal:
        c.setFont("Helvetica-Bold", 36)
        c.setFillColorRGB(1,0,0)
        c.drawString(width/2 + 0.5*inch, height-3.0*inch, "PAID")
        c.setFont("Helvetica", 12)
        c.drawString(width/2 + 0.5*inch, height-3.4*inch, datetime.now().strftime("%m/%d/%Y"))
        c.setFillColorRGB(0,0,0)

    # Table headers
    y = height-3.6*inch
    c.setFont("Helvetica-Bold", 10)
    c.drawString(1*inch, y, "Description")
    c.drawString(4.4*inch, y, "Qty")
    c.drawString(5.4*inch, y, "Unit")
    c.drawString(6.4*inch, y, "Line Total")
    y -= 16
    c.setFont("Helvetica", 10)

    # Line items (wrapped + spacing)
    for row in items:
        desc = str(row.get("Description", ""))
        wrapped = textwrap.wrap(desc, width=50)
        for j, line in enumerate(wrapped):
            c.drawString(1*inch, y, line)
            if j == 0:
                qty  = float(row.get("Qty", 0))
                unit = float(row.get("Unit Price", 0))
                c.drawString(4.4*inch, y, f"{qty:.2f}")
                c.drawString(5.4*inch, y, f"${unit:,.2f}")
                c.drawString(6.4*inch, y, f"${qty*unit:,.2f}")
            y -= 18

    # Totals
    y -= 10
    c.setFont("Helvetica-Bold", 11)
    if is_proposal:
        c.drawString(5*inch, y, "Subtotal:")
        c.drawString(6.4*inch, y, f"${subtotal:,.2f}")
        y -= 18
        c.drawString(5*inch, y, "Grand Total:")
        c.drawString(6.4*inch, y, f"${subtotal:,.2f}")
    else:
        c.drawString(5*inch, y, "Subtotal:")
        c.drawString(6.4*inch, y, f"${subtotal:,.2f}")
        y -= 18
        if deposit and float(deposit) > 0:
            c.drawString(5*inch, y, "Deposit:")
            c.drawString(6.4*inch, y, f"-${float(deposit):,.2f}")
            y -= 18
        c.drawString(5*inch, y, "Grand Total:")
        c.drawString(6.4*inch, y, f"${grand_total:,.2f}")
        if check_number:
            y -= 18
            c.setFont("Helvetica", 10)
            c.drawString(1*inch, y, f"Check #: {check_number}")

    # Notes
    if notes:
        y -= 25
        c.setFont("Helvetica-Oblique", 9)
        for ln in textwrap.wrap(notes, width=90):
            c.drawString(1*inch, y, ln)
            y -= 14

    # Signature
    y -= 40
    if signature_png_bytes:
        sig_reader = ImageReader(io.BytesIO(signature_png_bytes))
        c.drawImage(sig_reader, 1*inch, y, width=150, height=40, mask='auto')
        if signature_date_text:
            c.setFont("Helvetica", 10)
            c.drawString(4.5*inch, y+15, f"Signed: {signature_date_text}")
    else:
        c.setFont("Helvetica", 10)
        c.drawString(1*inch, y, "X ____________________________")
        c.drawString(4*inch, y, "Date: ________________________")

    c.save()
    buf.seek(0)
    return buf.getvalue()

# =========================
# Reset (safe: only clears customer, project, line items)
# =========================
def reset_proposal_form():
    # customer select back to default (we store a placeholder object in state)
    ss["proposal_cust_select"] = {"id": None, "name": "-- Select Customer --"}
    ss["p_project_name"] = ""
    ss["p_project_location"] = ""
    for i in range(ss.line_count):
        ss[f"p_desc_{i}"] = ""
        ss[f"p_qty_{i}"] = 0.0
        ss[f"p_unit_{i}"] = 0.0

def reset_invoice_form():
    ss["invoice_cust_select"] = {"id": None, "name": "-- Select Customer --"}
    ss["i_project_name"] = ""
    ss["i_project_location"] = ""
    for i in range(ss.line_count):
        ss[f"i_desc_{i}"] = ""
        ss[f"i_qty_{i}"] = 0.0
        ss[f"i_unit_{i}"] = 0.0

# =========================
# Tabs
# =========================
prop_tab, inv_tab = st.tabs(["Proposal", "Invoice"])

# -------------------------
# PROPOSAL TAB
# -------------------------
with prop_tab:
    st.subheader("Create Proposal")

    # Customer
    mode = st.radio(
        "Choose Option",
        ["Select Existing Customer", "➕ Add New Customer"],
        key="proposal_cust_mode"
    )
    if mode == "Select Existing Customer":
        customers = get_customers()
        cust_options = [{"id": None, "name": "-- Select Customer --"}] + customers
        # Seed default index once to avoid yellow warnings
        cust = st.selectbox(
            "Customer",
            cust_options,
            index=0,
            format_func=lambda c: c["name"],
            key="proposal_cust_select"
        )
    else:
        new_name = st.text_input("Full Name *", key="proposal_new_name")
        new_email = st.text_input("Email", key="proposal_new_email")
        new_phone = st.text_input("Phone", key="proposal_new_phone")
        new_addr = st.text_input("Street Address", key="proposal_new_addr")
        new_csz = st.text_input("City, State, Zip", key="proposal_new_csz")
        if st.button("💾 Save New Customer (Proposal)", key="proposal_save_customer"):
            with engine.begin() as conn:
                conn.execute(text("""
                    INSERT INTO customers(id,name,email,phone,address,city_state_zip)
                    VALUES(:id,:name,:email,:phone,:addr,:csz)
                    ON CONFLICT(id) DO UPDATE
                    SET name=:name, email=:email, phone=:phone, address=:addr, city_state_zip=:csz
                """), dict(
                    id=new_email or new_phone or new_name,
                    name=new_name, email=new_email, phone=new_phone,
                    addr=new_addr, csz=new_csz
                ))
            st.success("✅ New customer added for proposals.")
            cust = {"id": new_email or new_phone or new_name, "name": new_name}
    # If user hasn’t picked anything yet:
    if mode == "Select Existing Customer" and (not isinstance(ss.get("proposal_cust_select"), dict)):
        ss["proposal_cust_select"] = {"id": None, "name": "-- Select Customer --"}
    cust = ss.get("proposal_cust_select") if mode == "Select Existing Customer" else locals().get("cust", {"id": None})

    # Project info
    p_name_default = ss.get("p_project_name", "")
    p_loc_default  = ss.get("p_project_location", "")
    project_name = st.text_input("Project Name", value=p_name_default, key="p_project_name")
    project_location = st.text_input("Project Location (Address)", value=p_loc_default, key="p_project_location")

    # Line items
    st.markdown("**Line Items**")
    items = []
    for i in range(ss.line_count):
        d_default = ss.get(f"p_desc_{i}", "")
        q_default = ss.get(f"p_qty_{i}", 0.0)
        u_default = ss.get(f"p_unit_{i}", 0.0)
        c1, c2, c3, c4 = st.columns([5,1.5,2,2])
        desc = c1.text_area(f"Description {i+1}", d_default, height=60, key=f"p_desc_{i}")
        qty  = c2.number_input(f"Qty {i+1}", min_value=0.0, step=1.0, key=f"p_qty_{i},", value=q_default if f"p_qty_{i}" not in ss else ss[f"p_qty_{i}"])
        unit = c3.number_input(f"Unit Price {i+1}", min_value=0.0, step=10.0, key=f"p_unit_{i},", value=u_default if f"p_unit_{i}" not in ss else ss[f"p_unit_{i}"])
        # Normalize keys without trailing commas (for internal use)
        if f"p_qty_{i}" not in ss and f"p_qty_{i}," in ss: ss[f"p_qty_{i}"] = ss[f"p_qty_{i},"]; del ss[f"p_qty_{i},"]
        if f"p_unit_{i}" not in ss and f"p_unit_{i}," in ss: ss[f"p_unit_{i}"] = ss[f"p_unit_{i},"]; del ss[f"p_unit_{i},"]

        qty_val  = float(ss.get(f"p_qty_{i}", 0.0))
        unit_val = float(ss.get(f"p_unit_{i}", 0.0))
        c4.write(f"${qty_val*unit_val:,.2f}")
        if str(desc).strip():
            items.append({"Description": desc, "Qty": qty_val, "Unit Price": unit_val})
    st.button("➕ Add Line Item", on_click=add_line, key="p_add_btn")

    subtotal = compute_subtotal(items)
    default_notes = (
        "By signing, the signee agrees to pay the full balance upon project completion, "
        "acknowledges that additional work outside the scope will incur extra charges, "
        "and understands that all manufacturer details are outlined in the product owner’s manual."
    )
    notes = st.text_area("Notes", ss.get("p_notes", default_notes), height=100, key="p_notes")

    # Next Proposal ID
    with engine.begin() as conn:
        next_n = _max_existing_number(conn) + 1
    st.caption(f"Next Proposal ID will be **{format_prop_id(next_n)}** when saved.")

    # Signature
    st.subheader("Signature (optional)")
    proposal_sig_bytes = None
    if st.toggle("Add in-person Signature to Proposal", key="p_sig_toggle"):
        canvas_result = st_canvas(
            fill_color="rgba(255,255,255,0)",
            stroke_width=2,
            stroke_color="black",
            background_color="white",
            width=400,
            height=120,
            drawing_mode="freedraw",
            key="p_sig_canvas",
            display_toolbar=True
        )
        if canvas_result.image_data is not None:
            arr = (canvas_result.image_data[:, :, :3] * 255).astype("uint8")
            sig_img = Image.fromarray(arr)
            buf = io.BytesIO()
            sig_img.save(buf, format="PNG")
            proposal_sig_bytes = buf.getvalue()

    # Proposal PDF + Actions
    pdf_data = build_pdf(
        ref_no=format_prop_id(next_n),
        cust_name=(cust.get("name") if isinstance(cust, dict) else "") or "",
        project_name=project_name,
        project_location=project_location,
        items=items,
        subtotal=subtotal, deposit=0, grand_total=subtotal, check_number=None,
        show_paid=False, notes=notes, is_proposal=True,
        signature_png_bytes=proposal_sig_bytes, signature_date_text=datetime.now().strftime("%m/%d/%Y") if proposal_sig_bytes else None
    )

    cA, cB, cC, cD = st.columns(4)
    with cA:
        st.download_button("📄 Download Proposal", pdf_data, file_name=f"Proposal_{format_prop_id(next_n)}.pdf", key="p_dl_btn")
    with cB:
        if st.button("👀 View Proposal PDF", key="p_view_btn"):
            show_pdf_newtab(pdf_data)
    with cC:
        if st.button("📧 Email Proposal", key="p_email_btn") and isinstance(cust, dict) and cust.get("id"):
            try:
                send_email(pdf_data, cust.get("email"), f"Proposal {format_prop_id(next_n)}",
                           build_email_body(cust.get("name", ""), True, format_prop_id(next_n)),
                           f"Proposal_{format_prop_id(next_n)}.pdf")
                st.success("Proposal emailed.")
            except Exception as e:
                st.error(f"Email failed: {e}")
    with cD:
        if st.button("♻ Reset Form", key="p_reset_btn"):
            reset_proposal_form()
            st.rerun()

    if st.button("💾 Save Proposal", key="p_save_btn") and isinstance(cust, dict) and cust.get("id"):
        with engine.begin() as conn:
            n = _max_existing_number(conn) + 1
            pid = format_prop_id(n)
            conn.execute(text("""
                INSERT INTO proposals (id, number, customer_id, project_name, project_location, items_json, notes, status)
                VALUES (:id, :num, :cid, :pname, :ploc, :items, :notes, 'open')
            """), dict(id=pid, num=n, cid=cust["id"], pname=project_name, ploc=project_location,
                       items=json.dumps(items), notes=notes))
        st.success(f"Proposal {pid} saved!")

    # Active Proposals
    st.markdown("---")
    st.subheader("📋 Active Proposals")
    with engine.begin() as conn:
        props = conn.execute(text("SELECT * FROM proposals WHERE status='open' ORDER BY created_at DESC")).mappings().all()
    customers_lookup = get_customers()

    if not props:
        st.info("No open proposals.")
    else:
        for prop in props:
            with st.expander(f"{prop['id']} — {prop.get('project_name') or ''}"):
                st.write(f"Customer ID: {prop['customer_id']}")
                st.write(f"Location: {prop.get('project_location') or ''}")
                c1, c2, c3 = st.columns(3)

                if c1.button("Convert to Invoice", key=f"conv_{prop['id']}"):
                    with engine.begin() as conn:
                        exists = conn.execute(text("SELECT 1 FROM invoices WHERE number=:n"), {"n": prop["number"]}).fetchone()
                        if not exists:
                            inv_no = format_inv_id(prop["number"])
                            conn.execute(text("""
                                INSERT INTO invoices (invoice_no, number, customer_id, project_name, project_location,
                                                      items_json, total, deposit, check_number, paid)
                                VALUES (:inv, :num, :cid, :pname, :ploc, :items, 0, 0, NULL, FALSE)
                            """), dict(inv=inv_no, num=prop["number"], cid=prop["customer_id"],
                                       pname=prop.get("project_name"), ploc=prop.get("project_location"),
                                       items=prop["items_json"]))
                        conn.execute(text("UPDATE proposals SET status='converted' WHERE id=:id"), {"id": prop["id"]})
                    st.success(f"Converted {prop['id']} → {format_inv_id(prop['number'])}. Switch to the Invoice tab.")
                    st.rerun()

                if c2.button("Close Proposal", key=f"close_{prop['id']}"):
                    with engine.begin() as conn:
                        conn.execute(text("UPDATE proposals SET status='closed' WHERE id=:id"), {"id": prop["id"]})
                    st.warning(f"Proposal {prop['id']} closed.")
                    st.rerun()

                if c3.button("View PDF", key=f"view_{prop['id']}"):
                    prop_items = json.loads(prop["items_json"] or "[]")
                    cust_name = next((c["name"] for c in customers_lookup if c["id"] == prop["customer_id"]), prop["customer_id"])
                    prop_subtotal = compute_subtotal(prop_items)
                    prop_pdf = build_pdf(
                        ref_no=prop["id"], cust_name=cust_name,
                        project_name=prop.get("project_name"), project_location=prop.get("project_location"),
                        items=prop_items, subtotal=prop_subtotal, deposit=0, grand_total=prop_subtotal,
                        check_number=None, is_proposal=True, notes=prop.get("notes")
                    )
                    show_pdf_newtab(prop_pdf)

# -------------------------
# INVOICE TAB
# -------------------------
with inv_tab:
    st.subheader("Create / Manage Invoice")

    # Customer
    mode = st.radio(
        "Choose Option",
        ["Select Existing Customer", "➕ Add New Customer"],
        key="invoice_cust_mode"
    )
    if mode == "Select Existing Customer":
        customers = get_customers()
        cust_options = [{"id": None, "name": "-- Select Customer --"}] + customers
        cust = st.selectbox(
            "Customer",
            cust_options,
            index=0,
            format_func=lambda c: c["name"],
            key="invoice_cust_select"
        )
    else:
        new_name = st.text_input("Full Name *", key="invoice_new_name")
        new_email = st.text_input("Email", key="invoice_new_email")
        new_phone = st.text_input("Phone", key="invoice_new_phone")
        new_addr = st.text_input("Street Address", key="invoice_new_addr")
        new_csz = st.text_input("City, State, Zip", key="invoice_new_csz")
        if st.button("💾 Save New Customer (Invoice)", key="invoice_save_customer"):
            with engine.begin() as conn:
                conn.execute(text("""
                    INSERT INTO customers(id,name,email,phone,address,city_state_zip)
                    VALUES(:id,:name,:email,:phone,:addr,:csz)
                    ON CONFLICT(id) DO UPDATE
                    SET name=:name, email=:email, phone=:phone, address=:addr, city_state_zip=:csz
                """), dict(
                    id=new_email or new_phone or new_name,
                    name=new_name, email=new_email, phone=new_phone,
                    addr=new_addr, csz=new_csz
                ))
            st.success("✅ New customer added for invoices.")
            cust = {"id": new_email or new_phone or new_name, "name": new_name}
    if mode == "Select Existing Customer" and (not isinstance(ss.get("invoice_cust_select"), dict)):
        ss["invoice_cust_select"] = {"id": None, "name": "-- Select Customer --"}
    cust = ss.get("invoice_cust_select") if mode == "Select Existing Customer" else locals().get("cust", {"id": None})

    # Invoice Number (auto; keeps proposal number if converting)
    with engine.begin() as conn:
        max_all = _max_existing_number(conn)
    inv_no_suggested = format_inv_id(max_all + 1)
    inv_no = st.text_input("Invoice #", value=inv_no_suggested if "i_inv_no" not in ss else ss["i_inv_no"], key="i_inv_no")

    # Project info
    i_name_default = ss.get("i_project_name", "")
    i_loc_default  = ss.get("i_project_location", "")
    project_name = st.text_input("Project Name", value=i_name_default, key="i_project_name")
    project_location = st.text_input("Project Location (Address)", value=i_loc_default, key="i_project_location")

    # Line items
    st.markdown("**Line Items**")
    items = []
    for i in range(ss.line_count):
        d_default = ss.get(f"i_desc_{i}", "")
        q_default = ss.get(f"i_qty_{i}", 0.0)
        u_default = ss.get(f"i_unit_{i}", 0.0)
        c1, c2, c3, c4 = st.columns([5,1.5,2,2])
        desc = c1.text_area(f"Description {i+1}", d_default, height=60, key=f"i_desc_{i}")
        qty  = c2.number_input(f"Qty {i+1}", min_value=0.0, step=1.0, key=f"i_qty_{i},", value=q_default if f"i_qty_{i}" not in ss else ss[f"i_qty_{i}"])
        unit = c3.number_input(f"Unit Price {i+1}", min_value=0.0, step=10.0, key=f"i_unit_{i},", value=u_default if f"i_unit_{i}" not in ss else ss[f"i_unit_{i}"])
        if f"i_qty_{i}" not in ss and f"i_qty_{i}," in ss: ss[f"i_qty_{i}"] = ss[f"i_qty_{i},"]; del ss[f"i_qty_{i},"]
        if f"i_unit_{i}" not in ss and f"i_unit_{i}," in ss: ss[f"i_unit_{i}"] = ss[f"i_unit_{i},"]; del ss[f"i_unit_{i},"]

        qty_val  = float(ss.get(f"i_qty_{i}", 0.0))
        unit_val = float(ss.get(f"i_unit_{i}", 0.0))
        c4.write(f"${qty_val*unit_val:,.2f}")
        if str(desc).strip():
            items.append({"Description": desc, "Qty": qty_val, "Unit Price": unit_val})
    st.button("➕ Add Line Item", on_click=add_line, key="i_add_btn")

    # Totals / Payments
    subtotal = compute_subtotal(items)
    deposit = st.number_input("Deposit Amount", min_value=0.0, value=ss.get("i_deposit", 0.0), step=50.0, key="i_deposit")
    chk_no = st.text_input("Check Number (if paying by check)", ss.get("i_checknum", ""), key="i_checknum")
    show_paid = st.toggle("Show PAID Stamp", value=ss.get("i_paid_toggle", False), key="i_paid_toggle")
    grand_total = max(0.0, subtotal - float(deposit or 0))
    invoice_notes = "Thank you for your business!"

    # Signature
    st.subheader("Signature (optional)")
    invoice_sig_bytes = None
    if st.toggle("Add in-person Signature to Invoice", key="i_sig_toggle"):
        canvas_result = st_canvas(
            fill_color="rgba(255,255,255,0)",
            stroke_width=2,
            stroke_color="black",
            background_color="white",
            width=400,
            height=120,
            drawing_mode="freedraw",
            key="i_sig_canvas",
            display_toolbar=True
        )
        if canvas_result.image_data is not None:
            arr = (canvas_result.image_data[:, :, :3] * 255).astype("uint8")
            sig_img = Image.fromarray(arr)
            buf = io.BytesIO()
            sig_img.save(buf, format="PNG")
            invoice_sig_bytes = buf.getvalue()

    # Invoice PDF + Actions
    pdf_data = build_pdf(
        ref_no=inv_no, cust_name=(cust.get("name") if isinstance(cust, dict) else "") or "",
        project_name=project_name, project_location=project_location,
        items=items, subtotal=subtotal, deposit=deposit, grand_total=grand_total, check_number=chk_no,
        show_paid=show_paid, notes=invoice_notes, is_proposal=False,
        signature_png_bytes=invoice_sig_bytes,
        signature_date_text=datetime.now().strftime("%m/%d/%Y") if invoice_sig_bytes else None
    )

    cA, cB, cC, cD = st.columns(4)
    with cA:
        st.download_button("📄 Download Invoice", pdf_data, file_name=f"Invoice_{inv_no}.pdf", key="i_dl_btn")
    with cB:
        if st.button("👀 View Invoice PDF", key="i_view_btn"):
            show_pdf_newtab(pdf_data)
    with cC:
        if st.button("📧 Email Invoice", key="i_email_btn") and isinstance(cust, dict) and cust.get("id"):
            try:
                send_email(pdf_data, cust.get("email"), f"Invoice {inv_no}",
                           build_email_body(cust.get("name",""), False, inv_no),
                           f"Invoice_{inv_no}.pdf")
                st.success("Invoice emailed.")
            except Exception as e:
                st.error(f"Email failed: {e}")
    with cD:
        if st.button("♻ Reset Form", key="i_reset_btn"):
            reset_invoice_form()
            st.rerun()

    # Save Invoice (insert/update by numeric part)
    if st.button("💾 Save Invoice", key="i_save_btn") and isinstance(cust, dict) and cust.get("id"):
        try:
            number_part = int(inv_no.split("-")[-1])
        except Exception:
            st.error("Invoice # must look like INV-1001")
            st.stop()
        with engine.begin() as conn:
            existing = conn.execute(text("SELECT invoice_no FROM invoices WHERE number=:n"), {"n": number_part}).fetchone()
            if existing:
                conn.execute(text("""
                    UPDATE invoices
                    SET customer_id=:cid, project_name=:pname, project_location=:ploc,
                        items_json=:items, total=:total, deposit=:dep, check_number=:chk, paid=:paid
                    WHERE number=:n
                """), dict(cid=cust["id"], pname=project_name, ploc=project_location,
                           items=json.dumps(items), total=grand_total, dep=deposit,
                           chk=chk_no, paid=show_paid, n=number_part))
            else:
                conn.execute(text("""
                    INSERT INTO invoices (invoice_no, number, customer_id, project_name, project_location,
                                          items_json, total, deposit, check_number, paid)
                    VALUES (:inv,:n,:cid,:pname,:ploc,:items,:total,:dep,:chk,:paid)
                """), dict(inv=inv_no, n=number_part, cid=cust["id"], pname=project_name,
                           ploc=project_location, items=json.dumps(items),
                           total=grand_total, dep=deposit, chk=chk_no, paid=show_paid))
        st.success(f"Invoice {inv_no} saved!")

    # Recent Invoices (paid toggle, view/download)
    st.markdown("---")
    st.subheader("🧾 Recent Invoices")
    with engine.begin() as conn:
        invs = conn.execute(text("""
            SELECT invoice_no, customer_id, project_name, project_location, items_json, total, deposit, check_number, paid, created_at
            FROM invoices ORDER BY created_at DESC LIMIT 20
        """)).mappings().all()

    if not invs:
        st.info("No invoices yet.")
    else:
        for inv in invs:
            tot = float(inv["total"] or 0)
            with st.expander(f"{inv['invoice_no']} — {inv.get('project_name') or ''} — ${tot:,.2f}"):
                st.write(f"Customer ID: {inv['customer_id']}")
                paid_state = st.checkbox("Paid?", value=bool(inv["paid"]), key=f"paid_{inv['invoice_no']}")
                if paid_state != bool(inv["paid"]):
                    with engine.begin() as conn:
                        conn.execute(text("UPDATE invoices SET paid=:p WHERE invoice_no=:id"),
                                     {"p": paid_state, "id": inv["invoice_no"]})
                    st.success(f"{inv['invoice_no']} updated → {'Paid' if paid_state else 'Unpaid'}")
                    st.rerun()

                c1, c2 = st.columns(2)
                if c1.button("👀 View PDF", key=f"view_{inv['invoice_no']}"):
                    items_pdf = json.loads(inv["items_json"] or "[]")
                    subtotal_pdf = compute_subtotal(items_pdf)
                    pdf = build_pdf(
                        ref_no=inv["invoice_no"],
                        cust_name=inv["customer_id"],  # (join to customers if you want names here)
                        project_name=inv.get("project_name"),
                        project_location=inv.get("project_location"),
                        items=items_pdf, subtotal=subtotal_pdf, deposit=inv.get("deposit") or 0,
                        grand_total=inv.get("total") or subtotal_pdf, check_number=inv.get("check_number"),
                        show_paid=bool(inv.get("paid")), notes="Thank you for your business!", is_proposal=False
                    )
                    show_pdf_newtab(pdf)

                c2.download_button(
                    "⬇️ Download PDF",
                    data=build_pdf(
                        ref_no=inv["invoice_no"],
                        cust_name=inv["customer_id"],
                        project_name=inv.get("project_name"),
                        project_location=inv.get("project_location"),
                        items=json.loads(inv["items_json"] or "[]"),
                        subtotal=compute_subtotal(json.loads(inv["items_json"] or "[]")),
                        deposit=inv.get("deposit") or 0,
                        grand_total=inv.get("total") or 0,
                        check_number=inv.get("check_number"),
                        show_paid=bool(inv.get("paid")),
                        notes="Thank you for your business!",
                        is_proposal=False
                    ),
                    file_name=f"{inv['invoice_no']}.pdf",
                    key=f"dl_{inv['invoice_no']}"
                )
