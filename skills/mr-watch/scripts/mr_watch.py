#!/usr/bin/env python3
"""mr-watch: GitLab merge-request + GitHub pull-request watcher for Hermes cron.

Runs as a Hermes `--no-agent` cron job (no LLM, no tokens). Each tick it prints
only the events that concern YOU; empty stdout means a silent tick.

  GitLab (via `glab`)            GitHub (via `gh`)
  - review requested             - review requested
  - comments on your MRs         - comments / reviews on your PRs
  - approvals, merges, closes    - approvals, changes requested, merges, closes
  - replies in your threads      - replies in your review threads
  - @-mentions                   - @-mentions

Optional VPN gate (GitLab only): when the tunnel that reaches your GitLab is
down, it tells you to connect it instead of failing with timeouts, and skips
GitLab until it is back. Nothing is lost: polling resumes where it stopped.

Usage:
  mr_watch.py                 one tick (what the cron job runs)
  mr_watch.py init [...]      write the config (see --help)
  mr_watch.py check           validate config, CLIs, auth and VPN
  mr_watch.py --version

Files (override the directory with MR_WATCH_DIR):
  $HERMES_HOME/mr-watch/config.json   your settings
  $HERMES_HOME/mr-watch/state.json    cursors + health counters

Standard library only; Python 3.9+.
"""
import argparse
import copy
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

VERSION = "1.0.0"

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
DIR = Path(os.environ.get("MR_WATCH_DIR", HERMES_HOME / "mr-watch"))
CONFIG = DIR / "config.json"
STATE = DIR / "state.json"

SEP = "━━━━━━━━━━━━━━━"
CALL_TIMEOUT = 25          # seconds per glab/gh call; a 1-minute tick must not stall
GITLAB_EVENT_PAGES = 10
GITHUB_OVERLAP = timedelta(minutes=10)   # search-index lag cushion; dupes are filtered by id
SEEN_CAP = 5000
DEFAULTS = {"fail_alert_after": 5, "vpn_alert_after": 2}
BOT_RE = re.compile(r"(\[bot\]$|^project_\d+_bot|^group_\d+_bot|-bot$)", re.I)

STRINGS = {
    "en": {
        "review_req": "👀 **Review requested**",
        "comment_mine": "💬 **Comment on your {kind}**",
        "mention": "📣 **You were mentioned**",
        "reply": "↩️ **Reply in your thread**",
        "approved": "✅ **Approved**",
        "changes": "🔴 **Changes requested**",
        "reviewed": "📝 **Reviewed**",
        "merged": "🟣 **Merged**",
        "closed": "⛔ **Closed**",
        "reopened": "🔄 **Reopened**",
        "vpn_down": "🔌 **VPN {name} is disconnected — connect it**\n"
                    "GitLab can't be reached until it's back. Everything that happens "
                    "meanwhile is sent once it reconnects.",
        "vpn_up": "✅ **VPN {name} is connected again**",
        "src_down": "⚠️ **{src} unreachable {n} times in a row**\n{reason}",
        "src_up": "✅ **{src} reachable again**",
        "code": "[code]",
    },
    "fa": {
        "review_req": "👀 **درخواست ریویو**",
        "comment_mine": "💬 **کامنت روی {kind} تو**",
        "mention": "📣 **منشن شدی**",
        "reply": "↩️ **جواب به ریویوی تو**",
        "approved": "✅ **اپرو شد**",
        "changes": "🔴 **تغییرات خواستن**",
        "reviewed": "📝 **ریویو شد**",
        "merged": "🟣 **مرج شد**",
        "closed": "⛔ **بسته شد**",
        "reopened": "🔄 **دوباره باز شد**",
        "vpn_down": "🔌 **وی\u200cپی\u200cان {name} قطعه، وصلش کن**\n"
                    "تا وصل نشه گیت\u200cلب چک نمی\u200cشه. بعد از وصل شدن، همه\u200cی رویدادهای این مدت رو می\u200cفرستم.",
        "vpn_up": "✅ **وی\u200cپی\u200cان {name} وصل شد**",
        "src_down": "⚠️ **{src} {n} بار پشت سر هم در دسترس نبود**\n{reason}",
        "src_up": "✅ **{src} دوباره در دسترسه**",
        "code": "[کد]",
    },
}


# ── helpers ──────────────────────────────────────────────────────────────────

def find_bin(name, override=None):
    """Cron runs with a minimal PATH, so look in the usual install dirs too."""
    for p in (override, shutil.which(name), f"/opt/homebrew/bin/{name}",
              f"/usr/local/bin/{name}", f"/usr/bin/{name}",
              str(Path.home() / ".local/bin" / name)):
        if p and os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    raise RuntimeError(f"`{name}` not found — install it or set {name}_path in the config")


def run(cmd, env=None, timeout=CALL_TIMEOUT):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"timeout after {timeout}s: {' '.join(cmd[1:4])}")
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[1:4])}: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout


def clean(text, code_tag="[code]", limit=280):
    """Plain, Telegram-safe excerpt: no code blocks or markdown control chars."""
    text = re.sub(r"```.*?```", f" {code_tag} ", text or "", flags=re.S)
    text = re.sub(r"[`*_\[\]~>|#]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def link_text(text):
    return re.sub(r"[\[\]]", "", text or "").strip()


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s):
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    tmp.replace(path)


class Jira:
    """Optional: turn ticket keys found in branch/title/description into links."""

    def __init__(self, cfg):
        cfg = cfg or {}
        self.url = cfg.get("url")
        self.re = re.compile(rf"\b({cfg['pattern']})\b") if cfg.get("pattern") else None
        self.ignore = set(cfg.get("ignore", []))

    def key(self, *texts):
        if not (self.url and self.re):
            return None
        for text in texts:
            for hit in self.re.finditer(text or ""):
                if hit.group(1) not in self.ignore:
                    return hit.group(1)
        return None

    def suffix(self, key):
        return f" · 🎫 [{key}]({self.url.format(key=key)})" if key else ""


def card(label, title, url, ref, ref_url, jira_suffix="", who=None, where=None, body=None, code="[code]"):
    lines = [label, f"[{link_text(title) or ref}]({url})", f"🔢 [{ref}]({ref_url}){jira_suffix}"]
    if who:
        lines.append(f"👤 {who}")
    if where:
        lines.append(f"📄 {where}")
    if body and clean(body, code):
        lines.append(clean(body, code))
    return "\n".join(lines)


# ── GitLab ───────────────────────────────────────────────────────────────────

class GitLab:
    key = "gitlab"
    name = "GitLab"

    def __init__(self, cfg, t, jira):
        self.host = cfg["host"]
        self.me = cfg["username"]
        self.projects = cfg["projects"]
        self.ignore = {u.lower() for u in cfg.get("ignore_users", [])} | {self.me.lower()}
        self.bin = None if cfg.get("_test") else find_bin("glab", cfg.get("glab_path"))
        self.t, self.jira = t, jira

    def api(self, path):
        return json.loads(run([self.bin, "api", path, "--hostname", self.host]))

    def skip_user(self, user):
        return not user or user.lower() in self.ignore or bool(BOT_RE.search(user))

    def poll(self, state):
        msgs, errors = [], []
        for proj in self.projects:
            ps = state.setdefault(proj, {})
            work = copy.deepcopy(ps)
            try:
                found = self.poll_project(proj, work)
            except Exception as exc:          # keep the old cursor: next tick retries
                errors.append(f"{proj}: {exc}")
                continue
            ps.clear()
            ps.update(work)
            msgs += found
        return msgs, "; ".join(errors) or None

    def poll_project(self, proj, ps):
        pid = urllib.parse.quote(proj, safe="")
        mr_url = f"https://{self.host}/{proj}/-/merge_requests/{{iid}}"
        multi = len(self.projects) > 1
        cache = {}

        my_mrs = {m["iid"]: m for m in self.api(
            f"projects/{pid}/merge_requests?author_username={self.me}&state=all&per_page=100&order_by=updated_at")}
        reviewing = {m["iid"]: m for m in self.api(
            f"projects/{pid}/merge_requests?reviewer_username={self.me}&state=opened&per_page=100")}
        cache.update(my_mrs)
        cache.update(reviewing)

        if "last_event_id" not in ps:      # first run: baseline only, stay silent
            head = self.api(f"projects/{pid}/events?per_page=1")
            ps["last_event_id"] = head[0]["id"] if head else 0
            ps["reviewing"] = sorted(reviewing)
            return []

        def mr(iid):
            if iid not in cache:
                try:
                    cache[iid] = self.api(f"projects/{pid}/merge_requests/{iid}")
                except Exception:
                    cache[iid] = {}
            return cache[iid]

        def make(label, iid, title=None, anchor="", **kw):
            m = mr(iid)
            url = mr_url.format(iid=iid)
            ref = f"{proj}!{iid}" if multi else f"!{iid}"
            jk = self.jira.key(m.get("source_branch"), m.get("title"), m.get("description"))
            return card(label, title or m.get("title"), url + anchor, ref, url,
                        self.jira.suffix(jk), code=self.t["code"], **kw)

        msgs = []
        seen_reviewing = set(ps.get("reviewing", []))
        for iid in sorted(reviewing):
            if iid not in seen_reviewing:
                msgs.append(make(self.t["review_req"], iid))

        events = []
        for page in range(1, GITLAB_EVENT_PAGES + 1):
            batch = self.api(f"projects/{pid}/events?per_page=100&page={page}")
            if not batch:
                break
            fresh = [e for e in batch if e["id"] > ps["last_event_id"]]
            events += fresh
            if len(fresh) < len(batch):
                break
        events.sort(key=lambda e: e["id"])

        discussions = {}
        for e in events:
            author = e.get("author") or {}
            who = e.get("author_username") or author.get("username")
            if self.skip_user(who):
                continue
            name = author.get("name") or who
            note = e.get("note")
            if note and note.get("noteable_type") == "MergeRequest" and not note.get("system"):
                iid = note.get("noteable_iid")
                body = note.get("body", "")
                label = None
                if iid in my_mrs:
                    label = self.t["comment_mine"].format(kind="MR")
                elif re.search(rf"@{re.escape(self.me)}\b", body, re.I):
                    label = self.t["mention"]
                else:
                    if iid not in discussions:
                        try:
                            discussions[iid] = self.api(
                                f"projects/{pid}/merge_requests/{iid}/discussions?per_page=100")
                        except Exception:
                            discussions[iid] = []
                    for d in discussions[iid]:
                        notes = d.get("notes", [])
                        if any(n["id"] == note["id"] for n in notes):
                            if any(n["author"]["username"].lower() == self.me.lower() and n["id"] < note["id"]
                                   for n in notes):
                                label = self.t["reply"]
                            break
                if label:
                    path = (note.get("position") or {}).get("new_path")
                    msgs.append(make(label, iid, e.get("target_title"), f"#note_{note['id']}",
                                     who=name, where=path.split("/")[-1] if path else None, body=body))
                continue
            if e.get("target_type") == "MergeRequest" and e.get("target_iid") in my_mrs:
                label = {"approved": self.t["approved"], "accepted": self.t["merged"],
                         "closed": self.t["closed"], "reopened": self.t["reopened"]}.get(e.get("action_name"))
                if label:
                    msgs.append(make(label, e["target_iid"], e.get("target_title"), who=name))

        if events:
            ps["last_event_id"] = events[-1]["id"]
        ps["reviewing"] = sorted(reviewing)
        return msgs

    def check(self):
        user = self.api("user")
        out = [f"GitLab {self.host}: logged in as {user.get('username')}"]
        if user.get("username", "").lower() != self.me.lower():
            raise RuntimeError(f"glab is logged in as {user.get('username')}, config says {self.me}")
        for proj in self.projects:
            p = self.api(f"projects/{urllib.parse.quote(proj, safe='')}")
            out.append(f"  project {p.get('path_with_namespace')}: ok")
        return out


# ── GitHub ───────────────────────────────────────────────────────────────────

class GitHub:
    key = "github"
    name = "GitHub"

    def __init__(self, cfg, t, jira):
        self.me = cfg["username"]
        self.repos = cfg.get("repos") or []
        self.ignore = {u.lower() for u in cfg.get("ignore_users", [])} | {self.me.lower()}
        self.t, self.jira = t, jira
        self.env = None
        self.bin = None
        if not cfg.get("_test"):
            self.bin = find_bin("gh", cfg.get("gh_path"))
            if cfg.get("gh_account"):     # use one gh account without switching the active one
                token = run([self.bin, "auth", "token", "-u", cfg["gh_account"]]).strip()
                os.putenv("GH_TOKEN", token)   # inherited by gh child processes only

    def api(self, path, params=None):
        cmd = [self.bin, "api", "-X", "GET", path]
        for k, v in (params or {}).items():
            cmd += ["-f", f"{k}={v}"]
        return json.loads(run(cmd, env=self.env))

    def search(self, q):
        scope = " ".join(f"repo:{r}" for r in self.repos)
        return self.api("search/issues", {"q": f"is:pr archived:false {q} {scope}".strip(),
                                          "per_page": 100, "sort": "updated"}).get("items", [])

    def skip_user(self, user):
        return not user or user.lower() in self.ignore or bool(BOT_RE.search(user))

    @staticmethod
    def pr_key(item):
        return f"{item['repository_url'].split('/repos/', 1)[1]}#{item['number']}"

    def poll(self, state):
        work = copy.deepcopy(state)
        try:
            msgs = self._poll(work)
        except Exception as exc:
            return [], str(exc)
        state.clear()
        state.update(work)
        return msgs, None

    def _poll(self, st):
        started = utcnow()
        me = self.me.lower()
        review_req = {self.pr_key(i): i for i in self.search(f"is:open review-requested:{self.me}")}

        if "since" not in st:              # first run: baseline only, stay silent
            st.update(baseline=iso(started), since=iso(started),
                      review_requested=sorted(review_req), seen=[], pr_state={})
            return []

        lower = max(parse_ts(st["baseline"]), parse_ts(st["since"]) - GITHUB_OVERLAP)
        lower_s = iso(lower)
        seen = set(st.get("seen", []))
        new_seen = []
        msgs = []

        def make(label, item, url=None, **kw):
            key = self.pr_key(item)
            jk = self.jira.key(item.get("title"), item.get("body"))
            return card(label, item.get("title"), url or item["html_url"], key, item["html_url"],
                        self.jira.suffix(jk), code=self.t["code"], **kw)

        old_rr = set(st.get("review_requested", []))
        for key, item in sorted(review_req.items()):
            if key not in old_rr:
                msgs.append(make(self.t["review_req"], item))

        active = {}
        for q in (f"involves:{self.me} updated:>={lower_s}", f"reviewed-by:{self.me} updated:>={lower_s}"):
            for item in self.search(q):
                active[self.pr_key(item)] = item

        for key, item in sorted(active.items(), key=lambda kv: kv[1].get("updated_at", "")):
            repo, num = key.rsplit("#", 1)
            mine = (item.get("user") or {}).get("login", "").lower() == me
            new = lambda ts: (parse_ts(ts) or lower) >= lower

            def fresh(kind, obj):
                tag = f"{kind}:{obj['id']}"
                if tag in seen:
                    return False
                seen.add(tag)
                new_seen.append(tag)
                return True

            for c in self.api(f"repos/{repo}/issues/{num}/comments", {"since": lower_s, "per_page": 100}):
                who = (c.get("user") or {}).get("login")
                if self.skip_user(who) or not new(c.get("created_at")) or not fresh("ic", c):
                    continue
                body = c.get("body", "")
                if mine:
                    msgs.append(make(self.t["comment_mine"].format(kind="PR"), item, c.get("html_url"),
                                     who=who, body=body))
                elif re.search(rf"@{re.escape(self.me)}\b", body, re.I):
                    msgs.append(make(self.t["mention"], item, c.get("html_url"), who=who, body=body))

            rcs = self.api(f"repos/{repo}/pulls/{num}/comments", {"per_page": 100})
            for c in rcs:
                who = (c.get("user") or {}).get("login")
                if self.skip_user(who) or not new(c.get("created_at")) or not fresh("rc", c):
                    continue
                body = c.get("body", "")
                root = c.get("in_reply_to_id") or c["id"]
                in_my_thread = any((o.get("user") or {}).get("login", "").lower() == me
                                   and (o.get("in_reply_to_id") or o["id"]) == root and o["id"] < c["id"]
                                   for o in rcs)
                label = (self.t["comment_mine"].format(kind="PR") if mine
                         else self.t["mention"] if re.search(rf"@{re.escape(self.me)}\b", body, re.I)
                         else self.t["reply"] if in_my_thread else None)
                if label:
                    path = c.get("path")
                    msgs.append(make(label, item, c.get("html_url"), who=who,
                                     where=path.split("/")[-1] if path else None, body=body))

            if mine:
                for r in self.api(f"repos/{repo}/pulls/{num}/reviews", {"per_page": 100}):
                    who = (r.get("user") or {}).get("login")
                    if self.skip_user(who) or not new(r.get("submitted_at")):
                        continue
                    state, body = r.get("state"), r.get("body") or ""
                    label = {"APPROVED": self.t["approved"], "CHANGES_REQUESTED": self.t["changes"]}.get(state)
                    if not label and state == "COMMENTED" and body.strip():
                        label = self.t["reviewed"]   # inline-only reviews are reported per comment
                    if label and fresh("rv", r):
                        msgs.append(make(label, item, r.get("html_url"), who=who, body=body))

                pull = self.api(f"repos/{repo}/pulls/{num}")
                cur = "merged" if pull.get("merged") else pull.get("state")
                prev = st["pr_state"].get(key)
                st["pr_state"][key] = cur
                changed_at = pull.get("merged_at") if cur == "merged" else pull.get("closed_at")
                if prev is None and cur in ("merged", "closed") and changed_at and parse_ts(changed_at) < lower:
                    continue                  # first sight of an old state: just record it
                if cur != prev and (prev is not None or cur in ("merged", "closed")):
                    if cur == "merged":
                        who = (pull.get("merged_by") or {}).get("login")
                        if not self.skip_user(who):
                            msgs.append(make(self.t["merged"], item, who=who))
                    elif cur == "closed":
                        who = (self.api(f"repos/{repo}/issues/{num}").get("closed_by") or {}).get("login")
                        if not self.skip_user(who):
                            msgs.append(make(self.t["closed"], item, who=who))
                    elif cur == "open" and prev in ("closed", "merged"):
                        msgs.append(make(self.t["reopened"], item))

        st["since"] = iso(started)
        st["review_requested"] = sorted(review_req)
        st["seen"] = (st.get("seen", []) + new_seen)[-SEEN_CAP:]
        return msgs

    def check(self):
        user = self.api("user")
        login = user.get("login", "")
        if login.lower() != self.me.lower():
            raise RuntimeError(f"gh token belongs to {login}, config says {self.me} (set gh_account)")
        out = [f"GitHub: authenticated as {login}"]
        for r in self.repos:
            out.append(f"  repo {self.api(f'repos/{r}').get('full_name')}: ok")
        if not self.repos:
            out.append("  repos: all repositories you are involved in")
        return out


# ── VPN gate ─────────────────────────────────────────────────────────────────

def route_iface(ip):
    if sys.platform == "darwin":
        out = run(["/sbin/route", "-n", "get", ip], timeout=10)
        m = re.search(r"interface:\s*(\S+)", out)
    else:
        out = run([shutil.which("ip") or "/sbin/ip", "route", "get", ip], timeout=10)
        m = re.search(r"\bdev\s+(\S+)", out)
    return m.group(1) if m else None


def vpn_up(vpn, host):
    """True when traffic to the GitLab host leaves through a tunnel.

    Default (split-tunnel VPNs such as most company OpenVPN profiles in
    Tunnelblick / Pritunl / OpenVPN Connect): the route to GitLab must use a
    different interface than the route to the public internet. Set
    `interface_prefixes` (e.g. ["utun"]) when the VPN carries all traffic.
    """
    fake = os.getenv("MR_WATCH_FAKE_VPN")
    if fake in ("up", "down"):
        return fake == "up"
    ip = vpn.get("check_ip")
    if not ip:
        try:
            ip = socket.gethostbyname(host)
        except OSError:
            return False
    iface = route_iface(ip)
    if not iface:
        return False
    prefixes = vpn.get("interface_prefixes")
    if prefixes:
        return iface.startswith(tuple(prefixes))
    return iface != route_iface(vpn.get("public_ip", "1.1.1.1"))


# ── main ─────────────────────────────────────────────────────────────────────

def load_config():
    cfg = load_json(CONFIG)
    if not cfg:
        raise SystemExit(f"mr-watch: no config at {CONFIG} — run `mr_watch.py init` first")
    return {**DEFAULTS, **cfg}


def build_sources(cfg):
    t = STRINGS.get(cfg.get("lang", "en"), STRINGS["en"])
    jira = Jira(cfg.get("jira"))
    gl = GitLab(cfg["gitlab"], t, jira) if (cfg.get("gitlab") or {}).get("enabled", True) and cfg.get("gitlab") else None
    gh = GitHub(cfg["github"], t, jira) if (cfg.get("github") or {}).get("enabled", True) and cfg.get("github") else None
    return t, gl, gh


def tick(cfg=None, sources=None):
    cfg = cfg or load_config()
    t, gl, gh = sources or build_sources(cfg)
    state = load_json(STATE, {}) or {}
    health = state.setdefault("health", {})
    out = []

    pollers = []
    if gl:
        vpn = cfg["gitlab"].get("vpn") or {}
        if vpn.get("name"):
            vs = state.setdefault("vpn", {"down": 0, "alerted": False})
            if vpn_up(vpn, gl.host):
                if vs.get("alerted"):
                    out.append(t["vpn_up"].format(name=vpn["name"]))
                vs.update(down=0, alerted=False)
                pollers.append(gl)
            else:
                vs["down"] = vs.get("down", 0) + 1
                if vs["down"] >= cfg["vpn_alert_after"] and not vs.get("alerted"):
                    vs["alerted"] = True
                    out.append(t["vpn_down"].format(name=vpn["name"]))
        else:
            pollers.append(gl)
    if gh:
        pollers.append(gh)

    for src in pollers:
        hs = health.setdefault(src.key, {"count": 0, "alerted": False})
        msgs, err = src.poll(state.setdefault(src.key, {}))
        out += msgs
        if err:
            hs["count"] += 1
            hs["last_error"] = err[:300]
            print(f"mr-watch {src.name} error ({hs['count']}): {err}", file=sys.stderr)
            if hs["count"] >= cfg["fail_alert_after"] and not hs["alerted"]:
                hs["alerted"] = True
                reason = "timeout" if "timeout" in err else clean(err, limit=200)
                out.append(t["src_down"].format(src=src.name, n=hs["count"], reason=reason))
        else:
            if hs.get("alerted"):
                out.append(t["src_up"].format(src=src.name))
            health[src.key] = {"count": 0, "alerted": False}

    save_json(STATE, state)
    if out:
        print(f"\n{SEP}\n".join(out))


def cmd_init(a):
    if CONFIG.exists() and not a.force:
        raise SystemExit(f"{CONFIG} exists — pass --force to overwrite")
    cfg = {"lang": a.lang, **DEFAULTS}
    if a.gitlab_host:
        if not (a.gitlab_user and a.gitlab_project):
            raise SystemExit("GitLab needs --gitlab-user and at least one --gitlab-project")
        cfg["gitlab"] = {"host": a.gitlab_host, "username": a.gitlab_user, "projects": a.gitlab_project}
        if a.vpn_name:
            cfg["gitlab"]["vpn"] = {"name": a.vpn_name}
            if a.vpn_interface_prefix:
                cfg["gitlab"]["vpn"]["interface_prefixes"] = a.vpn_interface_prefix
    if a.github_user:
        cfg["github"] = {"username": a.github_user, "repos": a.github_repo or []}
        if a.github_account:
            cfg["github"]["gh_account"] = a.github_account
    if a.jira_url:
        if "{key}" not in a.jira_url or not a.jira_pattern:
            raise SystemExit("--jira-url must contain {key} and needs --jira-pattern")
        cfg["jira"] = {"url": a.jira_url, "pattern": a.jira_pattern, "ignore": a.jira_ignore or []}
    if "gitlab" not in cfg and "github" not in cfg:
        raise SystemExit("configure at least one of --gitlab-host / --github-user")
    save_json(CONFIG, cfg)
    print(f"wrote {CONFIG}")


def cmd_check(_a):
    cfg = load_config()
    ok = True
    try:
        _t, gl, gh = build_sources(cfg)
    except Exception as exc:
        print(f"✗ {exc}")
        return 1
    for src in (gl, gh):
        if not src:
            continue
        try:
            for line in src.check():
                print(f"✓ {line}")
        except Exception as exc:
            ok = False
            print(f"✗ {src.name}: {exc}")
    if gl and (cfg["gitlab"].get("vpn") or {}).get("name"):
        name = cfg["gitlab"]["vpn"]["name"]
        try:
            up = vpn_up(cfg["gitlab"]["vpn"], gl.host)
            print(f"{'✓' if up else '✗'} VPN {name}: {'connected' if up else 'NOT connected'}")
            ok = ok and up
        except Exception as exc:
            ok = False
            print(f"✗ VPN {name}: {exc}")
    print(f"config: {CONFIG}\nstate:  {STATE}")
    return 0 if ok else 1


def main(argv=None):
    p = argparse.ArgumentParser(prog="mr_watch.py", description="GitLab MR + GitHub PR watcher for Hermes cron.")
    p.add_argument("--version", action="version", version=f"mr-watch {VERSION}")
    sub = p.add_subparsers(dest="cmd")
    i = sub.add_parser("init", help="write the config")
    i.add_argument("--lang", choices=sorted(STRINGS), default="en")
    i.add_argument("--gitlab-host")
    i.add_argument("--gitlab-user")
    i.add_argument("--gitlab-project", action="append", help="group/project (repeatable)")
    i.add_argument("--vpn-name", help="label of the VPN that reaches GitLab (enables the VPN gate)")
    i.add_argument("--vpn-interface-prefix", action="append", help="e.g. utun (full-tunnel VPNs only)")
    i.add_argument("--github-user")
    i.add_argument("--github-account", help="gh account to use when several are logged in")
    i.add_argument("--github-repo", action="append", help="owner/repo to limit to (repeatable; default: all)")
    i.add_argument("--jira-url", help="e.g. https://acme.atlassian.net/browse/{key}")
    i.add_argument("--jira-pattern", help=r"e.g. PROJ-\d+")
    i.add_argument("--jira-ignore", action="append", help="placeholder keys to ignore, e.g. PROJ-0000")
    i.add_argument("--force", action="store_true")
    sub.add_parser("check", help="validate config, CLIs, auth and VPN")
    sub.add_parser("run", help="one tick (default)")
    a = p.parse_args(argv)
    if a.cmd == "init":
        return cmd_init(a)
    if a.cmd == "check":
        return cmd_check(a)
    tick()
    return 0


if __name__ == "__main__":
    sys.exit(main())
