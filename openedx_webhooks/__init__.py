from __future__ import print_function, unicode_literals

import os
import sys
import json
from datetime import datetime
from urllib2 import URLError

from flask import Flask, render_template, request, url_for, flash, redirect
import requests
from urlobject import URLObject
from .oauth import jira, jira_request
from .models import db, OAuthCredential
from bugsnag.flask import handle_exceptions

app = Flask(__name__)
handle_exceptions(app)
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ["DATABASE_URL"]
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "secrettoeveryone")
db.init_app(app)


@app.route("/")
def index():
    """
    Just to verify that things are working
    """
    return render_template("main.html")


@app.route('/oauth/jira')
def jira_oauth():
    return jira.authorize(callback=url_for('jira_oauth_authorized',
        next=request.args.get('next') or request.referrer or None))


@app.route("/oauth/jira/authorized")
def jira_oauth_authorized():
    resp = jira.authorized_response()
    next_url = request.args.get('next') or url_for('index')
    if not resp:
        flash("You denied the request to sign in.")
        return redirect(next_url)
    creds = OAuthCredential(
        name="jira",
        token=resp["oauth_token"],
        secret=resp["oauth_token_secret"],
        created_on=datetime.utcnow(),
    )
    db.session.add(creds)
    db.session.commit()

    flash("Signed in successfully")
    return redirect(next_url)


@app.route("/test-comment")
def test_comment():
    resp = jira.post(
        "/rest/api/2/issue/OSPR-1/comment",
        headers={"Accept": "application/json"},
        content_type="application/json",
        data=json.dumps({
            "body": "Posted from Heroku over OAuth"
        })
    )
    s = "{0} {1}".format(resp.status, resp.data)
    if resp.status not in (200, 201):
        print(s, file=sys.stderr)
    return s


@app.route("/issue/created", methods=("POST",))
def issue_created():
    """
    Received an "issue created" event from JIRA.
    https://developer.atlassian.com/display/JIRADEV/JIRA+Webhooks+Overview

    Ideally, this should be handled in a task queue, but we want to stay within
    Heroku's free plan, so it will be handled inline instead.
    (A worker dyno costs money.)
    """
    try:
        event = request.get_json()
    except ValueError:
        raise ValueError("Invalid JSON from JIRA: {data}".format(data=request.data))

    if app.debug:
        print(json.dumps(event), file=sys.stderr)

    issue_key = event["issue"]["key"]
    issue_status = event["issue"]["fields"]["status"]["name"]
    if issue_status != "Needs Triage":
        print(
            "{key} has status {status}, does not need to be processed".format(
                key=issue_key, status=issue_status,
            ),
            file=sys.stderr,
        )
        return "issue does not need to be triaged"

    issue_url = URLObject(event["issue"]["self"])
    user_url = URLObject(event["user"]["self"])
    user_url = user_url.set_query_param("expand", "groups")
    user_resp = jira_request(user_url)

    if not 200 <= user_resp.status < 300:
        raise URLError(user_resp.text)
    user = user_resp.data
    groups = {g["name"]: g["self"] for g in user["groups"]["items"]}

    # skip "Needs Triage" if bug was created by edX employee
    transitioned = False
    if "edx-employees" in groups:
        transitions_url = issue_url.with_path(issue_url.path + "/transitions")
        transitions_resp = jira_request(transitions_url)
        if not 200 <= transitions_resp.status < 300:
            raise URLError(transitions_resp.text)
        transitions = {t["name"]: t["id"] for t in transitions_resp.data["transitions"]}
        if "Open" in transitions:
            new_status = "Open"
        elif "Design Backlog" in transitions:
            new_status = "Design Backlog"
        else:
            raise ValueError("No valid transition! Possibilities are {}".format(transitions.keys()))

        body = {
            "transition": {
                "id": transitions[new_status],
            }
        }
        transition_resp = jira_request(transitions_url, data=body, method="POST")
        if not 200 <= transition_resp.status < 300:
            raise URLError(transition_resp.text)
        transitioned = True

    # log to stderr
    print(
        "{key} created by {name} ({username}), {action}".format(
            key=issue_key, name=event["user"]["displayName"],
            username=event["user"]["name"],
            action="Transitioned to Open" if transitioned else "ignored",
        ),
        file=sys.stderr,
    )
    return "Processed"


if __name__ == "__main__":
    app.run()
