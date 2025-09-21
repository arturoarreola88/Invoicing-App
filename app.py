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

st.set_page_config(page_title="J&I Proposals & Invoices", page_icon="🧾", layout="centered")

# ---- Header logo ----
try:
    st.image("logo.png", width=220)
except:
    st.write("⚠️ Put a 'logo.png' next to app.py for header + PDFs.")

st.title("🧾 J & I — Proposals & Invoices")

# ---- Secrets / env ----
DATABASE_URL = st.secrets.get("DATABASE_URL", os.getenv("DATABASE_URL", ""))
FROM_EMAIL   = st.secrets.get("FROM_EMAIL",   os.getenv("FROM_EMAIL",   "jiheatingcooling.homerepairs@gmail.com"))
SMTP_SERVER  = st.secrets.get("SMTP_SERVER",  os.getenv("SMTP_SERVER",  "smtp.gmail.com"))
SMTP_PORT    = int(st.secrets.get("SMTP_PORT", os.getenv("SMTP_PORT", 465)))
APP_PASSWORD = st.secrets.get("APP_PASSWORD", os.getenv("APP_PASSWORD", ""))

if not DATABASE_URL:
    st.error("DATABASE_URL not set in Secrets.")
    st.stop()

engine = create_engine(DATABASE_URL, pool_pre_ping=True)

# ---- DB init ----
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
              id TEXT PRIMARY KEY,
              number INTEGER NOT NULL,
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
            CREATE TABLE IF NOT EXISTS invoices(
              id SERIAL PRIMARY KEY,
              invoice_no TEXT UNIQUE,
              number INTEGER NOT NULL,
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
# ---- DB Migration ----
def migrate_db():
    with engine.begin() as conn:
        # Check if proposals.number exists
        res = conn.execute(text("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name='proposals' AND column_name='number'
        """)).fetchone()
        if not res:
            conn.execute(text("ALTER TABLE proposals ADD COLUMN number INTEGER"))
            conn.execute(text("""
                UPDATE proposals
                SET number = CAST(REGEXP_REPLACE(id, '\\D', '', 'g') AS INTEGER)
                WHERE number IS NULL
            """))

        # Check if invoices.number exists
        res = conn.execute(text("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name='invoices' AND column_name='number'
        """)).fetchone()
        if not res:
            conn.execute(text("ALTER TABLE invoices ADD COLUMN number INTEGER"))
            conn.execute(text("""
                UPDATE invoices
                SET number = CAST(REGEXP_REPLACE(invoice_no, '\\D', '', 'g') AS INTEGER)
                WHERE number IS NULL
            """))

# Run migrations
migrate_db()
# ---- Session defaults ----
ss = st.session_state
ss.setdefault("prefill_items", [])
ss.setdefault("prefill_customer_id", None)
ss.setdefault("project_name_value", "")
ss.setdefault("project_location_value", "")
ss.setdefault("line_count", 5)

def add_line():
    ss.line_count += 1

def prefill_from_proposal(prop):
    ss.prefill_customer_id = prop["customer_id"]
    ss.prefill_items = json.loads(prop["items_json"] or "[]")
    ss.project_name_value = prop.get("project_name") or ""
    ss.project_location_value = prop.get("project_location") or ""
    ss.prefill_proposal_number = prop["number"]
    ss.prefill_proposal_id = prop["id"]

# ---- Utilities ----
def compute_subtotal(items):
    return sum(float(r.get("Qty", 0)) * float(r.get("Unit Price", 0)) for r in items)

def show_pdf_newtab(pdf_bytes):
    b64 = base64.b64encode(pdf_bytes).decode("utf-8")
    st.markdown(f'<a href="data:application/pdf;base64,{b64}" target="_blank">📄 Open PDF in New Tab</a>', unsafe_allow_html=True)

def _max_existing_number(conn):
    r = conn.execute(text("SELECT COALESCE(MAX(number), 0) FROM proposals")).scalar() or 0
    r2 = conn.execute(text("SELECT COALESCE(MAX(number), 0) FROM invoices")).scalar() or 0
    return max(r, r2)

def next_number():
    with engine.begin() as conn:
        return _max_existing_number(conn) + 1

def format_prop_id(n): return f"P-{n:04d}"
def format_inv_id(n):  return f"INV-{n:04d}"

def build_email_body(cust_name, is_proposal, ref_no):
    hour = datetime.now().hour
    greeting = "Good morning" if hour < 12 else "Good afternoon"
    kind = "proposal" if is_proposal else "invoice"
    return f"""
    <p>{greeting} {cust_name},</p>
    <p>Attached is the {kind} ({ref_no}) you requested. Please review and
    contact me with any questions.</p>
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

# ---- PDF builder with signature support ----
def build_pdf(ref_no, cust_name, project_name, project_location, items,
              subtotal, deposit, grand_total, check_number,
              show_paid=False, notes=None, is_proposal=False,
              signature_png_bytes=None, signature_date_text=None):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=LETTER)
    width, height = LETTER
    page_num = 1

    # header
    try:
        logo = ImageReader("logo.png")
        c.drawImage(logo, width - 100 - 0.5*inch, height - 60 - 0.5*inch,
                    width=100, height=60, preserveAspectRatio=True, mask='auto')
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

    right_x = width - 2.5*inch
    c.setFont("Helvetica", 10)
    c.drawString(right_x, height-2.3*inch, f"Date: {issue_date.strftime('%m/%d/%Y')}")
    c.drawString(right_x, height-2.6*inch, terms_text)

    if show_paid and not is_proposal:
        c.setFont("Helvetica-Bold", 36)
        c.setFillColorRGB(1,0,0)
        offset_x = width/2 + 0.75*inch
        c.drawString(offset_x, height-3.0*inch, "PAID")
        c.setFont("Helvetica", 12)
        c.drawString(offset_x, height-3.4*inch, datetime.now().strftime("%m/%d/%Y"))
        c.setFillColorRGB(0,0,0)

    y = height-3.6*inch
    c.setFont("Helvetica-Bold", 10)
    c.drawString(1*inch, y, "Description")
    c.drawString(4*inch, y, "Qty")
    c.drawString(5*inch, y, "Unit")
    c.drawString(6*inch, y, "Line Total")
    y -= 14
    c.setFont("Helvetica", 10)

    for row in items:
        desc = str(row.get("Description", ""))
        wrapped = textwrap.wrap(desc, width=50)
        for j, line in enumerate(wrapped):
            c.drawString(1*inch, y, line)
            if j == 0:
                qty = float(row.get("Qty", 0))
                unit = float(row.get("Unit Price", 0))
                c.drawString(4*inch, y, f"{qty:.2f}")
                c.drawString(5*inch, y, f"${unit:.2f}")
                c.drawString(6*inch, y, f"${qty*unit:.2f}")
            y -= 16

    y -= 8
    c.setFont("Helvetica-Bold", 11)
    if is_proposal:
        c.drawString(5*inch, y, "Subtotal:")
        c.drawString(6*inch, y, f"${subtotal:,.2f}")
        y -= 18
        c.drawString(5*inch, y, "Grand Total:")
        c.drawString(6*inch, y, f"${subtotal:,.2f}")
    else:
        c.drawString(5*inch, y, "Subtotal:")
        c.drawString(6*inch, y, f"${subtotal:,.2f}")
        y -= 18
        if deposit and float(deposit) > 0:
            c.drawString(5*inch, y, "Deposit:")
            c.drawString(6*inch, y, f"-${float(deposit):,.2f}")
            y -= 18
        c.drawString(5*inch, y, "Grand Total:")
        c.drawString(6*inch, y, f"${grand_total:,.2f}")
        if check_number:
            y -= 18
            c.setFont("Helvetica", 10)
            c.drawString(1*inch, y, f"Check #: {check_number}")

    if notes:
        y -= 25
        c.setFont("Helvetica-Oblique", 9)
        for ln in textwrap.wrap(notes, width=90):
            c.drawString(1*inch, y, ln)
            y -= 14

    # Signature lines
    y -= 40
    c.setFont("Helvetica", 10)
    c.drawString(1*inch, y, "X ____________________________")
    c.drawString(4*inch, y, "Date: ________________________")

    if signature_png_bytes:
        sig_reader = ImageReader(io.BytesIO(signature_png_bytes))
        c.drawImage(sig_reader, 1*inch, y-30, width=150, height=40, mask='auto')
    if signature_date_text:
        c.setFont("Helvetica", 10)
        c.drawString(4.5*inch, y-15, signature_date_text)

    c.save()
    buf.seek(0)
    return buf.getvalue()

# ===== PROPOSAL TAB =====
prop_tab, inv_tab =st.tabs(["Proposal", "Invoice"])

with prop_tab:
    st.subheader("Create Proposal")

    # Customers
    with engine.begin() as conn:
        customers = conn.execute(text("SELECT * FROM customers ORDER BY name")).mappings().all()
    cust_options = [{"id": None, "name": "-- Select Customer --"}] + customers
    cust = st.selectbox("Customer", cust_options, index=0, format_func=lambda c: c["name"])
    if not cust["id"]:
        st.warning("Please select a customer before saving.")

    # project fields
    project_name = st.text_input("Project Name", ss.project_name_value)
    project_location = st.text_input("Project Location (Address)", ss.project_location_value)

    # line items
    st.markdown("**Line Items**")
    items = []
    prefill = ss.prefill_items or []
    for i in range(ss.line_count):
        d = prefill[i]["Description"] if i < len(prefill) else ""
        q = float(prefill[i]["Qty"]) if i < len(prefill) else 1.0
        u = float(prefill[i]["Unit Price"]) if i < len(prefill) else 0.0
        c1, c2, c3, c4 = st.columns([5,1.5,2,2])
        desc = c1.text_input(f"Description {i+1}", d, key=f"p_desc_{i}")
        qty  = c2.number_input(f"Qty {i+1}", min_value=0.0, value=q, step=1.0, key=f"p_qty_{i}")
        unit = c3.number_input(f"Unit Price {i+1}", min_value=0.0, value=u, step=10.0, key=f"p_unit_{i}")
        c4.write(f"${qty*unit:,.2f}")
        if str(desc).strip():
            items.append({"Description": desc, "Qty": qty, "Unit Price": unit})
    st.button("➕ Add Line Item", on_click=add_line, key="p_add")

    subtotal = compute_subtotal(items)
    default_notes = (
        "By signing, the signee agrees to pay the full balance upon project completion, "
        "acknowledges that additional work outside the scope will incur extra charges on the final invoice, "
        "and understands that all manufacturer details are outlined in the product owner’s manual."
    )
    notes = st.text_area("Notes", default_notes, height=100)

    # ID preview
    with engine.begin() as conn:
        next_n = _max_existing_number(conn) + 1
    st.caption(f"Next Proposal ID will be **{format_prop_id(next_n)}** when saved.")

    # signature capture
    st.subheader("Signature (optional)")
    canvas_result = st_canvas(
        fill_color="rgba(255,255,255,0)",
        stroke_width=2,
        stroke_color="black",
        background_color="white",
        width=400,
        height=120,
        drawing_mode="freedraw",
        key="proposal_sig",
        display_toolbar=True
    )
    sig_bytes = None
    if canvas_result.image_data is not None:
        import cv2, numpy as np
        img = (canvas_result.image_data[:,:,:3] * 255).astype("uint8")
        _, buf = cv2.imencode(".png", img)
        sig_bytes = buf.tobytes()

    # build PDF preview
    pdf_data = build_pdf(
        ref_no=format_prop_id(next_n),
        cust_name=cust["name"] if cust["id"] else "",
        project_name=project_name,
        project_location=project_location,
        items=items,
        subtotal=subtotal, deposit=0, grand_total=subtotal, check_number=None,
        show_paid=False, notes=notes, is_proposal=True,
        signature_png_bytes=sig_bytes, signature_date_text=None
    )

    cA, cB, cC = st.columns(3)
    with cA:
        st.download_button("📄 Download Proposal", pdf_data, file_name=f"Proposal_{format_prop_id(next_n)}.pdf")
    with cB:
        if st.button("👀 View Proposal PDF"):
            show_pdf_newtab(pdf_data)
    with cC:
        if st.button("📧 Email Proposal") and cust["id"]:
            try:
                send_email(pdf_data, cust.get("email"), f"Proposal {format_prop_id(next_n)}",
                           build_email_body(cust["name"], True, format_prop_id(next_n)),
                           f"Proposal_{format_prop_id(next_n)}.pdf")
                st.success("Proposal emailed.")
            except Exception as e:
                st.error(f"Email failed: {e}")

    if st.button("💾 Save Proposal") and cust["id"]:
        with engine.begin() as conn:
            n = _max_existing_number(conn) + 1
            pid = format_prop_id(n)
            conn.execute(text("""
                INSERT INTO proposals (id, number, customer_id, project_name, project_location, items_json, notes, status)
                VALUES (:id, :num, :cid, :pname, :ploc, :items, :notes, 'open')
            """), dict(id=pid, num=n, cid=cust["id"], pname=project_name, ploc=project_location,
                       items=json.dumps(items), notes=notes))
        st.success(f"Proposal {pid} saved!")

    # Active proposals
    st.markdown("---")
    st.subheader("📋 Active Proposals")
    with engine.begin() as conn:
        props = conn.execute(text("SELECT * FROM proposals WHERE status='open' ORDER BY created_at DESC")).mappings().all()

    if not props:
        st.info("No open proposals.")
    else:
        for prop in props:
            with st.expander(f"{prop['id']} — {prop.get('project_name') or ''}"):
                st.write(f"Customer ID: {prop['customer_id']}")
                st.write(f"Location: {prop.get('project_location') or ''}")
                c1, c2, c3 = st.columns(3)

                if c1.button("Convert to Invoice", key=f"conv_{prop['id']}"):
                    prefill_from_proposal(prop)
                    with engine.begin() as conn:
                        existing = conn.execute(text("SELECT 1 FROM invoices WHERE number=:n"), {"n": prop["number"]}).fetchone()
                        if not existing:
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
                    cust_name = next((c["name"] for c in customers if c["id"] == prop["customer_id"]), prop["customer_id"])
                    prop_subtotal = compute_subtotal(prop_items)
                    prop_pdf = build_pdf(
                        ref_no=prop["id"], cust_name=cust_name,
                        project_name=prop.get("project_name"), project_location=prop.get("project_location"),
                        items=prop_items, subtotal=prop_subtotal, deposit=0, grand_total=prop_subtotal,
                        check_number=None, is_proposal=True, notes=prop.get("notes")
                    )
                    show_pdf_newtab(prop_pdf)
# ===== INVOICE TAB =====
with inv_tab:
    st.subheader("Create / Manage Invoice")

    with engine.begin() as conn:
        customers = conn.execute(text("SELECT * FROM customers ORDER BY name")).mappings().all()
    cust_options = [{"id": None, "name": "-- Select Customer --"}] + customers
    cust = st.selectbox("Customer", cust_options, index=0, format_func=lambda c: c["name"])
    if not cust["id"]:
        st.warning("Please select a customer before saving.")

    with engine.begin() as conn:
        max_all = _max_existing_number(conn)
    default_num = ss.get("prefill_proposal_number", None)
    if default_num:
        inv_no = format_inv_id(default_num)
    else:
        inv_no = format_inv_id(max_all + 1)
    inv_no = st.text_input("Invoice #", inv_no)

    project_name = st.text_input("Project Name", ss.project_name_value, key="i_pname")
    project_location = st.text_input("Project Location (Address)", ss.project_location_value, key="i_ploc")

    st.markdown("**Line Items**")
    items = []
    prefill = ss.prefill_items or []
    for i in range(ss.line_count):
        d = prefill[i]["Description"] if i < len(prefill) else ""
        q = float(prefill[i]["Qty"]) if i < len(prefill) else 1.0
        u = float(prefill[i]["Unit Price"]) if i < len(prefill) else 0.0
        c1, c2, c3, c4 = st.columns([5,1.5,2,2])
        desc = c1.text_input(f"Description {i+1}", d, key=f"i_desc_{i}")
        qty  = c2.number_input(f"Qty {i+1}", min_value=0.0, value=q, step=1.0, key=f"i_qty_{i}")
        unit = c3.number_input(f"Unit Price {i+1}", min_value=0.0, value=u, step=10.0, key=f"i_unit_{i}")
        c4.write(f"${qty*unit:,.2f}")
        if str(desc).strip():
            items.append({"Description": desc, "Qty": qty, "Unit Price": unit})
    st.button("➕ Add Line Item", on_click=add_line, key="i_add")

    subtotal = compute_subtotal(items)
    deposit = st.number_input("Deposit Amount", min_value=0.0, value=0.0, step=50.0)
    chk_no = st.text_input("Check Number (if paying by check)", "")
    show_paid = st.toggle("Show PAID Stamp", value=False)
    grand_total = max(0.0, subtotal - deposit)
    invoice_notes = "Thank you for your business!"

    # signature
    st.subheader("Signature (optional)")
    canvas_result = st_canvas(
        fill_color="rgba(255,255,255,0)",
        stroke_width=2,
        stroke_color="black",
        background_color="white",
        width=400,
        height=120,
        drawing_mode="freedraw",
        key="invoice_sig",
        display_toolbar=True
    )
    sig_bytes = None
    if canvas_result.image_data is not None:
        import cv2, numpy as np
        img = (canvas_result.image_data[:,:,:3] * 255).astype("uint8")
        _, buf = cv2.imencode(".png", img)
        sig_bytes = buf.tobytes()

    pdf_data = build_pdf(
        ref_no=inv_no, cust_name=cust["name"] if cust["id"] else "",
        project_name=project_name, project_location=project_location,
        items=items, subtotal=subtotal, deposit=deposit, grand_total=grand_total, check_number=chk_no,
        show_paid=show_paid, notes=invoice_notes, is_proposal=False,
        signature_png_bytes=sig_bytes, signature_date_text=datetime.now().strftime("%m/%d/%Y") if sig_bytes else None
    )

    cA, cB, cC = st.columns(3)
    with cA:
        st.download_button("📄 Download Invoice", pdf_data, file_name=f"Invoice_{inv_no}.pdf")
    with cB:
        if st.button("👀 View Invoice PDF"):
            show_pdf_newtab(pdf_data)
    with cC:
        if st.button("📧 Email Invoice") and cust["id"]:
            try:
                send_email(pdf_data, cust.get("email"), f"Invoice {inv_no}",
                           build_email_body(cust["name"], False, inv_no),
                           f"Invoice_{inv_no}.pdf")
                st.success("Invoice emailed.")
            except Exception as e:
                st.error(f"Email failed: {e}")

    if st.button("💾 Save Invoice") and cust["id"]:
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

    st.markdown("---")
    st.subheader("🧾 Recent Invoices")
    with engine.begin() as conn:
        invs = conn.execute(text("""
            SELECT invoice_no, customer_id, project_name, total, paid, created_at
            FROM invoices ORDER BY created_at DESC LIMIT 20
        """)).mappings().all()
    if not invs:
        st.info("No invoices yet.")
    else:
        for inv in invs:
            with st.expander(f"{inv['invoice_no']} — {inv.get('project_name') or ''} — ${float(inv['total'] or 0):,.2f}"):
                st.write(f"Customer ID: {inv['customer_id']}")
                st.write(f"Total: ${float(inv['total'] or 0):,.2f}")
                st.write("Paid: ✅" if inv["paid"] else "Paid: ❌")

                c1, c2 = st.columns(2)
                if c1.button("Mark Paid / Unpaid", key=f"toggle_{inv['invoice_no']}"):
                    with engine.begin() as conn:
                        conn.execute(text("UPDATE invoices SET paid = NOT paid WHERE invoice_no=:id"),
                                     {"id": inv["invoice_no"]})
                    st.rerun()

                if c2.button("View PDF", key=f"view_{inv['invoice_no']}"):
                    with engine.begin() as conn:
                        row = conn.execute(text("SELECT * FROM invoices WHERE invoice_no=:id"),
                                           {"id": inv["invoice_no"]}).mappings().first()
                    cust_name = next((c["name"] for c in customers if c["id"] == row["customer_id"]), row["customer_id"])
                    items_pdf = json.loads(row["items_json"] or "[]")
                    subtotal_pdf = compute_subtotal(items_pdf)
                    pdf = build_pdf(
                        ref_no=row["invoice_no"], cust_name=cust_name,
                        project_name=row.get("project_name"), project_location=row.get("project_location"),
                        items=items_pdf, subtotal=subtotal_pdf, deposit=row.get("deposit") or 0,
                        grand_total=row.get("total") or subtotal_pdf, check_number=row.get("check_number"),
                        show_paid=bool(row.get("paid")), notes="Thank you for your business!", is_proposal=False
                    )
                    show_pdf_newtab(pdf)

                
# app.py (Invoicing App with Postgres)

