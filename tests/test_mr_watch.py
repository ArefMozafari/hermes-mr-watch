"""Offline tests: fake glab/gh API responses, real tick() logic.

Run: python3 -m unittest discover -s tests -v
"""
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/mr-watch/scripts/mr_watch.py"


def load(tmp):
    os.environ["MR_WATCH_DIR"] = tmp
    spec = importlib.util.spec_from_file_location("mr_watch", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def ts(minutes_ago=0):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeGitLab:
    def __init__(self):
        self.mine = [{"iid": 7, "title": "Add chat banner", "source_branch": "feature/PROJ-12-banner"}]
        self.reviewing = []
        self.events = [{"id": 100}]
        self.discussions = {}
        self.fail = False

    def api(self, path):
        if self.fail:
            raise RuntimeError("timeout after 25s: api projects/x")
        if "author_username" in path:
            return self.mine
        if "reviewer_username" in path:
            return self.reviewing
        if "/discussions" in path:
            return self.discussions.get(int(path.split("merge_requests/")[1].split("/")[0]), [])
        if "/events" in path:
            if "per_page=1" in path and "page=" not in path:
                return self.events[:1]
            page = int(path.rsplit("page=", 1)[1])
            return sorted(self.events, key=lambda e: -e["id"])[(page - 1) * 100: page * 100]
        if "/merge_requests/" in path:
            iid = int(path.rsplit("/", 1)[1])
            return {"iid": iid, "title": f"MR {iid}", "source_branch": "x"}
        raise AssertionError(path)


class FakeGitHub:
    def __init__(self):
        self.rr, self.active = [], []
        self.issue_comments, self.review_comments, self.reviews, self.pulls = {}, {}, {}, {}
        self.fail = False

    @staticmethod
    def item(repo, num, title, author="me", updated=None):
        return {"repository_url": f"https://api.github.com/repos/{repo}", "number": num, "title": title,
                "html_url": f"https://github.com/{repo}/pull/{num}", "user": {"login": author},
                "body": "", "updated_at": updated or ts()}

    def api(self, path, params=None):
        if self.fail:
            raise RuntimeError("timeout after 25s: api -X GET")
        if path == "search/issues":
            return {"items": self.rr if "review-requested:" in params["q"] else self.active}
        key = path.split("/repos/")[-1] if "/repos/" in path else path
        repo_num = "/".join(path.split("/")[1:3]) + "#" + path.split("/")[4]
        if path.endswith("/comments") and "/issues/" in path:
            return self.issue_comments.get(repo_num, [])
        if path.endswith("/comments"):
            return self.review_comments.get(repo_num, [])
        if path.endswith("/reviews"):
            return self.reviews.get(repo_num, [])
        if "/pulls/" in path:
            return self.pulls.get(repo_num, {"state": "open", "merged": False})
        if "/issues/" in path:
            return {"closed_by": {"login": "boss"}}
        raise AssertionError(key)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.m = load(self.tmp)
        self.t = self.m.STRINGS["en"]
        self.jira = self.m.Jira({"url": "https://j.example/browse/{key}", "pattern": r"PROJ-\d+",
                                 "ignore": ["PROJ-0000"]})

    def gitlab(self, fake, projects=("team/app",), vpn=None):
        cfg = {"host": "gitlab.example.com", "username": "me", "projects": list(projects), "_test": True}
        if vpn:
            cfg["vpn"] = vpn
        gl = self.m.GitLab(cfg, self.t, self.jira)
        gl.api = fake.api
        return gl, cfg

    def github(self, fake):
        cfg = {"username": "me", "_test": True}
        gh = self.m.GitHub(cfg, self.t, self.jira)
        gh.api = fake.api
        return gh, cfg

    def tick(self, cfg, gl=None, gh=None):
        out, err = io.StringIO(), io.StringIO()
        full = {**self.m.DEFAULTS, **cfg}
        with redirect_stdout(out), redirect_stderr(err):
            self.m.tick(full, (self.t, gl, gh))
        return out.getvalue().strip()

    def state(self):
        return json.loads((Path(self.tmp) / "state.json").read_text())


class GitLabTests(Base):
    def test_baseline_is_silent_then_reports_comment_with_jira(self):
        f = FakeGitLab()
        gl, c = self.gitlab(f)
        self.assertEqual(self.tick({"gitlab": c}, gl), "")
        f.events = [{"id": 101, "author_username": "ali", "author": {"name": "Ali", "username": "ali"},
                     "target_title": "Add chat banner",
                     "note": {"id": 55, "noteable_type": "MergeRequest", "noteable_iid": 7, "body": "nit: rename",
                              "position": {"new_path": "lib/chat/banner.dart"}}}] + f.events
        out = self.tick({"gitlab": c}, gl)
        self.assertIn("Comment on your MR", out)
        self.assertIn("[Add chat banner](https://gitlab.example.com/team/app/-/merge_requests/7#note_55)", out)
        self.assertIn("[PROJ-12](https://j.example/browse/PROJ-12)", out)
        self.assertIn("📄 banner.dart", out)
        self.assertEqual(self.tick({"gitlab": c}, gl), "")          # not repeated

    def test_own_and_bot_events_ignored_and_approval_reported(self):
        f = FakeGitLab()
        gl, c = self.gitlab(f)
        self.tick({"gitlab": c}, gl)
        f.events = [
            {"id": 103, "author_username": "boss", "author": {"name": "Boss"}, "action_name": "approved",
             "target_type": "MergeRequest", "target_iid": 7, "target_title": "Add chat banner"},
            {"id": 102, "author_username": "project_9_bot", "action_name": "approved",
             "target_type": "MergeRequest", "target_iid": 7},
            {"id": 101, "author_username": "me", "action_name": "approved",
             "target_type": "MergeRequest", "target_iid": 7},
        ] + f.events
        out = self.tick({"gitlab": c}, gl)
        self.assertEqual(out.count("✅ **Approved**"), 1)
        self.assertIn("👤 Boss", out)

    def test_review_request_and_thread_reply(self):
        f = FakeGitLab()
        gl, c = self.gitlab(f)
        self.tick({"gitlab": c}, gl)
        f.reviewing = [{"iid": 9, "title": "Refactor login", "source_branch": "PROJ-0000-x"}]
        f.discussions = {9: [{"notes": [{"id": 1, "author": {"username": "me"}},
                                        {"id": 2, "author": {"username": "sara"}}]}]}
        f.events = [{"id": 101, "author_username": "sara", "author": {"name": "Sara"},
                     "note": {"id": 2, "noteable_type": "MergeRequest", "noteable_iid": 9, "body": "done"}}] + f.events
        out = self.tick({"gitlab": c}, gl)
        self.assertIn("Review requested", out)
        self.assertIn("Reply in your thread", out)
        self.assertNotIn("PROJ-0000", out)                              # placeholder ignored

    def test_failure_keeps_cursor_and_alerts_once_after_threshold(self):
        f = FakeGitLab()
        gl, c = self.gitlab(f)
        self.tick({"gitlab": c}, gl)
        cursor = self.state()["gitlab"]["team/app"]["last_event_id"]
        f.fail = True
        outs = [self.tick({"gitlab": c}, gl) for _ in range(6)]
        self.assertEqual([bool(o) for o in outs], [False, False, False, False, True, False])
        self.assertIn("GitLab unreachable 5 times", outs[4])
        self.assertEqual(self.state()["gitlab"]["team/app"]["last_event_id"], cursor)
        f.fail = False
        self.assertIn("GitLab reachable again", self.tick({"gitlab": c}, gl))
        self.assertEqual(self.tick({"gitlab": c}, gl), "")

    def test_vpn_gate(self):
        f = FakeGitLab()
        gl, c = self.gitlab(f, vpn={"name": "Corp"})
        os.environ["MR_WATCH_FAKE_VPN"] = "up"
        self.tick({"gitlab": c}, gl)
        os.environ["MR_WATCH_FAKE_VPN"] = "down"
        f.fail = True                                    # must not even be called
        outs = [self.tick({"gitlab": c}, gl) for _ in range(3)]
        self.assertEqual(outs[0], "")
        self.assertIn("VPN Corp is disconnected", outs[1])
        self.assertEqual(outs[2], "")
        self.assertEqual(self.state()["health"].get("gitlab", {}).get("count", 0), 0)
        os.environ["MR_WATCH_FAKE_VPN"] = "up"
        f.fail = False
        self.assertIn("VPN Corp is connected again", self.tick({"gitlab": c}, gl))
        del os.environ["MR_WATCH_FAKE_VPN"]

    def test_multi_project_refs(self):
        f = FakeGitLab()
        gl, c = self.gitlab(f, projects=("team/app", "team/api"))
        self.tick({"gitlab": c}, gl)
        f.events = [{"id": 101, "author_username": "boss", "author": {"name": "Boss"}, "action_name": "accepted",
                     "target_type": "MergeRequest", "target_iid": 7}] + f.events
        out = self.tick({"gitlab": c}, gl)
        self.assertIn("🔢 [team/app!7]", out)
        self.assertIn("🔢 [team/api!7]", out)


class GitHubTests(Base):
    def test_baseline_then_comment_review_merge(self):
        f = FakeGitHub()
        gh, c = self.github(f)
        self.assertEqual(self.tick({"github": c}, gh=gh), "")
        pr = f.item("acme/app", 3, "Fix PROJ-12 crash")
        f.active = [pr]
        f.issue_comments["acme/app#3"] = [
            {"id": 1, "user": {"login": "ali"}, "body": "LGTM?", "created_at": ts(), "html_url": "u1"},
            {"id": 2, "user": {"login": "me"}, "body": "mine", "created_at": ts(), "html_url": "u2"},
            {"id": 3, "user": {"login": "dependabot[bot]"}, "body": "bump", "created_at": ts(), "html_url": "u3"},
            {"id": 4, "user": {"login": "old"}, "body": "old", "created_at": ts(600), "html_url": "u4"},
        ]
        f.reviews["acme/app#3"] = [
            {"id": 9, "user": {"login": "boss"}, "state": "CHANGES_REQUESTED", "body": "pls",
             "submitted_at": ts(), "html_url": "r9"},
            {"id": 10, "user": {"login": "boss"}, "state": "COMMENTED", "body": "",
             "submitted_at": ts(), "html_url": "r10"},
        ]
        out = self.tick({"github": c}, gh=gh)
        self.assertEqual(out.count("Comment on your PR"), 1)
        self.assertIn("👤 ali", out)
        self.assertIn("Changes requested", out)
        self.assertNotIn("Reviewed", out)
        self.assertIn("🔢 [acme/app#3](https://github.com/acme/app/pull/3) · 🎫 [PROJ-12]", out)
        self.assertEqual(self.tick({"github": c}, gh=gh), "")            # dedup inside overlap window
        f.pulls["acme/app#3"] = {"state": "closed", "merged": True, "merged_by": {"login": "boss"},
                                 "merged_at": ts()}
        out = self.tick({"github": c}, gh=gh)
        self.assertIn("🟣 **Merged**", out)
        self.assertEqual(self.tick({"github": c}, gh=gh), "")

    def test_old_merged_pr_first_seen_is_silent(self):
        f = FakeGitHub()
        gh, c = self.github(f)
        self.tick({"github": c}, gh=gh)
        f.active = [f.item("acme/app", 4, "Old")]
        f.pulls["acme/app#4"] = {"state": "closed", "merged": True, "merged_by": {"login": "boss"},
                                 "merged_at": ts(60 * 24)}
        self.assertEqual(self.tick({"github": c}, gh=gh), "")

    def test_review_request_mention_and_thread_reply(self):
        f = FakeGitHub()
        gh, c = self.github(f)
        self.tick({"github": c}, gh=gh)
        other = f.item("acme/web", 8, "Theme", author="sara")
        f.rr = [other]
        f.active = [other]
        f.issue_comments["acme/web#8"] = [
            {"id": 5, "user": {"login": "sara"}, "body": "@me thoughts?", "created_at": ts(), "html_url": "c5"}]
        f.review_comments["acme/web#8"] = [
            {"id": 20, "user": {"login": "me"}, "body": "why?", "created_at": ts(5), "html_url": "x", "path": "a/b.ts"},
            {"id": 21, "user": {"login": "sara"}, "body": "because", "created_at": ts(), "html_url": "c21",
             "in_reply_to_id": 20, "path": "a/b.ts"}]
        out = self.tick({"github": c}, gh=gh)
        self.assertIn("Review requested", out)
        self.assertIn("You were mentioned", out)
        self.assertIn("Reply in your thread", out)
        self.assertIn("📄 b.ts", out)

    def test_failure_keeps_window(self):
        f = FakeGitHub()
        gh, c = self.github(f)
        self.tick({"github": c}, gh=gh)
        since = self.state()["github"]["since"]
        f.fail = True
        self.tick({"github": c}, gh=gh)
        self.assertEqual(self.state()["github"]["since"], since)
        self.assertEqual(self.state()["health"]["github"]["count"], 1)


class MiscTests(Base):
    def test_fa_strings_and_init(self):
        self.t = self.m.STRINGS["fa"]
        f = FakeGitLab()
        gl, c = self.gitlab(f)
        self.tick({"gitlab": c}, gl)
        f.events = [{"id": 101, "author_username": "boss", "author": {"name": "Boss"}, "action_name": "accepted",
                     "target_type": "MergeRequest", "target_iid": 7}] + f.events
        self.assertIn("مرج شد", self.tick({"gitlab": c}, gl))
        with redirect_stdout(io.StringIO()):
            self.m.main(["init", "--gitlab-host", "g.example", "--gitlab-user", "me", "--gitlab-project", "a/b",
                         "--vpn-name", "Corp", "--github-user", "me", "--jira-url", "https://j/{key}",
                         "--jira-pattern", r"X-\d+"])
        cfg = json.loads((Path(self.tmp) / "config.json").read_text())
        self.assertEqual(cfg["gitlab"]["vpn"]["name"], "Corp")
        self.assertEqual(cfg["github"]["repos"], [])

    def test_strings_have_same_keys(self):
        self.assertEqual(set(self.m.STRINGS["en"]), set(self.m.STRINGS["fa"]))


if __name__ == "__main__":
    unittest.main()
