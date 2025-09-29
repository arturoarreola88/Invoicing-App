# =========================
# J & I — Proposals & Invoices (Streamlit)
# =========================
import os, io, json, base64, textwrap, smtplib, pytz
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
st.set_page_config(page_title="J&I Proposals & Invoices", page_icon="🧾", layout="centered")
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
init_db()

def migrate_db():
    with engine.begin() as conn:
        # ensure internal_cost column exists
        res = conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name='invoices' AND column_name='internal_cost'
        """)).fetchone()
        if not res:
            conn.execute(text("ALTER TABLE invoices ADD COLUMN internal_cost NUMERIC DEFAULT 0"))
migrate_db()

# =========================
# Helpers
# =========================
CT = pytz.timezone("America/Chicago")
def now_ct(): return datetime.now(CT)

ss = st.session_state
# shared state
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

def show_pdf_newtab(pdf_bytes: bytes, label: str="📄 Open PDF in New Tab"):
    b64 = base64.b64encode(pdf_bytes).decode("utf-8")
    st.markdown(f'<a href="data:application/pdf;base64,{b64}" target="_blank">{label}</a>', unsafe_allow_html=True)

# =========================
# Email
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
    <p>
      Best regards,<br>
      <b>Arturo Arreola</b><br>
      Owner<br>
      Direct: (630) 849-0385<br>
      <a href="https://jihchr.com">Click here for our website</a>
    </p>
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

    # header
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

    # table
    y=h-3.6*inch; c.setFont("Helvetica-Bold",10)
    c.drawString(1*inch,y,"Description"); c.drawString(4.4*inch,y,"Qty")
    c.drawString(5.4*inch,y,"Unit"); c.drawString(6.4*inch,y,"Line Total"); y-=16
    c.setFont("Helvetica",10)

    for row in items:
        desc=str(row.get("Description","")); wrapped=textwrap.wrap(desc,width=50)
        for j,line in enumerate(wrapped):
            c.drawString(1*inch,y,line)
            if j==0:
                qty=float(row.get("Qty",0)); unit=float(row.get("Unit Price",0))
                c.drawString(4.4*inch,y,f"{qty:.2f}")
                c.drawString(5.4*inch,y,f"${unit:,.2f}")
                c.drawString(6.4*inch,y,f"${qty*unit:,.2f}")
            y-=18

    y-=10; c.setFont("Helvetica-Bold",11)
    if is_proposal:
        c.drawString(5*inch,y,"Subtotal:"); c.drawString(6.4*inch,y,f"${subtotal:,.2f}"); y-=18
        c.drawString(5*inch,y,"Grand Total:"); c.drawString(6.4*inch,y,f"${subtotal:,.2f}")
    else:
        c.drawString(5*inch,y,"Subtotal:"); c.drawString(6.4*inch,y,f"${subtotal:,.2f}"); y-=18
        if deposit and float(deposit)>0:
            c.drawString(5*inch,y,"Deposit:"); c.drawString(6.4*inch,y,f"-${float(deposit):,.2f}"); y-=18
        c.drawString(5*inch,y,"Grand Total:"); c.drawString(6.4*inch,y,f"${grand_total:,.2f}")
        if check_number: y-=18; c.setFont("Helvetica",10); c.drawString(1*inch,y,f"Check #: {check_number}")

    if notes:
        y-=25; c.setFont("Helvetica-Oblique",9)
        for ln in textwrap.wrap(notes,width=90): c.drawString(1*inch,y,ln); y-=14

    y-=40
    if signature_png_bytes:
        sig=ImageReader(io.BytesIO(signature_png_bytes))
        c.drawImage(sig,1*inch,y,width=150,height=40,mask='auto')
        if signature_date_text: c.setFont("Helvetica",10); c.drawString(4.5*inch,y+15,f"Signed: {signature_date_text}")
    else:
        c.setFont("Helvetica",10); c.drawString(1*inch,y,"X ____________________"); c.drawString(4*inch,y,"Date: ______________")

    c.save(); buf.seek(0); return buf.getvalue()

# =========================
# Tabs
# =========================
prop_tab,inv_tab=st.tabs(["Proposal","Invoice"])

# ====================================================
# PROPOSAL TAB
# ====================================================
with prop_tab:
    st.subheader("Create Proposal")

    # Customer choose/add
    mode = st.radio("Choose Option", ["Select Existing Customer", "➕ Add New Customer"], key=f"proposal_cust_mode")
    cust = {"id": None, "name": ""}
    if mode == "Select Existing Customer":
        with engine.begin() as conn:
            customers = conn.execute(text("SELECT * FROM customers ORDER BY name")).mappings().all()
        cust_options = [{"id": None, "name": "-- Select Customer --"}] + customers
        cust = st.selectbox("Customer", cust_options, index=0,
                            format_func=lambda c: c["name"], key=f"proposal_cust_select")
        if not cust["id"]:
            st.warning("Please select a customer before saving.")
    else:
        new_name = st.text_input("Full Name *", key=f"proposal_new_name")
        new_email = st.text_input("Email", key=f"proposal_new_email")
        new_phone = st.text_input("Phone", key=f"proposal_new_phone")
        new_addr = st.text_input("Street Address", key=f"proposal_new_addr")
        new_csz = st.text_input("City, State, Zip", key=f"proposal_new_csz")
        if st.button("💾 Save New Customer (Proposal)", key=f"proposal_save_customer"):
            if not new_name.strip():
                st.error("Name is required.")
            else:
                with engine.begin() as conn:
                    conn.execute(text("""
                        INSERT INTO customers(id,name,email,phone,address,city_state_zip)
                        VALUES(:id,:name,:email,:phone,:addr,:csz)
                        ON CONFLICT(id) DO UPDATE
                        SET name=:name, email=:email, phone=:phone, address=:addr, city_state_zip=:csz
                    """), dict(id=new_email or new_phone or new_name,
                               name=new_name, email=new_email, phone=new_phone,
                               addr=new_addr, csz=new_csz))
                st.success("✅ New customer added for proposals.")
                cust = {"id": new_email or new_phone or new_name, "name": new_name}

    # Project info
    p_nonce = ss.p_nonce
    project_name = st.text_input("Project Name", ss.project_name_value, key=f"p_project_name_{p_nonce}")
    project_location = st.text_input("Project Location (Address)", ss.project_location_value, key=f"p_project_location_{p_nonce}")

    # Line items
    st.markdown("**Line Items**")
    items = []
    for i in range(ss.line_count):
        c1,c2,c3,c4 = st.columns([5,1.5,2,2])
        desc = c1.text_input(f"Description {i+1}", "", key=f"p_desc_{p_nonce}_{i}")
        qty  = c2.number_input(f"Qty {i+1}", min_value=0.0, value=1.0, step=1.0, key=f"p_qty_{p_nonce}_{i}")
        unit = c3.number_input(f"Unit Price {i+1}", min_value=0.0, value=0.0, step=10.0, key=f"p_unit_{p_nonce}_{i}")
        c4.write(f"${qty*unit:,.2f}")
        if desc.strip():
            items.append({"Description": desc, "Qty": qty, "Unit Price": unit})
    st.button("➕ Add Line Item", on_click=add_line, key=f"p_add_btn_{p_nonce}")

    subtotal = compute_subtotal(items)
    default_notes = (
        "By signing, the signee agrees to pay the full balance upon project completion, "
        "acknowledges that additional work outside the scope will incur extra charges on the final invoice, "
        "and understands that all manufacturer details are outlined in the product owner’s manual."
    )
    notes = st.text_area("Notes", default_notes, height=100, key=f"p_notes_{p_nonce}")

    with engine.begin() as conn:
        next_n = _max_existing_number(conn) + 1

    # Signature toggle (optional)
    st.subheader("Signature (optional)")
    proposal_sig_bytes = None
    if st.toggle("Add In-Person Signature to Proposal", key=f"p_sig_toggle_{p_nonce}"):
        canvas_result = st_canvas(
            fill_color="rgba(255,255,255,0)",
            stroke_width=2, stroke_color="black",
            background_color="white", width=400, height=120,
            drawing_mode="freedraw", key=f"p_sig_canvas_{p_nonce}",
            display_toolbar=True
        )
        if canvas_result.image_data is not None:
            arr=(canvas_result.image_data[:,:,:3]*255).astype("uint8")
            sig_img=Image.fromarray(arr); buf=io.BytesIO(); sig_img.save(buf,format="PNG")
            proposal_sig_bytes=buf.getvalue()

    pdf_prop = build_pdf(
        ref_no=format_prop_id(next_n), cust_name=cust["name"] if cust and cust.get("id") else "",
        project_name=project_name, project_location=project_location,
        items=items, subtotal=subtotal, deposit=0, grand_total=subtotal, check_number=None,
        show_paid=False, notes=notes, is_proposal=True,
        signature_png_bytes=proposal_sig_bytes,
        signature_date_text=now_ct().strftime("%m/%d/%Y") if proposal_sig_bytes else None
    )

    cA,cB,cC,cD = st.columns(4)
    with cA: st.download_button("📄 Download Proposal", data=pdf_prop, file_name=f"Proposal_{format_prop_id(next_n)}.pdf")
    with cB:
        if st.button("👀 View Proposal PDF"): show_pdf_newtab(pdf_prop,"📄 Open Proposal PDF")
    with cC:
        if st.button("📧 Email Proposal") and cust.get("id"):
            try:
                with engine.begin() as conn:
                    row=conn.execute(text("SELECT email FROM customers WHERE id=:id"),{"id":cust["id"]}).mappings().first()
                to_addr=(row["email"] if row and row.get("email") else None) or cust.get("email")
                send_email(pdf_prop,to_addr,f"Proposal {format_prop_id(next_n)}",
                           build_email_body(cust["name"],True,format_prop_id(next_n)),
                           f"Proposal_{format_prop_id(next_n)}.pdf")
                st.success("Proposal emailed.")
            except Exception as e: st.error(f"Email failed: {e}")
    with cD:
        if st.button("💾 Save Proposal") and cust.get("id"):
            with engine.begin() as conn:
                n=_max_existing_number(conn)+1; pid=format_prop_id(n)
                conn.execute(text("""
                    INSERT INTO proposals (id, number, customer_id, project_name, project_location, items_json, notes, status)
                    VALUES (:id,:num,:cid,:pname,:ploc,:items,:notes,'open')
                """),dict(id=pid,num=n,cid=cust["id"],pname=project_name,ploc=project_location,
                          items=json.dumps(items),notes=notes))
            st.success(f"✅ Proposal {pid} saved."); reset_proposal_form()

    if st.button("♻ Reset Proposal Form"): reset_proposal_form()

    # Dashboard: Active Proposals
    st.markdown("---"); st.subheader("📋 Active Proposals")
    with engine.begin() as conn:
        props=conn.execute(text("SELECT * FROM proposals WHERE status='open' ORDER BY created_at DESC")).mappings().all()
    if not props: st.info("No open proposals.")
    else:
        with engine.begin() as conn:
            cust_map={c["id"]:c["name"] for c in conn.execute(text("SELECT id,name FROM customers")).mappings().all()}
        for prop in props:
            with st.expander(f"{prop['id']} — {prop.get('project_name') or ''}"):
                st.write(f"Customer: {cust_map.get(prop['customer_id'], prop['customer_id'])}")
                st.write(f"Location: {prop.get('project_location') or ''}")
                c1,c2,c3=st.columns(3)
                if c1.button("Convert to Invoice", key=f"conv_{prop['id']}"):
                    with engine.begin() as conn:
                        exists=conn.execute(text("SELECT 1 FROM invoices WHERE number=:n"),{"n":prop["number"]}).fetchone()
                        if not exists:
                            inv_no=format_inv_id(prop["number"])
                            conn.execute(text("""
                                INSERT INTO invoices (invoice_no,number,customer_id,project_name,project_location,
                                                      items_json,total,deposit,check_number,paid)
                                VALUES (:inv,:num,:cid,:pname,:ploc,:items,0,0,NULL,FALSE)
                            """),dict(inv=inv_no,num=prop["number"],cid=prop["customer_id"],
                                      pname=prop.get("project_name"),ploc=prop.get("project_location"),
                                      items=prop["items_json"]))
                        conn.execute(text("UPDATE proposals SET status='converted' WHERE id=:id"),{"id":prop["id"]})
                    st.success(f"Converted {prop['id']} → {format_inv_id(prop['number'])}."); st.rerun()
                if c2.button("Close Proposal", key=f"close_{prop['id']}"):
                    with engine.begin() as conn: conn.execute(text("UPDATE proposals SET status='closed' WHERE id=:id"),{"id":prop["id"]})
                    st.warning(f"Proposal {prop['id']} closed."); st.rerun()
                if c3.button("View PDF", key=f"view_{prop['id']}"):
                    prop_items=json.loads(prop["items_json"] or "[]"); prop_subtotal=compute_subtotal(prop_items)
                    prop_pdf=build_pdf(ref_no=prop["id"],cust_name=cust_map.get(prop["customer_id"],prop["customer_id"]),
                        project_name=prop.get("project_name"),project_location=prop.get("project_location"),
                        items=prop_items,subtotal=prop_subtotal,deposit=0,grand_total=prop_subtotal,
                        check_number=None,is_proposal=True,notes=prop.get("notes"))
                    show_pdf_newtab(prop_pdf,"📄 Open Proposal PDF")

# ====================================================
# INVOICE TAB
# ====================================================
with inv_tab:
    st.subheader("Create / Manage Invoice")

    # Customer choose/add
    mode = st.radio("Choose Option", ["Select Existing Customer", "➕ Add New Customer"], key=f"invoice_cust_mode")
    cust={"id":None,"name":""}
    if mode=="Select Existing Customer":
        with engine.begin() as conn: customers=conn.execute(text("SELECT * FROM customers ORDER BY name")).mappings().all()
        cust_options=[{"id":None,"name":"-- Select Customer --"}]+customers
        cust=st.selectbox("Customer",cust_options,index=0,format_func=lambda c:c["name"],key=f"invoice_cust_select")
        if not cust["id"]:
            st.warning("Please select a customer before saving.")
    else:
        new_name=st.text_input("Full Name *",key=f"invoice_new_name")
        new_email=st.text_input("Email",key=f"invoice_new_email")
        new_phone=st.text_input("Phone",key=f"invoice_new_phone")
        new_addr=st.text_input("Street Address",key=f"invoice_new_addr")
        new_csz=st.text_input("City, State, Zip",key=f"invoice_new_csz")
        if st.button("💾 Save New Customer (Invoice)",key=f"invoice_save_customer"):
            if not new_name.strip(): st.error("Name is required.")
            else:
                with engine.begin() as conn:
                    conn.execute(text("""
                        INSERT INTO customers(id,name,email,phone,address,city_state_zip)
                        VALUES(:id,:name,:email,:phone,:addr,:csz)
                        ON CONFLICT(id) DO UPDATE
                        SET name=:name,email=:email,phone=:phone,address=:addr,city_state_zip=:csz
                    """),dict(id=new_email or new_phone or new_name,
                              name=new_name,email=new_email,phone=new_phone,
                              addr=new_addr,csz=new_csz))
                st.success("✅ New customer added for invoices."); cust={"id":new_email or new_phone or new_name,"name":new_name}

    # Invoice number defaults
    with engine.begin() as conn: max_all=_max_existing_number(conn)
    default_num=ss.prefill_proposal_number
    inv_no_default=format_inv_id(default_num) if default_num else format_inv_id(max_all+1)
    i_nonce=ss.i_nonce
    inv_no=st.text_input("Invoice #",inv_no_default,key=f"i_inv_no_{i_nonce}")

    # Project info
    project_name=st.text_input("Project Name",ss.project_name_value,key=f"i_project_name_{i_nonce}")
    project_location=st.text_input("Project Location (Address)",ss.project_location_value,key=f"i_project_location_{i_nonce}")

    # Line items
    st.markdown("**Line Items**"); items=[]; prefill=ss.prefill_items or []
    needed=max(len(prefill),ss.line_count)
    for i in range(needed):
        d=(prefill[i]["Description"] if i<len(prefill) else ""); q=(float(prefill[i]["Qty"]) if i<len(prefill) else 1.0)
        u=(float(prefill[i]["Unit Price"]) if i<len(prefill) else 0.0)
        c1,c2,c3,c4=st.columns([5,1.5,2,2])
        desc=c1.text_input(f"Description {i+1}",d,key=f"i_desc_{i_nonce}_{i}")
        qty=c2.number_input(f"Qty {i+1}",min_value=0.0,value=q,step=1.0,key=f"i_qty_{i_nonce}_{i}")
        unit=c3.number_input(f"Unit Price {i+1}",min_value=0.0,value=u,step=10.0,key=f"i_unit_{i_nonce}_{i}")
        c4.write(f"${qty*unit:,.2f}")
        if str(desc).strip(): items.append({"Description":desc,"Qty":qty,"Unit Price":unit})
    st.button("➕ Add Line Item",on_click=add_line,key=f"i_add_btn_{i_nonce}")

    # Totals/payments
    subtotal=compute_subtotal(items)
    deposit=st.number_input("Deposit Amount",min_value=0.0,value=0.0,step=50.0,key=f"i_deposit_{i_nonce}")
    chk_no=st.text_input("Check Number (if paying by check)","",key=f"i_checknum_{i_nonce}")
    show_paid=st.toggle("Show PAID Stamp",value=False,key=f"i_paid_toggle_{i_nonce}")
    grand_total=max(0.0,subtotal-deposit); invoice_notes="Thank you for your business!"

    # Signature toggle (optional)
    st.subheader("Signature (optional)")
    invoice_sig_bytes=None
    if st.toggle("Add In-Person Signature to Invoice",key=f"i_sig_toggle_{i_nonce}"):
        canvas_result=st_canvas(fill_color="rgba(255,255,255,0)",stroke_width=2,stroke_color="black",
            background_color="white",width=400,height=120,drawing_mode="freedraw",
            key=f"i_sig_canvas_{i_nonce}",display_toolbar=True)
        if canvas_result.image_data is not None:
            arr=(canvas_result.image_data[:,:,:3]*255).astype("uint8")
            sig_img=Image.fromarray(arr); buf=io.BytesIO(); sig_img.save(buf,format="PNG")
            invoice_sig_bytes=buf.getvalue()

    pdf_inv=build_pdf(ref_no=inv_no,cust_name=cust["name"] if cust and cust.get("id") else "",
        project_name=project_name,project_location=project_location,
        items=items,subtotal=subtotal,deposit=deposit,grand_total=grand_total,check_number=chk_no,
        show_paid=show_paid,notes=invoice_notes,is_proposal=False,
        signature_png_bytes=invoice_sig_bytes,
        signature_date_text=now_ct().strftime("%m/%d/%Y") if invoice_sig_bytes else None)

    cA,cB,cC,cD=st.columns(4)
    with cA: st.download_button("📄 Download Invoice",data=pdf_inv,file_name=f"Invoice_{inv_no}.pdf")
    with cB:
        if st.button("👀 View Invoice PDF"): show_pdf_newtab(pdf_inv,"📄 Open Invoice PDF")
    with cC:
        if st.button("📧 Email Invoice") and cust.get("id"):
            try:
                with engine.begin() as conn: row=conn.execute(text("SELECT email,name FROM customers WHERE id=:id"),{"id":cust["id"]}).mappings().first()
                to_addr=(row["email"] if row and row.get("email") else None) or cust.get("email")
                name_for_email=(row["name"] if row and row.get("name") else cust.get("name") or "")
                send_email(pdf_inv,to_addr,f"Invoice {inv_no}",build_email_body(name_for_email,False,inv_no),f"Invoice_{inv_no}.pdf")
                st.success("Invoice emailed.")
            except Exception as e: st.error(f"Email failed: {e}")
    with cD:
        if st.button("💾 Save Invoice") and cust.get("id"):
            try: number_part=int(inv_no.split("-")[-1])
            except Exception: st.error("Invoice # must look like INV-1001"); st.stop()
            with engine.begin() as conn:
                existing=conn.execute(text("SELECT invoice_no FROM invoices WHERE number=:n"),{"n":number_part}).fetchone()
                if existing:
                    conn.execute(text("""
                        UPDATE invoices
                        SET customer_id=:cid, project_name=:pname, project_location=:ploc,
                            items_json=:items, total=:total, deposit=:dep, check_number=:chk, paid=:paid
                        WHERE number=:n
                    """),dict(cid=cust["id"],pname=project_name,ploc=project_location,
                              items=json.dumps(items),total=grand_total,dep=deposit,chk=chk_no,paid=show_paid,n=number_part))
                else:
                    conn.execute(text("""
                        INSERT INTO invoices (invoice_no,number,customer_id,project_name,project_location,
                                              items_json,total,deposit,check_number,paid)
                        VALUES (:inv,:n,:cid,:pname,:ploc,:items,:total,:dep,:chk,:paid)
                    """),dict(inv=inv_no,n=number_part,cid=cust["id"],pname=project_name,ploc=project_location,
                              items=json.dumps(items),total=grand_total,dep=deposit,chk=chk_no,paid=show_paid))
            st.success(f"✅ Invoice {inv_no} saved.")

    if st.button("♻ Reset Invoice Form"): reset_invoice_form()

    # Dashboard: Recent Invoices
    st.markdown("---"); st.subheader("🧾 Recent Invoices")
    with engine.begin() as conn:
        invs=conn.execute(text("""
            SELECT invoice_no,customer_id,project_name,project_location,items_json,total,deposit,check_number,paid,internal_cost,created_at
            FROM invoices ORDER BY created_at DESC LIMIT 20
        """)).mappings().all()
        cust_map={c["id"]:c["name"] for c in conn.execute(text("SELECT id,name FROM customers")).mappings().all()}
    if not invs: st.info("No invoices yet.")
    else:
        for inv in invs:
            tot=float(inv["total"] or 0); title=f"{inv['invoice_no']} — {inv.get('project_name') or ''} — ${tot:,.2f}"
            with st.expander(title):
                st.write(f"Customer: {cust_map.get(inv['customer_id'],inv['customer_id'])}")
                st.write(f"Paid: {'✅' if inv['paid'] else '❌'}")
                c1,c2,c3=st.columns(3)
                if c1.button("Mark Paid / Unpaid",key=f"toggle_{inv['invoice_no']}"):
                    with engine.begin() as conn: conn.execute(text("UPDATE invoices SET paid = NOT paid WHERE invoice_no=:id"),{"id":inv["invoice_no"]}); st.rerun()
                if c2.button("View PDF",key=f"view_{inv['invoice_no']}"):
                    items_pdf=json.loads(inv["items_json"] or "[]"); subtotal_pdf=compute_subtotal(items_pdf)
                    pdf=build_pdf(ref_no=inv["invoice_no"],cust_name=cust_map.get(inv["customer_id"],inv["customer_id"]),
                        project_name=inv.get("project_name"),project_location=inv.get("project_location"),
                        items=items_pdf,subtotal=subtotal_pdf,deposit=inv.get("deposit") or 0,
                        grand_total=inv.get("total") or subtotal_pdf,check_number=inv.get("check_number"),
                        show_paid=bool(inv.get("paid")),notes="Thank you for your business!",is_proposal=False)
                    show_pdf_newtab(pdf,"📄 Open Invoice PDF")
                # Internal cost entry
                cost_val=st.number_input("Internal Cost (not shown to customer)",min_value=0.0,value=float(inv.get("internal_cost") or 0),step=50.0,key=f"cost_{inv['invoice_no']}")
                if st.button("💾 Save Cost",key=f"savecost_{inv['invoice_no']}"):
                    with engine.begin() as conn: conn.execute(text("UPDATE invoices SET internal_cost=:c WHERE invoice_no=:id"),{"c":cost_val,"id":inv["invoice_no"]})
                    st.success("Internal cost saved."); st.rerun()
                # Download PDF
                st.download_button("⬇️ Download PDF",data=build_pdf(
                    ref_no=inv["invoice_no"],cust_name=cust_map.get(inv["customer_id"],inv["customer_id"]),
                    project_name=inv.get("project_name"),project_location=inv.get("project_location"),
                    items=json.loads(inv["items_json"] or "[]"),subtotal=compute_subtotal(json.loads(inv["items_json"] or "[]")),
                    deposit=inv.get("deposit") or 0,grand_total=inv.get("total") or 0,
                    check_number=inv.get("check_number"),show_paid=bool(inv.get("paid")),
                    notes="Thank you for your business!",is_proposal=False),
                    file_name=f"{inv['invoice_no']}.pdf",key=f"dl_{inv['invoice_no']}")

    # Dashboard: Converted Proposals
    st.markdown("---"); st.subheader("📑 Converted Proposals")
    with engine.begin() as conn:
        converted_props=conn.execute(text("SELECT * FROM proposals WHERE status='converted' ORDER BY created_at DESC")).mappings().all()
        cust_map2={c["id"]:c["name"] for c in conn.execute(text("SELECT id,name FROM customers")).mappings().all()}
    if not converted_props: st.info("No converted proposals yet.")
    else:
        for prop in converted_props:
            with st.expander(f"{prop['id']} — {prop.get('project_name') or ''}"):
                st.write(f"Customer: {cust_map2.get(prop['customer_id'],prop['customer_id'])}")
                st.write(f"Location: {prop.get('project_location') or ''}"); st.write(f"Status: {prop['status']}")
                c1,c2=st.columns(2)
                if c1.button("Load into Invoice Maker",key=f"load_{prop['id']}"):
                    ss.prefill_customer_id=prop["customer_id"]; ss.prefill_items=json.loads(prop["items_json"] or "[]")
                    ss.project_name_value=prop.get("project_name") or ""; ss.project_location_value=prop.get("project_location") or ""
                    ss.prefill_proposal_number=prop["number"]; ss.prefill_proposal_id=prop["id"]; ss.i_nonce+=1
                    st.success(f"Proposal {prop['id']} loaded above."); st.rerun()
                if c2.button("View Proposal PDF",key=f"view_conv_{prop['id']}"):
                    prop_items=json.loads(prop["items_json"] or "[]"); prop_subtotal=compute_subtotal(prop_items)
                    prop_pdf=build_pdf(ref_no=prop["id"],cust_name=cust_map2.get(prop["customer_id"],prop["customer_id"]),
                        project_name=prop.get("project_name"),project_location=prop.get("project_location"),
                        items=prop_items,subtotal=prop_subtotal,deposit=0,grand_total=prop_subtotal,
                        check_number=None,is_proposal=True,notes=prop.get("notes"))
                    show_pdf_newtab(prop_pdf,"📄 Open Proposal PDF")

    # =========================
    # YTD SUMMARY
    # =========================
    st.markdown("---")
    with st.expander("📊 Year-to-Date Summary"):
        year = st.number_input("Year", min_value=2000, max_value=2100, value=now_ct().year, step=1)
        start = datetime(year, 1, 1, tzinfo=CT)
        end   = datetime(year+1, 1, 1, tzinfo=CT)
        with engine.begin() as conn:
            totals = conn.execute(text("""
                SELECT COALESCE(SUM(total),0) AS total_sum,
                       COALESCE(SUM(internal_cost),0) AS cost_sum
                FROM invoices
                WHERE created_at >= :start AND created_at < :end
            """), {"start": start, "end": end}).mappings().first()
        total_sum = float(totals["total_sum"] or 0)
        cost_sum  = float(totals["cost_sum"] or 0)
        profit    = total_sum - cost_sum
        st.write(f"**Grand Totals (Customer):** ${total_sum:,.2f}")
        st.write(f"**Internal Costs (You):** ${cost_sum:,.2f}")
        st.write(f"**Profit (Difference):** ${profit:,.2f}")
