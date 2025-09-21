import os, json, io, smtplib
from email.message import EmailMessage
import streamlit as st
from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas
from reportlab.lib.units import inch
from sqlalchemy import create_engine, text

st.set_page_config(page_title="J&I Invoicing", page_icon="🧾", layout="centered")
st.title("🧾 J&I Invoicing — Proposals & Invoices")

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
        conn.execute(text("ALTER TABLE proposals ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'open';"))
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
                project TEXT,
                items_json TEXT DEFAULT '[]',
                status TEXT DEFAULT 'open',
                created_at TIMESTAMPTZ DEFAULT NOW()
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

# Utility: build PDF
def build_pdf(invoice_no, cust_name, project, items, total_amount, show_paid=False):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=LETTER)
    width, height = LETTER
    c.setFont("Helvetica-Bold", 16)
    c.drawString(1*inch, height-1*inch, "J & I Heating and Cooling")
    c.setFont("Helvetica", 12)
    c.drawString(1*inch, height-1.3*inch, f"Invoice #: {invoice_no}")
    c.drawString(1*inch, height-1.6*inch, f"Customer: {cust_name}")
    c.drawString(1*inch, height-1.9*inch, f"Project: {project}")
    y = height-2.3*inch
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

# Mode switch
mode = st.radio("Choose Mode", ["Proposal", "Invoice"], horizontal=True)

# Customers
with engine.begin() as conn:
    customers = conn.execute(text("SELECT * FROM customers ORDER BY name")).mappings().all()
if not customers:
    st.warning("No customers yet. Add them using the Intake Form or directly in Supabase.")
    st.stop()

cust = st.selectbox("Select Customer", customers, format_func=lambda c: c["name"])

project = st.text_input("Project / Job", "")
items = []
st.write("Line Items")
for i in range(5):
    cols = st.columns([5,1,2,2])
    desc = cols[0].text_input(f"Description {i+1}", key=f"desc_{mode}_{i}")
    qty = cols[1].number_input(f"Qty {i+1}", 0, 100, 1, key=f"qty_{mode}_{i}")
    unit = cols[2].number_input(f"Unit Price {i+1}", 0.0, 10000.0, 0.0, key=f"unit_{mode}_{i}")
    total = qty * unit
    cols[3].write(f"${total:,.2f}")
    if desc.strip():
        items.append({"Description":desc,"Qty":qty,"Unit Price":unit})
total_amount = sum(r["Qty"]*r["Unit Price"] for r in items)

# Proposal mode
if mode == "Proposal":
    pid = st.text_input("Proposal ID", "P-1001")
    if st.button("💾 Save Proposal"):
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO proposals(id,customer_id,project,items_json,status)
                VALUES(:id,:cid,:proj,:items,'open')
                ON CONFLICT(id) DO UPDATE
                SET customer_id=EXCLUDED.customer_id,
                    project=EXCLUDED.project,
                    items_json=EXCLUDED.items_json,
                    status='open'
            """), dict(id=pid, cid=cust["id"], proj=project, items=json.dumps(items)))
        st.success(f"Proposal {pid} saved!")

    # Proposal Dashboard
    st.subheader("📑 Proposal Dashboard (Open Only)")
    with engine.begin() as conn:
        props = conn.execute(text("""
            SELECT p.*, c.name AS customer_name
            FROM proposals p
            JOIN customers c ON c.id=p.customer_id
            WHERE p.status='open'
            ORDER BY p.created_at DESC
        """)).mappings().all()

    if not props:
        st.info("No open proposals.")
    else:
        for p in props:
            st.markdown(f"""
            **Proposal ID:** {p['id']}  
            **Customer:** {p['customer_name']}  
            **Project:** {p['project'] or "—"}  
            **Created:** {p['created_at']}  
            """)
            items_list = json.loads(p["items_json"])
            if items_list:
                st.write("Items:")
                for row in items_list:
                    st.write(f"- {row['Description']} — {row['Qty']} × ${row['Unit Price']:.2f}")

            if st.button(f"Convert {p['id']} to Invoice"):
                new_invoice_no = f"INV-{p['id']}"
                with engine.begin() as conn:
                    conn.execute(text("""
                        INSERT INTO invoices(invoice_no,customer_id,project,items_json,total,paid)
                        VALUES(:inv,:cid,:proj,:items,:total,:paid)
                        ON CONFLICT(invoice_no) DO UPDATE
                        SET customer_id=EXCLUDED.customer_id,
                            project=EXCLUDED.project,
                            items_json=EXCLUDED.items_json,
                            total=EXCLUDED.total,
                            paid=EXCLUDED.paid
                    """), dict(
                        inv=new_invoice_no,
                        cid=p["customer_id"],
                        proj=p["project"],
                        items=p["items_json"],
                        total=sum(row["Qty"]*row["Unit Price"] for row in json.loads(p["items_json"])),
                        paid=False
                    ))
                    conn.execute(text("UPDATE proposals SET status='closed' WHERE id=:id"), dict(id=p["id"]))
                st.success(f"Proposal {p['id']} converted → Invoice {new_invoice_no}")
                st.experimental_rerun()  # refresh UI so invoice shows

            st.divider()

# Invoice mode
if mode == "Invoice":
    invoice_no = st.text_input("Invoice #", "1001")
    show_paid = st.toggle("Show PAID Stamp", value=False)

    st.write(f"**Total: ${total_amount:,.2f}**")

    col1,col2,col3 = st.columns(3)
    with col1:
        if st.button("📄 Download PDF"):
            st.download_button("Save PDF", build_pdf(invoice_no, cust['name'], project, items, total_amount, show_paid),
                               file_name=f"Invoice_{invoice_no}.pdf")
    with col2:
        if st.button("📧 Email Invoice"):
            if not APP_PASSWORD:
                st.error("APP_PASSWORD missing")
            else:
                msg = EmailMessage()
                msg["From"] = FROM_EMAIL
                msg["To"] = cust["email"]
                msg["Subject"] = f"Invoice {invoice_no}"
                msg.set_content("Please find attached invoice.")
                msg.add_attachment(build_pdf(invoice_no, cust['name'], project, items, total_amount, show_paid),
                                   maintype="application", subtype="pdf", filename=f"Invoice_{invoice_no}.pdf")
                with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
                    server.login(FROM_EMAIL, APP_PASSWORD)
                    server.send_message(msg)
                st.success("Invoice emailed")
    with col3:
        if st.button("💾 Save Invoice"):
            with engine.begin() as conn:
                conn.execute(text("""
                    INSERT INTO invoices(invoice_no,customer_id,project,items_json,total,paid)
                    VALUES(:inv,:cid,:proj,:items,:total,:paid)
                    ON CONFLICT(invoice_no) DO UPDATE
                    SET customer_id=EXCLUDED.customer_id,
                        project=EXCLUDED.project,
                        items_json=EXCLUDED.items_json,
                        total=EXCLUDED.total,
                        paid=EXCLUDED.paid
                """), dict(inv=invoice_no, cid=cust["id"], proj=project,
                           items=json.dumps(items), total=total_amount, paid=show_paid))
            st.success(f"Invoice {invoice_no} saved!")

    # Invoice Dashboard
    st.subheader("📊 Invoice Dashboard")
    with engine.begin() as conn:
        invoices = conn.execute(text("""
            SELECT i.*, c.name AS customer_name
            FROM invoices i
            JOIN customers c ON c.id=i.customer_id
            ORDER BY i.created_at DESC
        """)).mappings().all()

    if not invoices:
        st.info("No invoices saved yet.")
    else:
        for inv in invoices:
            items_list = json.loads(inv["items_json"])
            st.markdown(f"""
            **Invoice #:** {inv['invoice_no']}  
            **Customer:** {inv['customer_name']}  
            **Project:** {inv['project'] or "—"}  
            **Total:** ${inv['total']:.2f}  
            **Paid:** {"✅ Yes" if inv['paid'] else "❌ No"}  
            **Created:** {inv['created_at']}  
            """)
            if items_list:
                st.write("Items:")
                for row in items_list:
                    st.write(f"- {row['Description']} — {row['Qty']} × ${row['Unit Price']:.2f}")

            # Dashboard actions
            colA, colB, colC = st.columns(3)
            with colA:
                st.download_button("📄 View PDF",
                    build_pdf(inv['invoice_no'], inv['customer_name'], inv['project'], items_list, inv['total'], inv['paid']),
                    file_name=f"Invoice_{inv['invoice_no']}.pdf")
            with colB:
                if st.button(f"📧 Email {inv['invoice_no']}"):
                    if not APP_PASSWORD:
                        st.error("APP_PASSWORD missing")
                    else:
                        msg = EmailMessage()
                        msg["From"] = FROM_EMAIL
                        msg["To"] = cust["email"]
                        msg["Subject"] = f"Invoice {inv['invoice_no']}"
                        msg.set_content("Please find attached invoice.")
                        msg.add_attachment(
                            build_pdf(inv['invoice_no'], inv['customer_name'], inv['project'], items_list, inv['total'], inv['paid']),
                            maintype="application", subtype="pdf", filename=f"Invoice_{inv['invoice_no']}.pdf"
                        )
                        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
                            server.login(FROM_EMAIL, APP_PASSWORD)
                            server.send_message(msg)
                        st.success(f"Invoice {inv['invoice_no']} emailed")
            with colC:
                if st.button(f"💲 Toggle Paid {inv['invoice_no']}"):
                    with engine.begin() as conn:
                        conn.execute(text("UPDATE invoices SET paid=NOT paid WHERE invoice_no=:inv"),
                                     dict(inv=inv["invoice_no"]))
                    st.experimental_rerun()

            st.divider()
# app.py (Invoicing App with Postgres)
# Full Streamlit code provided in chat previously
