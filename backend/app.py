import os
import re
from functools import wraps

import psycopg2
from flask import Flask, redirect, render_template, request, session, url_for
from psycopg2.extras import RealDictCursor

from rules import weigh

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "tea-cupping-dev-secret")

ACCOUNTS = {
    "taster": {"password": "tea123456", "role": "writer"},
    "observer": {"password": "look123456", "role": "reader"},
}


def db():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def login_required(fn):
    @wraps(fn)
    def wrap(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        return fn(*args, **kwargs)

    return wrap


@app.get("/health")
def health():
    return {"status": "ok", "service": "tea-blend-cupping"}


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        name = request.form.get("username", "").strip()
        account = ACCOUNTS.get(name)
        if not account or account["password"] != request.form.get("password", ""):
            error = "用户名或密码错误"
        else:
            session["user"] = name
            session["role"] = account["role"]
            return redirect(url_for("home"))
    return render_template("login.html", error=error)


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
@login_required
def home():
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM cuppings ORDER BY id DESC")
        rows = cur.fetchall()
        cur.execute("SELECT * FROM return_tickets ORDER BY id DESC")
        tickets = cur.fetchall()
    open_returns = {}
    last_returns = {}
    for ticket in tickets:
        last_returns.setdefault(ticket["cupping_id"], ticket)
        if ticket["status"] == "open":
            open_returns.setdefault(ticket["cupping_id"], ticket)
    return render_template(
        "home.html",
        rows=rows,
        can_write=session.get("role") == "writer",
        open_returns=open_returns,
        last_returns=last_returns,
    )


@app.post("/cuppings")
@login_required
def create():
    if session.get("role") != "writer":
        return ("仅审评员可提交拼配审评", 403)
    aroma = float(request.form["aroma"])
    taste = float(request.form["taste"])
    liquor = float(request.form["liquor"])
    lot = request.form["lot"].strip()
    ticket_raw = request.form.get("ticket", "").strip()
    ticket_id = None
    if ticket_raw:
        digits = re.search(r"\d+", ticket_raw)
        if not digits:
            return ("退回单号格式不对", 400)
        ticket_id = int(digits.group())
    verdict, note, score = weigh(aroma, taste, liquor)
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        if ticket_id is not None:
            cur.execute(
                "UPDATE return_tickets SET status = 'closed' WHERE id = %s AND status = 'open' RETURNING id",
                (ticket_id,),
            )
            if cur.fetchone() is None:
                cur.execute("SELECT status FROM return_tickets WHERE id = %s", (ticket_id,))
                if cur.fetchone() is None:
                    return ("退回单号不存在", 404)
                return ("该退回单已消单，不能重复引用", 409)
        cur.execute(
            """INSERT INTO cuppings (lot, aroma, taste, liquor, score, verdict, note, created_by, return_id)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (lot, aroma, taste, liquor, score, verdict, note, session["user"], ticket_id),
        )
        row = cur.fetchone()
        conn.commit()
    if request.headers.get("HX-Request"):
        return render_template("_row.html", row=row, can_write=True, open_ret=None, last_ret=None)
    return redirect(url_for("home"))


@app.post("/cuppings/<int:cupping_id>/return")
@login_required
def initiate_return(cupping_id):
    if session.get("role") != "writer":
        return ("仅审评员可发起退回", 403)
    reason = request.form.get("reason", "").strip()
    if not reason:
        return ("发起退回必须填写退回原因", 400)
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM cuppings WHERE id = %s", (cupping_id,))
        row = cur.fetchone()
        if row is None:
            return ("审评行不存在", 404)
        if row["verdict"] != "不通过":
            return ("仅不通过行可发起退回重评", 400)
        cur.execute(
            "SELECT id FROM return_tickets WHERE cupping_id = %s AND status = 'open'",
            (cupping_id,),
        )
        if cur.fetchone():
            return ("该笔已有待重评的退回单", 409)
        cur.execute(
            "INSERT INTO return_tickets (cupping_id, reason, created_by) VALUES (%s,%s,%s)",
            (cupping_id, reason, session["user"]),
        )
        conn.commit()
    return redirect(url_for("returns_page"))


@app.get("/returns")
@login_required
def returns_page():
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """SELECT t.*, c.lot, c.aroma, c.taste, c.liquor, c.score, c.verdict, c.note
               FROM return_tickets t JOIN cuppings c ON c.id = t.cupping_id
               ORDER BY t.id DESC"""
        )
        tickets = cur.fetchall()
        cur.execute("SELECT * FROM cuppings WHERE return_id IS NOT NULL ORDER BY id")
        reviews = cur.fetchall()
    reviews_by_ticket = {}
    for review in reviews:
        reviews_by_ticket.setdefault(review["return_id"], []).append(review)
    return render_template(
        "returns.html",
        tickets=tickets,
        reviews_by_ticket=reviews_by_ticket,
        can_write=session.get("role") == "writer",
    )
