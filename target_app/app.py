"""
Mock "core banking console" — the proxy target for the discovery/replay
agent. Deliberately built to look like the legacy surfaces described in the
brief: server-rendered HTML, table-based layout, no data-testid attributes,
plain class names that could plausibly collide across a vendor's tenants.

Simulates the runtime conditions the replay engine must handle:
  - member not found            -> legitimate business outcome
  - validation error on form    -> legitimate business outcome
  - permission denial           -> injectable via ?deny=1
  - session timeout             -> injectable via ?expire=1
  - transient slow load         -> injectable via ?slow=1

Run: python3 target_app/app.py  (serves on http://localhost:5000)
"""

from flask import Flask, request, redirect, session, url_for
import time
import uuid

app = Flask(__name__)
app.secret_key = "dev-only-not-for-production"

MEMBERS = {
    "12345": {"name": "J. Alvarez", "savings_balance": "4,210.55"},
    "67890": {"name": "R. Chen", "savings_balance": "912.10"},
}

CONFIRMATIONS = {}


def layout(body: str) -> str:
    # Intentionally plain/legacy markup: nested tables, no semantic ids.
    return f"""<html><head><title>CoreBank Teller Console</title></head>
<body>
<table width="100%"><tr><td bgcolor="#003366">
  <font color="white" size="4">&nbsp;CoreBank Teller Console v3.2</font>
</td></tr></table>
<table width="100%"><tr><td>
{body}
</td></tr></table>
</body></html>"""


@app.route("/")
def home():
    return layout("""
    <h2>Member Search</h2>
    <form action="/search" method="get">
      <table>
        <tr><td>Member ID:</td><td><input type="text" name="member_id"></td></tr>
        <tr><td></td><td><input type="submit" value="Search"></td></tr>
      </table>
    </form>
    """)


@app.route("/search")
def search():
    if request.args.get("slow") == "1":
        time.sleep(2)
    member_id = request.args.get("member_id", "").strip()
    member = MEMBERS.get(member_id)
    if not member:
        # legitimate business outcome, not an error page
        return layout(f"""
        <h2>Member Search</h2>
        <p><b>No record found for member ID "{member_id}".</b></p>
        <p><a href="/">Back to search</a></p>
        """)
    qs = ""
    if request.args.get("deny") == "1":
        qs = "&deny=1"
    if request.args.get("expire") == "1":
        qs += "&expire=1"
    return layout(f"""
    <h2>Member Record</h2>
    <table border="1" cellpadding="4">
      <tr><td>Name</td><td>{member['name']}</td></tr>
      <tr><td>Member ID</td><td>{member_id}</td></tr>
      <tr><td>Savings Balance</td><td>${member['savings_balance']}</td></tr>
    </table>
    <br>
    <a href="/open-subaccount?member_id={member_id}{qs}">Open New Sub-Account</a>
    """)


@app.route("/open-subaccount")
def open_subaccount_form():
    member_id = request.args.get("member_id", "")
    member = MEMBERS.get(member_id)
    if not member:
        return layout("<p><b>Unknown member.</b></p>")

    if request.args.get("expire") == "1":
        return layout("""
        <h2>Session Expired</h2>
        <p><b>Your session has timed out. Please log in again.</b></p>
        <p><a href="/">Return to login</a></p>
        """)

    if request.args.get("deny") == "1":
        return layout("""
        <h2>Access Denied</h2>
        <p><b>You do not have permission to open sub-accounts for this member's
        segment. Contact your supervisor.</b></p>
        """)

    return layout(f"""
    <h2>Open New Sub-Account &mdash; {member['name']} ({member_id})</h2>
    <form action="/open-subaccount/submit" method="post">
      <input type="hidden" name="member_id" value="{member_id}">
      <table>
        <tr><td>Sub-Account Type:</td><td>
          <select name="account_type">
            <option value="">-- select --</option>
            <option value="youth_savings">Youth Savings</option>
            <option value="holiday_club">Holiday Club</option>
          </select>
        </td></tr>
        <tr><td>Initial Deposit ($):</td><td><input type="text" name="deposit"></td></tr>
        <tr><td></td><td><input type="submit" value="Continue"></td></tr>
      </table>
    </form>
    """)


@app.route("/open-subaccount/submit", methods=["POST"])
def open_subaccount_submit():
    member_id = request.form.get("member_id", "")
    account_type = request.form.get("account_type", "")
    deposit = request.form.get("deposit", "")

    errors = []
    if not account_type:
        errors.append("Sub-Account Type is required.")
    try:
        if not deposit or float(deposit) < 25:
            errors.append("Initial Deposit must be at least $25.")
    except ValueError:
        errors.append("Initial Deposit must be a number.")

    if errors:
        # legitimate business outcome: validation error, re-show form
        err_html = "".join(f"<li>{e}</li>" for e in errors)
        return layout(f"""
        <h2>Open New Sub-Account</h2>
        <p style="color:red"><b>Please correct the following:</b></p>
        <ul style="color:red">{err_html}</ul>
        <p><a href="/open-subaccount?member_id={member_id}">Try again</a></p>
        """)

    confirmation_number = f"CNF-{uuid.uuid4().hex[:8].upper()}"
    CONFIRMATIONS[confirmation_number] = {
        "member_id": member_id, "account_type": account_type, "deposit": deposit
    }
    return layout(f"""
    <h2>Confirmation</h2>
    <p><b>Sub-account opened successfully.</b></p>
    <table border="1" cellpadding="4">
      <tr><td>Confirmation Number</td><td>{confirmation_number}</td></tr>
      <tr><td>Account Type</td><td>{account_type}</td></tr>
      <tr><td>Initial Deposit</td><td>${deposit}</td></tr>
    </table>
    """)


if __name__ == "__main__":
    app.run(port=5050, debug=False)
