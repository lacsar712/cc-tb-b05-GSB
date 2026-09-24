import os
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


def writer_required(fn):
    @wraps(fn)
    def wrap(*args, **kwargs):
        if session.get("role") != "writer":
            return ("仅审评员可操作", 403)
        return fn(*args, **kwargs)

    return wrap


ROW_SQL = (
    "SELECT c.*, ro.return_no AS open_return_no, ro.reason AS open_return_reason, "
    "rc.return_no AS any_return_no, "
    "rr.return_no AS ref_return_no "
    "FROM cuppings c "
    "LEFT JOIN cupping_returns ro ON ro.cupping_id = c.id AND ro.status = 'open' "
    "LEFT JOIN cupping_returns rc ON rc.cupping_id = c.id "
    "LEFT JOIN cupping_returns rr ON rr.id = c.return_id"
)


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
        cur.execute(ROW_SQL + " ORDER BY c.id DESC")
        rows = cur.fetchall()
    return render_template("home.html", rows=rows, can_write=session.get("role") == "writer")


@app.post("/cuppings")
@login_required
@writer_required
def create():
    try:
        aroma = float(request.form["aroma"])
        taste = float(request.form["taste"])
        liquor = float(request.form["liquor"])
    except (KeyError, TypeError, ValueError):
        return ("香气、滋味、汤色需为数值", 400)
    lot = request.form.get("lot", "").strip()
    if not lot:
        return ("批次必填", 400)
    return_no = request.form.get("return_no", "").strip()
    verdict, note, score = weigh(aroma, taste, liquor)
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        return_id = None
        if return_no:
            # 重评：必须引用一张有效（存在且未消单）的退回单
            cur.execute(
                "SELECT * FROM cupping_returns WHERE return_no = %s FOR UPDATE",
                (return_no,),
            )
            ret = cur.fetchone()
            if not ret:
                conn.rollback()
                return (f"退回单号 {return_no} 不存在", 400)
            if ret["status"] != "open":
                conn.rollback()
                return (f"退回单 {return_no} 已消单，不能再引用", 400)
            return_id = ret["id"]
        # 未引用单号的普通交评：return_id 为空，不触碰任何退回单，不自动消单
        cur.execute(
            """INSERT INTO cuppings
                   (lot, aroma, taste, liquor, score, verdict, note, created_by, return_id)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (lot, aroma, taste, liquor, score, verdict, note, session["user"], return_id),
        )
        row = cur.fetchone()
        if return_id is not None:
            cur.execute(
                "UPDATE cupping_returns SET status = 'closed' WHERE id = %s",
                (return_id,),
            )
        conn.commit()
    if request.headers.get("HX-Request"):
        with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(ROW_SQL + " WHERE c.id = %s", (row["id"],))
            row = cur.fetchone()
        return render_template("_row.html", row=row, can_write=True)
    return redirect(url_for("home"))


@app.post("/cuppings/<int:cupping_id>/returns")
@login_required
@writer_required
def create_return(cupping_id):
    reason = request.form.get("reason", "").strip()
    if not reason:
        return ("退回原因必填", 400)
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM cuppings WHERE id = %s FOR UPDATE", (cupping_id,))
        cupping = cur.fetchone()
        if not cupping:
            conn.rollback()
            return ("审评行不存在", 404)
        if cupping["verdict"] == "通过":
            conn.rollback()
            return ("仅不通过行可发起退回重评", 400)
        cur.execute(
            "SELECT 1 FROM cupping_returns WHERE cupping_id = %s",
            (cupping_id,),
        )
        if cur.fetchone():
            conn.rollback()
            return ("该行已发起过退回，不能重复发起", 400)
        cur.execute(
            """INSERT INTO cupping_returns (return_no, cupping_id, reason, status, created_by)
               VALUES ('RT' || lpad(nextval('cupping_returns_no_seq')::text, 6, '0'),
                       %s, %s, 'open', %s)
               RETURNING *""",
            (cupping_id, reason, session["user"]),
        )
        ret = cur.fetchone()
        conn.commit()
    if request.headers.get("HX-Request"):
        with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(ROW_SQL + " WHERE c.id = %s", (cupping_id,))
            row = cur.fetchone()
        return render_template("_row.html", row=row, can_write=True)
    return redirect(url_for("home"))


@app.get("/returns")
@login_required
def returns_page():
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM cupping_returns ORDER BY id DESC")
        ret_orders = cur.fetchall()
        cur.execute("SELECT * FROM cuppings")
        cuppings = cur.fetchall()
    by_id = {c["id"]: c for c in cuppings}
    review_by_return = {c["return_id"]: c for c in cuppings if c["return_id"] is not None}
    chains = []
    for ret in ret_orders:
        chains.append(
            {
                "ret": ret,
                "origin": by_id.get(ret["cupping_id"]),
                "review": review_by_return.get(ret["id"]),
            }
        )
    return render_template(
        "returns.html", chains=chains, can_write=session.get("role") == "writer"
    )
