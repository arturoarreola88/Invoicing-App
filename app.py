# =========================
# J & I — Proposals & Invoices (Streamlit)
# =========================

import os, io, json, base64, textwrap, smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage
import pytz

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
# Timezone Helper
# =========================
CT = pytz.timezone("America/Chicago")
def now_ct():
    return datetime.now(CT)

# =========================
# Branding
# =========================
try:
    st.image("logo.png", width=220)
except:
    st.info("Place a logo.png in the app folder to display.")

st.title("🧾 J & I — Proposals & Invoices")

# =========================
# Environment / Secrets
# =========================
DATABASE_URL = st.secrets.get("DATABASE_URL", os.getenv("DATABASE_URL", ""))
FROM_EMAIL   = st.secrets.get("FROM_EMAIL",   os.getenv("FROM_EMAIL",   "jiheatingcooling.homerepairs@gmail.com"))
SMTP_SERVER  = st.secrets.get("SMTP_SERVER",  os.getenv("SMTP_SERVER",  "smtp.gmail.com"))
SMTP_PORT    = int(st.secrets.get("SMTP_PORT", os.getenv("SMTP_PORT", 465)))
APP_PASSWORD = st.secrets.get("APP_PASSWORD", os.getenv("APP_PASSWORD", ""))

if not DATABASE_URL:
    st.error("DATABASE_URL not set")
    st.stop()

engine = create_engine(DATABASE_URL, pool_pre_ping=True)

# =========================
# Database Init
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

# =========================
# Helpers
# =========================
ss = st.session_state
ss.setdefault("line_count", 5)

def add_line():
    ss.line_count += 1

def reset_proposal_form():
    ss.line_count = 5
    for k in list(ss.keys()):
        if k.startswith("p_") or k in ["project_name_value","project_location_value"]:
            del ss[k]
    st.rerun()

def reset_invoice_form():
    ss.line_count = 5
    for k in list(ss.keys()):
        if k.startswith("i_") or k in ["project_name_value","project_location_value"]:
            del ss[k]
    st.rerun()

def compute_subtotal(items):
    return sum(float(r.get("Qty",0)) * float(r.get("Unit Price",0)) for r in items)

def _max_existing_number(conn):
    r1 = conn.execute(text("SELECT COALESCE(MAX(number),0) FROM proposals")).scalar() or 0
    r2 = conn.execute(text("SELECT COALESCE(MAX(number),0) FROM invoices")).scalar() or 0
    return max(r1, r2)

def format_prop_id(n): return f"P-{n:04d}"
def format_inv_id(n):  return f"INV-{n:04d}"

def show_pdf_newtab(pdf_bytes: bytes, label: str = "📄 Open PDF in New Tab"):
    b64 = base64.b64encode(pdf_bytes).decode("utf-8")
    st.markdown(f'<a href="data:application/pdf;base64,{b64}" target="_blank">{label}</a>', unsafe_allow_html=True)

# =========================
# Email
# =========================
def build_email_body(cust_name, is_proposal, ref_no):
    first = (cust_name or "Customer").split()[0]
    hour = now_ct().hour
    if hour < 12: greeting = "Good morning"
    elif hour < 18: greeting = "Good afternoon"
    else: greeting = "Good evening"
    kind = "proposal" if is_proposal else "invoice"
    return f"""
    <p>{greeting} {first},</p>
    <p>Attached is the {kind} ({ref_no}) you requested. Please take a moment at your earliest convenience and look it over. If you have any questions, comments, or concerns please don’t hesitate to contact me.</p>
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
    terms = f"Valid until: {(issue+timedelta(days=15)).strftime('%m/%d/%Y')}" if is_proposal else f"Due: {issue.strftime('%m/%d/%Y')}"
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
    c.drawString(5*inch,y,"Subtotal:"); c.drawString(6.4*inch,y,f"${subtotal:,.2f}"); y-=18
    if not is_proposal:
        if deposit and float(deposit)>0:
            c.drawString(5*inch,y,"Deposit:"); c.drawString(6.4*inch,y,f"-${float(deposit):,.2f}"); y-=18
        c.drawString(5*inch,y,"Grand Total:"); c.drawString(6.4*inch,y,f"${grand_total:,.2f}")
        if check_number: y-=18; c.setFont("Helvetica",10); c.drawString(1*inch,y,f"Check #: {check_number}")
    else:
        c.drawString(5*inch,y,"Grand Total:"); c.drawString(6.4*inch,y,f"${subtotal:,.2f}")

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

# -------------------------
# PROPOSAL TAB
# -------------------------
with prop_tab:
    st.subheader("Create Proposal")
    # … (customer/project/line items UI like before) …

    # Build a sample PDF (replace with real values from UI in your version)
    pdf_prop = build_pdf("P-0001","Customer","","",[],0,0,0,None,is_proposal=True)

    cA,cB,cC,cD = st.columns(4)
    with cA: st.download_button("📄 Download Proposal",data=pdf_prop,file_name="Proposal.pdf")
    with cB: st.button("👀 View Proposal PDF")
    with cC: st.button("📧 Email Proposal")
    with cD: st.button("💾 Save Proposal")

    # 🔹 Reset button
    if st.button("♻ Reset Proposal Form"): reset_proposal_form()

# -------------------------
# INVOICE TAB (Recent Invoices + YTD Summary)
# -------------------------
with inv_tab:
    st.subheader("🧾 Recent Invoices")

    with engine.begin() as conn:
        invs = conn.execute(text("SELECT * FROM invoices ORDER BY created_at DESC LIMIT 20")).mappings().all()
        cust_map = {c["id"]: c["name"] for c in conn.execute(text("SELECT id,name FROM customers")).mappings().all()}

    if not invs:
        st.info("No invoices yet.")
    else:
        for inv in invs:
            tot=float(inv["total"] or 0)
            with st.expander(f"{inv['invoice_no']} — {inv.get('project_name') or ''} — ${tot:,.2f}"):
                st.write(f"Customer: {cust_map.get(inv['customer_id'], inv['customer_id'])}")
                st.write(f"Paid: {'✅' if inv['paid'] else '❌'}")

                # Internal cost
                cost_val=st.number_input("Internal Cost (not shown to customer)",
                                         min_value=0.0,value=float(inv.get("internal_cost") or 0),
                                         step=50.0,key=f"cost_{inv['invoice_no']}")
                if st.button(f"💾 Save Cost for {inv['invoice_no']}",key=f"savecost_{inv['invoice_no']}"):
                    with engine.begin() as conn:
                        conn.execute(text("UPDATE invoices SET internal_cost=:c WHERE invoice_no=:id"),
                                     {"c":cost_val,"id":inv["invoice_no"]})
                    st.success("Internal cost saved.")

                pdf=build_pdf(inv["invoice_no"],cust_map.get(inv["customer_id"],inv["customer_id"]),
                              inv.get("project_name"),inv.get("project_location"),
                              json.loads(inv["items_json"] or "[]"),
                              compute_subtotal(json.loads(inv["items_json"] or "[]")),
                              inv.get("deposit") or 0,inv.get("total") or 0,
                              inv.get("check_number"),bool(inv.get("paid")))

                st.download_button("⬇️ Download PDF",data=pdf,file_name=f"{inv['invoice_no']}.pdf")

    # 📊 YTD Summary
    st.markdown("---")
    with st.expander("📊 Year-to-Date Summary"):
        year = st.number_input("Year",min_value=2000,max_value=2100,value=now_ct().year,step=1)
        start=datetime(year,1,1,tzinfo=CT); end=datetime(year+1,1,1,tzinfo=CT)
        with engine.begin() as conn:
            totals=conn.execute(text("""
                SELECT COALESCE(SUM(total),0) as total_sum,
                       COALESCE(SUM(internal_cost),0) as cost_sum
                FROM invoices
                WHERE created_at >= :start AND created_at < :end
            """),{"start":start,"end":end}).mappings().first()
        st.write(f"**Grand Totals (Customer):** ${totals['total_sum']:,.2f}")
        st.write(f"**Internal Costs (You):** ${totals['cost_sum']:,.2f}")
        st.write(f"**Profit (Difference):** ${totals['total_sum']-totals['cost_sum']:,.2f}")
