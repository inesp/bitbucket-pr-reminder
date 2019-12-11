#!/usr/bin/env python

from __future__ import absolute_import
from __future__ import print_function

import requests
import sys

from secrets import BITBUCKET_REPO_LINK
from secrets import BITBUCKET_TOKEN
from secrets import BITBUCKET_USER_NAME_TO_SLACK_USER_NAME
from secrets import BITBUCKET_USERNAME
from secrets import SLACK_WEBHOOK_URL

PR_BASE_LINK = "{repo}/pull-requests/{{pr_id}}/overview".format(
    repo=BITBUCKET_REPO_LINK
)


def fetch_all_prs(pr_limit):

    get_all_prs_url = "{repo_url}/pull-requests?state=OPEN&limit={limit}".format(
        repo_url=BITBUCKET_REPO_LINK, limit=pr_limit
    )
    response = requests.get(get_all_prs_url, auth=(BITBUCKET_USERNAME, BITBUCKET_TOKEN))

    if response.status_code != 200:
        raise Exception(
            "Could not connect to BitBucket repo. Response: {}".format(response.content)
        )

    response_json = response.json()
    if not response_json:
        raise Exception("BitBucket returned an error: {}".format(response_json))

    all_prs = response_json["values"]
    return all_prs


def limit_to_my_prs(all_prs):
    my_prs = [
        pr for pr in all_prs if pr["author"]["user"]["name"] == BITBUCKET_USERNAME
    ]
    return my_prs


def get_slack_username(bitbucket_username):
    return BITBUCKET_USER_NAME_TO_SLACK_USER_NAME.get(
        bitbucket_username, bitbucket_username
    )


def create_reminder_message_for(pr):
    pr_link = PR_BASE_LINK.format(pr_id=pr["id"])
    pr_title = pr["title"]
    people_to_ping = set()

    for person in pr["reviewers"]:
        if person["status"] == "UNAPPROVED":
            reviewer_name = person["user"]["name"]

            slack_nick_name = get_slack_username(reviewer_name)
            people_to_ping.add("@{}".format(slack_nick_name))

    if not people_to_ping:
        # the PR is waiting for ME
        people_to_ping.add("@{}".format(get_slack_username(BITBUCKET_USERNAME)))

    return "{people_to_ping} :pray: {pr_link} ({pr_title})".format(
        people_to_ping=" ".join(people_to_ping), pr_link=pr_link, pr_title=pr_title
    )


def send_reminders_to_slack(slack_msgs):
    response = requests.post(SLACK_WEBHOOK_URL, json={"text": "\n".join(slack_msgs)})
    assert response.status_code == 200


def collect_all_reminder_messages(my_prs):
    slack_msgs = []
    for pr in my_prs:
        msg = create_reminder_message_for(pr)
        if msg is not None:
            slack_msgs.append(msg)
            print (msg)

    return slack_msgs


def send_pr_reminders_to_slack(pr_limit):
    all_prs = fetch_all_prs(pr_limit)
    my_prs = limit_to_my_prs(all_prs)
    if not my_prs:
        print ("You have no open PRs")
        return

    slack_msgs = collect_all_reminder_messages(my_prs)
    send_reminders_to_slack(slack_msgs)


if __name__ == "__main__":

    if len(sys.argv) >= 2 and (sys.argv[1] == "--help" or sys.argv[1] == "-h"):
        print (
            "Fetch my OPEN PRs from Bitbucket, compose a reminder message "
            "for PR reviewers and send it to Slack."
        )
        exit()

    send_pr_reminders_to_slack(pr_limit=100)
