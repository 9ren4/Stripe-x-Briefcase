import json

from flask import Flask, jsonify, render_template, request

import agent
import memory

app = Flask(__name__)
INBOX = json.load(open("inbox.json"))
memory.seed_policy(agent.POLICY)


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/state")
def state():
    return jsonify(inbox=INBOX, cases=agent.load_cases(), policy=agent.POLICY, batch=agent.current_batch())


@app.post("/api/process/<msg_id>")
def process(msg_id):
    msg = next(m for m in INBOX if m["id"] == msg_id)
    return jsonify(agent.process(msg))


@app.post("/api/approve/<case_id>")
def approve(case_id):
    return jsonify(agent.approve(case_id, request.json.get("approver", "finance@company.com")))


@app.post("/api/reject/<case_id>")
def reject(case_id):
    return jsonify(agent.reject(case_id, request.json.get("approver", "finance@company.com")))


@app.post("/api/ask")
def ask():
    return jsonify(answer=agent.explain(request.json["question"]))


@app.errorhandler(Exception)
def on_error(e):
    print("error:", repr(e))
    return jsonify(error=str(e)), 500


if __name__ == "__main__":
    app.run(port=5050, debug=True, threaded=True)
