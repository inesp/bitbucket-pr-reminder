#!/usr/bin/env python

from __future__ import absolute_import
from __future__ import print_function

import argparse
import requests

from local_scripts.secrets import BITBUCKET_API_REPO_LINK
from local_scripts.secrets import BITBUCKET_REPO_LINK
from local_scripts.secrets import BITBUCKET_TOKEN
from local_scripts.secrets import BITBUCKET_USER_NAME_TO_SLACK_USER_NAME
from local_scripts.secrets import BITBUCKET_USERNAME
from local_scripts.secrets import SLACK_WEBHOOK_URL

PR_BASE_LINK = "{repo}/pull-requests/{{pr_id}}/overview".format(
    repo=BITBUCKET_REPO_LINK
)

MSG_TEMPLATE = "<{pr_link}|{pr_title}>\nWaiting for: {people_to_ping}\n"


class PRFetcher(object):
    @staticmethod
    def _fetch_url(url):
        response = requests.get(url, auth=(BITBUCKET_USERNAME, BITBUCKET_TOKEN))

        if response.status_code != 200:
            raise Exception(
                "Could not connect to BitBucket repo. Response: {}".format(
                    response.content
                )
            )

        response_json = response.json()
        if not response_json:
            raise Exception("BitBucket returned an error: {}".format(response_json))

        return response_json

    @classmethod
    def fetch_one_pr(cls, pr_id):
        url = "{repo_url}/pull-requests/{pr_id}".format(
            repo_url=BITBUCKET_API_REPO_LINK, pr_id=pr_id
        )
        response_json = cls._fetch_url(url)

        if response_json["state "] != "OPEN":
            return None

        return response_json

    @classmethod
    def fetch_all_prs(cls, limit):
        url = "{repo_url}/pull-requests?state=OPEN&limit={limit}".format(
            repo_url=BITBUCKET_API_REPO_LINK, limit=limit
        )
        response_json = cls._fetch_url(url)

        all_prs = response_json["values"]
        return all_prs

    @classmethod
    def fetch_open_tasks(cls, pr_id):
        # tasks can only be fetched per PR, there is no other bulk fecthing for tasks
        url = "{repo_url}/pull-requests/{pr_id}/tasks".format(
            repo_url=BITBUCKET_API_REPO_LINK, pr_id=pr_id
        )
        response_json = cls._fetch_url(url)

        tasks = []
        for raw_task in response_json.get("values", []):
            if raw_task["state"] != "OPEN":
                continue

            tasks.append(raw_task["text"])

        return tasks

    @classmethod
    def fetch_mergeable_status(cls, pr_id):
        # the merge-ability of a PR can only be fetched per PR, no bulk fetching available
        url = "{repo_url}/pull-requests/{pr_id}/merge".format(
            repo_url=BITBUCKET_API_REPO_LINK, pr_id=pr_id
        )
        response_json = cls._fetch_url(url)

        return response_json


class SlackHandler(object):
    @staticmethod
    def get_slack_name_of(pr_user_name):
        slack_nick_name = BITBUCKET_USER_NAME_TO_SLACK_USER_NAME.get(
            pr_user_name, pr_user_name
        )
        return "@{}".format(slack_nick_name)

    @staticmethod
    def send_reminders(slack_msgs):
        response = requests.post(
            SLACK_WEBHOOK_URL, json={"text": "\n".join(slack_msgs)}
        )
        assert response.status_code == 200


class PRResolver(object):
    def __init__(self, pr_data):
        self.pr_data = pr_data

    @property
    def pr_id(self):
        return self.pr_data["id"]

    @property
    def link(self):
        return PR_BASE_LINK.format(pr_id=self.pr_data["id"])

    @property
    def title(self):
        return self.pr_data["title"]

    @property
    def author_name(self):
        return self.pr_data["author"]["user"]["name"]

    def get_undone_reviewers(self):
        people_to_ping = set()
        for reviewer in self.pr_data["reviewers"]:
            if reviewer["status"] == "UNAPPROVED":
                reviewer_name = reviewer["user"]["name"]
                people_to_ping.add(SlackHandler.get_slack_name_of(reviewer_name))

        return people_to_ping


class PRIsMergeableResolver(object):

    VETO_REASONS_WE_IGNORE = {
        "Requires approvals",  # we handle separatelly
        "Requires all tasks to be resolved",  # we handle separetelly
        "Insufficient branch permissions",  # irrelevant veto
    }

    VETO_BUILD_NOT_FINISHED = "Not all required builds are successful yet"

    def __init__(self, pr_id):
        self._merge_status = PRFetcher.fetch_mergeable_status(pr_id)

        self._valid_vetos = set()
        self.is_conflicted = False
        self.builds_in_progress = False
        self.builds_have_failed = False
        self._resolve_reasons()

    def _resolve_reasons(self):
        self._valid_vetos = set()
        self.is_conflicted = bool(self._merge_status["conflicted"])
        for veto in self._merge_status["vetoes"]:
            veto_msg = veto["summaryMessage"]

            if veto_msg in self.VETO_REASONS_WE_IGNORE:
                continue

            if veto_msg == self.VETO_BUILD_NOT_FINISHED:
                if "has failed builds" in veto["detailedMessage"]:
                    self.builds_have_failed = True
                else:
                    self.builds_in_progress = True
                continue

            self._valid_vetos.add(veto_msg)

    def merge_vetos(self):
        return self._valid_vetos


class PRReminder(object):
    @staticmethod
    def _prepare_pr_objects(limit, pr_id, users):
        if pr_id:
            pr = PRFetcher.fetch_one_pr(pr_id)
            raw_prs = [pr] if pr else []
        else:
            raw_prs = PRFetcher.fetch_all_prs(limit)

        all_prs = []
        for pr_data in raw_prs:
            pr = PRResolver(pr_data)

            if users and pr.author_name in users:
                all_prs.append(pr)

        return all_prs

    @classmethod
    def run(cls, limit=1000, pr_id=None, users=None):
        all_prs = cls._prepare_pr_objects(limit, pr_id, users)

        if not all_prs:
            print ("No open PRs found")
            return

        slack_msgs = []
        for pr in all_prs:
            people_to_ping = cls._collect_people_to_ping(pr)
            msg = MSG_TEMPLATE.format(
                people_to_ping=", ".join(people_to_ping),
                pr_link=pr.link,
                pr_title=pr.title,
            )
            slack_msgs.append(msg)

        SlackHandler.send_reminders(slack_msgs)

    @staticmethod
    def _collect_people_to_ping(pr):
        pr_author = SlackHandler.get_slack_name_of(pr.author_name)

        merge_status_resolver = PRIsMergeableResolver(pr.pr_id)
        if merge_status_resolver.is_conflicted:
            return {"{} (merge CONFLICT)".format(pr_author)}

        if merge_status_resolver.builds_have_failed:
            return {"{} (builds FAILED)".format(pr_author)}

        authors_unfinished_work = set()
        people_to_ping = set()

        merge_vetos = merge_status_resolver.merge_vetos()
        if merge_vetos:
            authors_unfinished_work.update(merge_vetos)

        undone_tasks = PRFetcher.fetch_open_tasks(pr.pr_id)
        if undone_tasks:
            authors_unfinished_work.add(
                "open tasks: {}".format(" & ".join(undone_tasks))
            )

        people_to_ping.update(pr.get_undone_reviewers())

        if authors_unfinished_work:
            people_to_ping.add(
                "{} ({})".format(pr_author, "; ".join(authors_unfinished_work))
            )

        if people_to_ping:
            return people_to_ping

        return {pr_author}


if __name__ == "__main__":

    parser = argparse.ArgumentParser(
        usage="Fetch OPEN PRs from Bitbucket, compose a reminder message "
        "for PR reviewers and send it to Slack."
    )
    parser.add_argument(
        "--pr", "-p", type=int, help="PR ID. Generate a message for this PR only"
    )
    parser.add_argument(
        "--users",
        "-u",
        nargs="+",
        help="Usernames of users, whose open PRs we will process",
    )
    parser.add_argument(
        "--limit",
        "-l",
        type=int,
        default=1000,
        help="Limit to only this many PRs. Default: 1000.",
    )

    arguments = parser.parse_args()

    PRReminder.run(limit=arguments.limit, pr_id=arguments.pr, users=arguments.users)
