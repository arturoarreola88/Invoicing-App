import os, json, io, smtplib, textwrap, base64
from datetime import datetime, timedelta
from email.message import EmailMessage

import streamlit as st
import streamlit.components.v1 as components
from streamlit_drawable_canvas import st_canvas

from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader

from sqlalchemy import create_engine, text

st.set_page_config(page_title="J&I Invoicing", page_icon="🧾", layout="centered")

# ---------- Logo ----------
try:
    st.image("logo.png", width=200)
except:
    st.write("⚠️ Place logo.png in the same folder as app.py")

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

def show_pdf_inline(pdf_bytes, height=900):
    b64 = base64.b64encode(pdf_bytes).decode("utf-8")
    html = f'<iframe src="data:application/pdf;base64,{b64}" width="100%" height="{height}" type="application/pdf"></iframe>'
    components.html(html, height=height, scrolling=True)

def build_pdf(invoice_no, cust_name, project_name, project_location, items,
              subtotal, deposit, grand_total, check_number,
              show_paid=False, notes=None, is_proposal=False,
              signature_png_bytes=None, signature_date_text=None):
    """Generate Proposal/Invoice PDF with header/footer, wrapped text, terms/dates, and optional signature overlay."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=LETTER)
    width, height = LETTER
    page_num = 1

    # --- Footer & Header ---
    def draw_footer(canv, pnum):
        canv.setFont("Helvetica-Oblique", 9)
        canv.setFillColorRGB(0.3, 0.3, 0.3)
        footer_text = f"Thank you for your business!  •  J & I Heating and Cooling  •  (630) 849-0385  •  Sandwich, IL   |   Page {pnum}"
        canv.drawCentredString(width/2, 0.5*inch, footer_text)

    def draw_header():
        try:
            logo = ImageReader("logo.png")
            logo_w = 120
            logo_x = (width - logo_w) / 2
            c.drawImage(logo, logo_x, height-1.2*inch, width=logo_w, preserveAspectRatio=True, mask='auto')
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

    # Header info & Terms
    issue_date = datetime.now().date()
    if is_proposal:
        terms_date = issue_date + timedelta(days=15)
        terms_text = f"Valid until: {terms_date.strftime('%m/%d/%Y')}"
        heading = "Proposal"
    else:
        terms_text = f"Due Date: {issue_date.strftime('%m/%d/%Y')}"
        heading = "Invoice"

    c.setFont("Helvetica", 12)
    c.drawString(1*inch, height-3.1*inch, f"{heading} #: {invoice_no}")
    c.drawString(1*inch, height-3.3*inch, f"Customer: {cust_name}")
    c.drawString(1*inch, height-3.5*inch, f"Project: {project_name or ''}")
    c.drawString(1*inch, height-3.7*inch, f"Location: {project_location or ''}")
    c.setFont("Helvetica", 10)
    c.drawString(1*inch, height-3.9*inch, f"Date: {issue_date.strftime('%m/%d/%Y')}")
    c.drawString(3.5*inch, height-3.9*inch, terms_text)

    # "PAID" stamp + date (top-right)
    if show_paid and not is_proposal:
        c.setFont("Helvetica-Bold", 36)
        c.setFillColorRGB(1, 0, 0)
        c.drawString(4.5*inch, height-3.1*inch, "PAID")
        c.setFont("Helvetica", 12)
        c.drawString(4.5*inch, height-3.4*inch, datetime.now().strftime("%m/%d/%Y"))
        c.setFillColorRGB(0, 0, 0)

    # Line items (wrapped)
    y = height-4.2*inch
    c.setFont("Helvetica-Bold", 10)
    c.drawString(1*inch, y, "Description")
    c.drawString(4*inch, y, "Qty")
    c.drawString(5*inch, y, "Unit")
    c.drawString(6*inch, y, "Line Total")
    y -= 14
    c.setFont("Helvetica", 10)

    for row in items:
        desc_text = str(row.get("Description", ""))
        wrapped_desc = textwrap.wrap(desc_text, width=50)  # keep in Description column
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
                qty = float(row.get('Qty', 0))
                unit = float(row.get('Unit Price', 0))
                c.drawString(4*inch, y, f"{qty:.2f}")
                c.drawString(5*inch, y, f"${unit:.2f}")
                c.drawString(6*inch, y, f"${qty*unit:.2f}")
            y -= 12

    # Totals
    y -= 8
    c.setFont("Helvetica-Bold", 11)
    if is_proposal:
        c.drawString(5*inch, y, "Subtotal:")
        c.drawString(6*inch, y, f"${subtotal:,.2f}")
        y -= 15
        c.drawString(5*inch, y, "Grand Total:")
        c.drawString(6*inch, y, f"${subtotal:,.2f}")
        y -= 15
    else:
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

    # Signature lines
    y -= 30
    if y < 1.2*inch:
        draw_footer(c, page_num)
        c.showPage()
        page_num += 1
        new_page(page_num)
        y = height - 1.2*inch

    c.setFont("Helvetica", 10)
    c.drawString(1*inch, y, "X ____________________________")
    c.drawString(4*inch, y, "Date: ________________________")
    y -= 5

    # Optional signature image overlay + date
    if signature_png_bytes:
        try:
            sig_reader = ImageReader(io.BytesIO(signature_png_bytes))
            # place signature image roughly over the first line
            c.drawImage(sig_reader, 1.0*inch, y, width=2.5*inch, height=0.7*inch, mask='auto')
        except Exception as e:
            print("Signature draw failed:", e)
    if signature_date_text:
        c.setFont("Helvetica", 10)
        c.drawString(4.9*inch, y + 0.35*inch, signature_date_text)

    draw_footer(c, page_num)
    c.save()
    buf.seek(0)
    return buf.getvalue()

# ---------- Session defaults for prefill ----------
if "line_count" not in st.session_state:
    st.session_state.line_count = 5
if "prefill_items" not in st.session_state:
    st.session_state.prefill_items = []
if "prefill_customer_id" not in st.session_state:
    st.session_state.prefill_customer_id = None
if "project_name" not in st.session_state:
    st.session_state.project_name = ""
if "project_location" not in st.session_state:
    st.session_state.project_location = ""

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

# Customer preselect if converted from proposal
def_cust_id = st.session_state.prefill_customer_id
cust_index = 0
if def_cust_id:
    for i, cobj in enumerate(customers):
        if cobj["id"] == def_cust_id:
            cust_index = i
            break

cust = st.selectbox("Select Customer", customers, index=cust_index, format_func=lambda c: c["name"])

# ---------- Project fields ----------
project_name = st.text_input("Project Name", st.session_state.project_name, key="project_name")
project_location = st.text_input("Project Location (Address)", st.session_state.project_location, key="project_location")

# ---------- Line items (supports prefill) ----------
items = []
st.write("Line Items")
prefill = st.session_state.prefill_items or []
for i in range(st.session_state.line_count):
    default_desc = prefill[i]["Description"] if i < len(prefill) else ""
    default_qty = float(prefill[i]["Qty"]) if i < len(prefill) else 1.0
    default_unit = float(prefill[i]["Unit Price"]) if i < len(prefill) else 0.0

    c1, c2, c3, c4 = st.columns([5,1.5,2,2])
    desc = c1.text_input(f"Description {i+1}", default_desc, key=f"desc_{i}")
    qty = c2.number_input(f"Qty {i+1}", min_value=0.0, value=default_qty, step=1.0, key=f"qty_{i}")
    unit = c3.number_input(f"Unit Price {i+1}", min_value=0.0, value=default_unit, step=10.0, key=f"unit_{i}")
    c4.write(f"${qty*unit:,.2f}")
    if str(desc).strip():
        items.append({"Description": desc, "Qty": qty, "Unit Price": unit})
st.button("➕ Add Line Item", on_click=add_line)

subtotal = compute_subtotal(items)

# ---------- Signature Capture (optional, works great on iPad/Pencil) ----------
st.markdown("### Signature (optional)")
sig_cols = st.columns([3, 2])
with sig_cols[0]:
    use_signature = st.toggle("Capture on-screen signature", value=False)
with sig_cols[1]:
    signature_date = st.date_input("Signature Date", value=datetime.now().date())

signature_bytes = None
if use_signature:
    st.caption("Sign below:")
    canvas_result = st_canvas(
        fill_color="rgba(0, 0, 0, 0)",
        stroke_width=3,
        stroke_color="#000000",
        background_color="#FFFFFF",
        width=560,
        height=150,
        drawing_mode="freedraw",
        key="signature_canvas",
    )
    if canvas_result.image_data is not None:
        # Convert RGBA numpy array to PNG bytes
        import PIL.Image
        img = PIL.Image.fromarray(canvas_result.image_data.astype("uint8"))
        # Trim white border to get a tighter signature (optional)
        # Save to bytes
        out = io.BytesIO()
        img.save(out, format="PNG")
        signature_bytes = out.getvalue()

signature_date_text = signature_date.strftime("%m/%d/%Y") if signature_date else None

# ---------- Email Body ----------
def build_email_body(cust_name, is_proposal, ref_no):
    hour = datetime.now().hour
    greeting = "Good morning" if hour < 12 else "Good afternoon"
    body = f"""
    <p>{greeting} {cust_name},</p>
    <p>Attached is the {'proposal' if is_proposal else 'invoice'} ({ref_no}) that has been requested.
    Please take a moment at your earliest convenience to look it over and if you have any
    questions, comments, or concerns please contact me.</p>
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
    return body

# ---------- Proposal Mode ----------
if mode == "Proposal":
    pid = st.text_input("Proposal ID", "P-1001")
    proposal_notes = (
        "By signing, the signee agrees to pay the full balance upon project completion, "
        "acknowledges that additional work outside the scope will incur extra charges on the final invoice, "
        "and understands that all manufacturer details are outlined in the product owner’s manual."
    )
    st.text_area("Proposal Terms", proposal_notes, disabled=True)

    pdf_data = build_pdf(
        pid, cust["name"], project_name, project_location,
        items, subtotal, 0, subtotal, None,
        show_paid=False, notes=proposal_notes, is_proposal=True,
        signature_png_bytes=signature_bytes, signature_date_text=signature_date_text
    )

    c1, c2, c3 = st.columns(3)
    with c1:
        st.download_button("📄 Download Proposal PDF", pdf_data, file_name=f"Proposal_{pid}.pdf", mime="application/pdf")
    with c2:
        if st.button("👀 View Proposal PDF"):
            show_pdf_inline(pdf_data)
    with c3:
        if st.button("📧 Email Proposal"):
            msg = EmailMessage()
            msg["From"] = FROM_EMAIL
            msg["To"] = cust.get("email") or ""
            msg["Subject"] = f"Proposal {pid}"
            msg.add_alternative(build_email_body(cust["name"], True, pid), subtype="html")
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

    pdf_data = build_pdf(
        invoice_no, cust["name"], project_name, project_location,
        items, subtotal, deposit, grand_total, check_number,
        show_paid=show_paid, notes=None, is_proposal=False,
        signature_png_bytes=signature_bytes, signature_date_text=signature_date_text
    )

    c1, c2, c3 = st.columns(3)
    with c1:
        st.download_button("📄 Download Invoice PDF", pdf_data, file_name=f"Invoice_{invoice_no}.pdf", mime="application/pdf")
    with c2:
        if st.button("👀 View Invoice PDF"):
            show_pdf_inline(pdf_data)
    with c3:
        if st.button("📧 Email Invoice"):
            msg = EmailMessage()
            msg["From"] = FROM_EMAIL
            msg["To"] = cust.get("email") or ""
            msg["Subject"] = f"Invoice {invoice_no}"
            msg.add_alternative(build_email_body(cust["name"], False, invoice_no), subtype="html")
            msg.add_attachment(pdf_data, maintype="application", subtype="pdf", filename=f"Invoice_{invoice_no}.pdf")
            try:
                with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
                    server.login(FROM_EMAIL, APP_PASSWORD)
                    server.send_message(msg)
                st.success("Invoice emailed")
            except Exception as e:
                st.error(f"Email failed: {e}")

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

    # ---------- Active Proposals Dashboard ----------
    st.subheader("Active Proposals")
    with engine.begin() as conn:
        active_props = conn.execute(
            text("SELECT * FROM proposals WHERE status='open' ORDER BY created_at DESC")
        ).mappings().all()

    if not active_props:
        st.info("No active proposals.")
    else:
        for prop in active_props:
            st.markdown(f"**{prop['id']}** — {prop['project_name'] or ''} ({prop['project_location'] or ''})")
            col1, col2, col3 = st.columns([1,1,2])
            with col1:
                if st.button(f"Convert {prop['id']}", key=f"conv_{prop['id']}"):
                    # Prefill fields from the proposal, then mark converted and refresh
                    st.session_state.project_name = prop["project_name"] or ""
                    st.session_state.project_location = prop["project_location"] or ""
                    st.session_state.prefill_items = json.loads(prop["items_json"] or "[]")
                    st.session_state.prefill_customer_id = prop["customer_id"]
                    with engine.begin() as conn:
                        conn.execute(text("UPDATE proposals SET status='converted' WHERE id=:pid"), {"pid": prop["id"]})
                    st.success(f"Proposal {prop['id']} converted — fields above pre-filled.")
                    st.rerun()
            with col2:
                if st.button(f"Close {prop['id']}", key=f"close_{prop['id']}"):
                    with engine.begin() as conn:
                        conn.execute(text("UPDATE proposals SET status='closed' WHERE id=:pid"), {"pid": prop['id']})
                    st.warning(f"Proposal {prop['id']} closed.")
                    st.rerun()
            with col3:
                # Quick View PDF for proposal (no conversion)
                if st.button(f"View {prop['id']} PDF", key=f"view_{prop['id']}"):
                    prop_items = json.loads(prop["items_json"] or "[]")
                    # Load customer name for proposal quick view
                    cust_name = next((c["name"] for c in customers if c["id"] == prop["customer_id"]), "Customer")
                    pdf_quick = build_pdf(
                        prop["id"], cust_name, prop["project_name"], prop["project_location"],
                        prop_items, compute_subtotal(prop_items), 0, compute_subtotal(prop_items), None,
                        show_paid=False, notes=prop.get("notes"), is_proposal=True,
                        signature_png_bytes=None, signature_date_text=None
                    )
                    show_pdf_inline(pdf_quick, height=800)
# app.py (Invoicing App with Postgres)
# Full Streamlit code provided in chat previously
