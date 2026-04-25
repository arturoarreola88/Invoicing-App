# =========================
# J & I — Proposals & Invoices (Streamlit)
# =========================
import os, io, json, base64, textwrap, smtplib, pytz, re
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

# =========================
# Config + Branding
# =========================
# Using 'wide' layout helps on tablets to prevent text being cut off
st.set_page_config(page_title="J&I Proposals & Invoices", page_icon="🧾", layout="wide")
try:
    st.image("logo.png", width=220)
except:
    st.info("Tip: place a 'logo.png' next to app.py to show it in header + PDFs.")
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
# Database Init + Migration
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
              id TEXT PRIMARY KEY,
              number INTEGER,
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
              number INTEGER,
              customer_id TEXT NOT NULL REFERENCES customers(id),
              project_name TEXT,
              project_location TEXT,
              items_json TEXT DEFAULT '[]',
              total NUMERIC DEFAULT 0,
              deposit NUMERIC DEFAULT 0,
              check_number TEXT,
              paid BOOLEAN DEFAULT FALSE,
              internal_cost NUMERIC DEFAULT 0,
              created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """))

def migrate_db():
    with engine.begin() as conn:
        res = conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name='invoices' AND column_name='internal_cost'
        """)).fetchone()
        if not res:
            conn.execute(text("ALTER TABLE invoices ADD COLUMN internal_cost NUMERIC DEFAULT 0"))

init_db()
migrate_db()

# =========================
# Helpers
# =========================
CT = pytz.timezone("America/Chicago")
def now_ct(): return datetime.now(CT)

ss = st.session_state
ss.setdefault("line_count", 5)
ss.setdefault("p_nonce", 0)
ss.setdefault("i_nonce", 0)
ss.setdefault("project_name_value", "")
ss.setdefault("project_location_value", "")
ss.setdefault("prefill_items", [])
ss.setdefault("prefill_customer_id", None)
ss.setdefault("prefill_proposal_number", None)
ss.setdefault("prefill_proposal_id", None)

def add_line(): ss.line_count += 1

def reset_proposal_form():
    ss.project_name_value = ""
    ss.project_location_value = ""
    ss.prefill_items = []
    ss.prefill_customer_id = None
    ss.prefill_proposal_number = None
    ss.prefill_proposal_id = None
    ss.line_count = 5
    ss.p_nonce += 1
    st.rerun()

def reset_invoice_form():
    ss.project_name_value = ""
    ss.project_location_value = ""
    ss.prefill_items = []
    ss.prefill_customer_id = None
    ss.prefill_proposal_number = None
    ss.prefill_proposal_id = None
    ss.line_count = 5
    ss.i_nonce += 1
    st.rerun()

def compute_subtotal(items):
    return sum(float(r.get("Qty",0)) * float(r.get("Unit Price",0)) for r in items)

def _max_existing_number(conn):
    r1 = conn.execute(text("SELECT COALESCE(MAX(number),0) FROM proposals")).scalar() or 0
    r2 = conn.execute(text("SELECT COALESCE(MAX(number),0) FROM invoices")).scalar() or 0
    return max(r1, r2)

def format_prop_id(n): return f"P-{n:04d}"
def format_inv_id(n):  return f"INV-{n:04d}"
def format_inv_from_proposal(n): return f"INV-P-{n:04d}"

def parse_numeric_number(inv_no: str) -> int:
    m = re.search(r"(\d+)$", inv_no or "")
    if not m: raise ValueError("Could not parse numeric invoice number.")
    return int(m.group(1))

# FIX: Added unique ID to the URL to prevent the "Blank Page" bug on iPad/Chrome
def show_pdf_newtab(pdf_bytes: bytes, label: str="📄 Open PDF in New Tab"):
    b64 = base64.b64encode(pdf_bytes).decode("utf-8")
    cache_buster = now_ct().strftime("%H%M%S")
    st.markdown(
        f'<a href="data:application/pdf;base64,{b64}#toolbar=0&navpanes=0&{cache_buster}" target="_blank" style="text-decoration:none; background-color:#F0F2F6; padding:10px; border-radius:5px; color:black; font-weight:bold;">{label}</a>', 
        unsafe_allow_html=True
    )

# =========================
# Email Builder
# =========================
def build_email_body(cust_name, is_proposal, ref_no):
    first = (cust_name or "Customer").split()[0]
    hour = now_ct().hour
    greeting = "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"
    kind = "proposal" if is_proposal else "invoice"
    return f"""
    <p>{greeting} {first},</p>
    <p>Attached is the {kind} ({ref_no}) you requested. Please take a moment at your earliest convenience and look it over. If you have any questions, comments, or concerns, please don’t hesitate to contact me.</p>
    <p>Thank you for choosing J & I Heating and Cooling.</p>
    <hr>
    <p>Best regards,<br><b>Arturo Arreola</b><br>Owner<br>Direct: (630) 849-0385<br><a href="https://jihchr.com">Click here for our website</a></p>
    """

def send_email(pdf_bytes,to_email,subject,html_body,filename):
    if not to_email: raise RuntimeError("Missing recipient email.")
    msg=EmailMessage()
    msg["From"],msg["To"],msg["Subject"]=FROM_EMAIL,to_email,subject
    msg.add_alternative(html_body,subtype="html")
    msg.add_attachment(pdf_bytes,maintype="application",subtype="pdf",filename=filename)
    with smtplib.SMTP_SSL(SMTP_SERVER,SMTP_PORT) as server:
        server.login(FROM_EMAIL,APP_PASSWORD)
        server.send_message(msg)

# =========================
# PDF Builder
# =========================
def build_pdf(ref_no,cust_name,project_name,project_location,items,
              subtotal,deposit,grand_total,check_number,
              show_paid=False,notes=None,is_proposal=False,
              signature_png_bytes=None,signature_date_text=None):
    buf=io.BytesIO()
    c=canvas.Canvas(buf,pagesize=LETTER)
    w,h=LETTER

    try: c.drawImage(ImageReader("logo.png"),w-120,h-80,width=100,height=60,mask='auto')
    except: pass
    c.setFont("Helvetica-Bold",16); c.drawString(1*inch,h-1*inch,"J & I Heating and Cooling")
    c.setFont("Helvetica",10)
    c.drawString(1*inch,h-1.25*inch,"2788 N. 48th Rd."); c.drawString(1*inch,h-1.45*inch,"Sandwich IL, 60548")
    c.drawString(1*inch,h-1.65*inch,"Phone (630) 849-0385"); c.drawString(1*inch,h-1.85*inch,"Insured and Bonded")

    issue=now_ct().date()
    heading="Proposal" if is_proposal else "Invoice"
    terms = f"Valid until: {(issue+timedelta(days=15)).strftime('%m/%d/%Y')}" if is_proposal else f"Due Date: {issue.strftime('%m/%d/%Y')}"
    c.setFont("Helvetica",12)
    c.drawString(1*inch,h-2.3*inch,f"{heading} #: {ref_no}")
    c.drawString(1*inch,h-2.5*inch,f"Customer: {cust_name}")
    c.drawString(1*inch,h-2.7*inch,f"Project: {project_name or ''}")
    c.drawString(1*inch,h-2.9*inch,f"Location: {project_location or ''}")
    c.setFont("Helvetica",10)
    c.drawString(w-2.5*inch,h-2.3*inch,f"Date: {issue.strftime('%m/%d/%Y')}")
    c.drawString(w-2.5*inch,h-2.6*inch,terms)

    if show_paid and not is_proposal:
        c.setFont("Helvetica-Bold",36); c.setFillColorRGB(1,0,0)
        c.drawString(w/2,h-3.0*inch,"PAID"); c.setFont("Helvetica",12)
        c.drawString(w/2,h-3.4*inch,now_ct().strftime("%m/%d/%Y")); c.setFillColorRGB(0,0,0)

    y=h-3.6*inch; c.setFont("Helvetica-Bold",10)
    c.drawString(1*inch,y,"Description"); c.drawString(4.4*inch,y,"Qty"); c.drawString(5.4*inch,y,"Unit"); c.drawString(6.4*inch,y,"Line Total"); y-=16
    c.setFont("Helvetica",10)

    for row in items:
        desc=str(row.get("Description","")); wrapped=textwrap.wrap(desc,width=50)
        for j,line in enumerate(wrapped):
            c.drawString(1*inch,y,line)
            if j==0:
                qty, unit = float(row.get("Qty",0)), float(row.get("Unit Price",0))
                c.drawString(4.4*inch,y,f"{qty:.2f}"); c.drawString(5.4*inch,y,f"${unit:,.2f}"); c.drawString(6.4*inch,y,f"${qty*unit:,.2f}")
            y-=18
            if y < 1*inch: c.showPage(); y=h-1*inch

    y-=10; c.setFont("Helvetica-Bold",11)
    c.drawString(5*inch,y,"Subtotal:"); c.drawString(6.4*inch,y,f"${subtotal:,.2f}"); y-=18
    if not is_proposal and deposit > 0:
        c.drawString(5*inch,y,"Deposit:"); c.drawString(6.4*inch,y,f"-${float(deposit):,.2f}"); y-=18
    c.drawString(5*inch,y,"Grand Total:"); c.drawString(6.4*inch,y,f"${grand_total:,.2f}")

    if notes:
        y-=25; c.setFont("Helvetica-Oblique",9)
        for ln in textwrap.wrap(notes,width=90): c.drawString(1*inch,y,ln); y-=14

    if signature_png_bytes:
        y-=50; sig=ImageReader(io.BytesIO(signature_png_bytes))
        c.drawImage(sig,1*inch,y,width=150,height=40,mask='auto')
        if signature_date_text: c.setFont("Helvetica",10); c.drawString(4.5*inch,y+15,f"Signed: {signature_date_text}")
    elif not is_proposal:
        c.setFont("Helvetica",10); y-=40
        c.drawString(1*inch,y,"X ____________________"); c.drawString(4*inch,y,"Date: ______________")

    c.save(); buf.seek(0); return buf.getvalue()

# =========================
# Main Navigation
# =========================
prop_tab, inv_tab = st.tabs(["Proposal", "Invoice"])

# =========================
# PROPOSAL TAB
# =========================
with prop_tab:
    st.subheader("Create Proposal")
    focus_mode_p = st.toggle("🔍 Focus Mode (Bigger text boxes for tablet)", key="p_focus")
    
    mode = st.radio("Choose Option", ["Select Customer", "➕ New Customer"], key="p_cust_mode", horizontal=True)
    cust = {"id": None, "name": ""}
    
    if mode == "Select Customer":
        with engine.begin() as conn:
            customers = conn.execute(text("SELECT * FROM customers ORDER BY name")).mappings().all()
        cust = st.selectbox("Customer", [{"id": None, "name": "-- Select --"}] + customers, format_func=lambda c: c["name"], key="p_cust_sel")
    else:
        n_name = st.text_input("Name", key="p_new_name")
        n_email = st.text_input("Email", key="p_new_email")
        if st.button("Save New Customer"):
            with engine.begin() as conn:
                conn.execute(text("INSERT INTO customers(id,name,email) VALUES(:id,:n,:e)"), dict(id=n_email or n_name, n=n_name, e=n_email))
            st.success("Customer added!")

    p_nonce = ss.p_nonce
    p_name = st.text_input("Project Name", ss.project_name_value, key=f"p_proj_{p_nonce}")
    p_loc = st.text_input("Location", ss.project_location_value, key=f"p_loc_{p_nonce}")

    st.markdown("**Line Items**")
    items = []
    for i in range(ss.line_count):
        c1,c2,c3,c4 = st.columns([5,1.5,2,2])
        if focus_mode_p:
            d = c1.text_area(f"Description {i+1}", key=f"p_d_{p_nonce}_{i}", height=100)
        else:
            d = c1.text_input(f"Description {i+1}", key=f"p_d_{p_nonce}_{i}")
        q = c2.number_input(f"Qty {i+1}", min_value=0.0, value=1.0, key=f"p_q_{p_nonce}_{i}")
        u = c3.number_input(f"Price {i+1}", min_value=0.0, value=0.0, key=f"p_u_{p_nonce}_{i}")
        c4.write(f"\n\n${q*u:,.2f}")
        if d: items.append({"Description": d, "Qty": q, "Unit Price": u})
    
    st.button("➕ Add Line", on_click=add_line, key=f"p_add_{p_nonce}")
    sub = compute_subtotal(items)
    def_notes = "By signing, you agree to 50% deposit..."
    notes = st.text_area("Notes", def_notes, key=f"p_notes_{p_nonce}")

    p_sig = None
    if st.toggle("Add Signature"):
        canvas_p = st_canvas(stroke_width=2, stroke_color="black", background_color="white", width=400, height=120, key=f"p_sig_can_{p_nonce}")
        if canvas_p.image_data is not None:
            img = Image.fromarray((canvas_p.image_data[:,:,:3]*255).astype("uint8"))
            b = io.BytesIO(); img.save(b, format="PNG"); p_sig = b.getvalue()

    with engine.begin() as conn: next_n = _max_existing_number(conn) + 1
    pdf_p = build_pdf(format_prop_id(next_n), cust["name"], p_name, p_loc, items, sub, 0, sub, "", False, notes, True, p_sig, now_ct().strftime("%m/%d/%Y"))

    colA, colB, colC, colD = st.columns(4)
    with colA: st.download_button("📄 Download", pdf_p, f"Prop_{next_n}.pdf")
    with colB: 
        if st.button("👀 View PDF"): show_pdf_newtab(pdf_p)
    with colC:
        if st.button("📧 Email"):
            send_email(pdf_p, cust["email"], f"Proposal {next_n}", build_email_body(cust["name"], True, next_n), f"Prop_{next_n}.pdf")
            st.success("Email sent!")
    with colD:
        if st.button("💾 Save"):
            with engine.begin() as conn:
                conn.execute(text("INSERT INTO proposals (id,number,customer_id,project_name,project_location,items_json,notes) VALUES (:id,:n,:cid,:pn,:pl,:ij,:nt)"),
                             dict(id=format_prop_id(next_n), n=next_n, cid=cust["id"], pn=p_name, pl=p_loc, ij=json.dumps(items), nt=notes))
            st.success("Saved!"); reset_proposal_form()

    # Dashboard: Active Proposals
    st.markdown("---"); st.subheader("📋 Active Proposals")
    with engine.begin() as conn:
        props = conn.execute(text("SELECT * FROM proposals WHERE status='open' ORDER BY created_at DESC")).mappings().all()
        cust_map = {c["id"]: c["name"] for c in conn.execute(text("SELECT id,name FROM customers")).mappings().all()}
    
    for prop in props:
        with st.expander(f"{prop['id']} — {cust_map.get(prop['customer_id'])}"):
            c1,c2,c3 = st.columns(3)
            if c1.button("Convert to Invoice", key=f"conv_{prop['id']}"):
                with engine.begin() as conn:
                    conn.execute(text("INSERT INTO invoices (invoice_no,number,customer_id,items_json) VALUES (:inv,:n,:cid,:ij)"),
                                 dict(inv=format_inv_from_proposal(prop["number"]), n=prop["number"], cid=prop["customer_id"], ij=prop["items_json"]))
                    conn.execute(text("UPDATE proposals SET status='converted' WHERE id=:id"), {"id": prop["id"]})
                st.rerun()
            if c2.button("View PDF", key=f"v_old_{prop['id']}"):
                p_items = json.loads(prop["items_json"]); p_sub = compute_subtotal(p_items)
                old_pdf = build_pdf(prop["id"], cust_map.get(prop["customer_id"]), prop["project_name"], prop["project_location"], p_items, p_sub, 0, p_sub, "", False, prop["notes"], True)
                show_pdf_newtab(old_pdf)

# =========================
# INVOICE TAB
# =========================
with inv_tab:
    st.subheader("Invoice Maker")
    focus_mode_i = st.toggle("🔍 Focus Mode (Bigger text boxes for tablet)", key="i_focus")
    
    with engine.begin() as conn:
        inv_customers = conn.execute(text("SELECT * FROM customers ORDER BY name")).mappings().all()
    
    # Logic for prefilling when "Load into Invoice Maker" is clicked
    sel_idx = 0
    if ss.prefill_customer_id:
        for idx, opt in enumerate(inv_customers):
            if opt["id"] == ss.prefill_customer_id:
                sel_idx = idx + 1
                break

    i_cust = st.selectbox("Customer", [{"id": None, "name": "-- Select --"}] + inv_customers, index=sel_idx, format_func=lambda c: c["name"], key="i_cust_sel")
    
    i_nonce = ss.i_nonce
    with engine.begin() as conn: max_n = _max_existing_number(conn)
    i_no = st.text_input("Invoice #", format_inv_id(max_n+1), key=f"i_no_{i_nonce}")

    i_items = []
    prefill = ss.prefill_items or []
    needed = max(len(prefill), ss.line_count)
    for i in range(needed):
        d_val = prefill[i]["Description"] if i < len(prefill) else ""
        q_val = float(prefill[i]["Qty"]) if i < len(prefill) else 1.0
        u_val = float(prefill[i]["Unit Price"]) if i < len(prefill) else 0.0
        
        c1,c2,c3,c4 = st.columns([5,1.5,2,2])
        if focus_mode_i:
            d = c1.text_area(f"Description {i+1}", d_val, key=f"i_d_{i_nonce}_{i}", height=100)
        else:
            d = c1.text_input(f"Description {i+1}", d_val, key=f"i_d_{i_nonce}_{i}")
        q = c2.number_input(f"Qty {i+1}", value=q_val, key=f"i_q_{i_nonce}_{i}")
        u = c3.number_input(f"Price {i+1}", value=u_val, key=f"i_u_{i_nonce}_{i}")
        c4.write(f"\n\n${q*u:,.2f}")
        if d: i_items.append({"Description": d, "Qty": q, "Unit Price": u})

    dep = st.number_input("Deposit Paid", min_value=0.0, value=0.0)
    paid = st.toggle("Mark PAID Stamp")
    sub_i = compute_subtotal(i_items)
    grand = max(0.0, sub_i - dep)

    pdf_i = build_pdf(i_no, i_cust["name"], "", "", i_items, sub_i, dep, grand, "", paid, "Thank you!", False)

    col1, col2, col3 = st.columns(3)
    with col1: 
        if st.button("💾 Save Invoice"):
            with engine.begin() as conn:
                num = parse_numeric_number(i_no)
                conn.execute(text("INSERT INTO invoices (invoice_no,number,customer_id,items_json,total,deposit,paid) VALUES (:inv,:n,:cid,:ij,:t,:d,:p)"),
                             dict(inv=i_no, n=num, cid=i_cust["id"], ij=json.dumps(i_items), t=grand, d=dep, p=paid))
            st.success("Invoice Saved!")
    with col2:
        if st.button("👀 View Invoice PDF"): show_pdf_newtab(pdf_i)
    with col3:
        if st.button("📧 Email Invoice"):
            send_email(pdf_i, i_cust["email"], f"Invoice {i_no}", build_email_body(i_cust["name"], False, i_no), f"Inv_{i_no}.pdf")
            st.success("Emailed!")

    # Dashboard: Recent Invoices
    st.markdown("---"); st.subheader("🧾 Recent Invoices")
    with engine.begin() as conn:
        recent_invs = conn.execute(text("SELECT * FROM invoices ORDER BY created_at DESC LIMIT 10")).mappings().all()
    for inv in recent_invs:
        with st.expander(f"{inv['invoice_no']} — ${float(inv['total']):,.2f}"):
            cost = st.number_input("Internal Cost", value=float(inv["internal_cost"] or 0), key=f"cost_{inv['invoice_no']}")
            if st.button("Save Cost", key=f"sc_{inv['invoice_no']}"):
                with engine.begin() as conn:
                    conn.execute(text("UPDATE invoices SET internal_cost=:c WHERE invoice_no=:id"), {"c": cost, "id": inv["invoice_no"]})
                st.success("Cost updated!")

    # Converted Proposals Dashboard
    st.markdown("---"); st.subheader("📑 Converted Proposals")
    with engine.begin() as conn:
        conv_props = conn.execute(text("SELECT * FROM proposals WHERE status='converted' ORDER BY created_at DESC")).mappings().all()
    for cp in conv_props:
        with st.expander(f"{cp['id']} - Ready to Finalize"):
            if st.button("Load into Invoice Maker", key=f"ld_{cp['id']}"):
                ss.prefill_customer_id = cp["customer_id"]
                ss.prefill_items = json.loads(cp["items_json"])
                ss.project_name_value = cp["project_name"]
                ss.project_location_value = cp["project_location"]
                ss.i_nonce += 1
                st.rerun()

    # YTD Summary
    st.markdown("---")
    with st.expander("📊 Year-to-Date Summary"):
        year = now_ct().year
        with engine.begin() as conn:
            stats = conn.execute(text("SELECT SUM(total) as revenue, SUM(internal_cost) as costs FROM invoices WHERE EXTRACT(YEAR FROM created_at) = :y"), {"y": year}).mappings().first()
        rev = float(stats["revenue"] or 0)
        cst = float(stats["costs"] or 0)
        st.write(f"**{year} Revenue:** ${rev:,.2f}")
        st.write(f"**{year} Costs:** ${cst:,.2f}")
        st.write(f"**{year} Profit:** ${rev-cst:,.2f}")
