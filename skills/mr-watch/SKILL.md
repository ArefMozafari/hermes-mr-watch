---
name: mr-watch
description: Use when setting up, checking, updating or removing a GitLab merge-request / GitHub pull-request watcher that pings you in chat about reviews, comments, approvals, merges and @-mentions, with a VPN-disconnect alert for GitLab. Runs as a no-LLM Hermes cron job every minute.
license: MIT
compatibility: Hermes Agent with cron; macOS or Linux; Python 3.9+; glab and/or gh logged in.
metadata:
  author: ArefMozafari
  version: "1.0.0"
---

# mr-watch: review pings from GitLab and GitHub

A Hermes cron job that runs [scripts/mr_watch.py](scripts/mr_watch.py) every minute with `--no-agent`. No LLM is involved and no tokens are spent. The script prints only the events that concern the user, and Hermes delivers that text to their chat. When there is nothing new the output is empty and nothing is sent.

What it reports:

| | GitLab (`glab`) | GitHub (`gh`) |
|---|---|---|
| Review requested from you | ✓ | ✓ |
| Comments on your MRs/PRs (with file name and excerpt) | ✓ | ✓ |
| Approved, changes requested, merged, closed, reopened | ✓ | ✓ |
| Replies in threads where you commented | ✓ | ✓ |
| `@you` mentions | ✓ | ✓ |
| Jira ticket link from branch, title or description | optional | optional |
| "VPN disconnected, connect it" (before GitLab calls start failing) | optional | — |

Your own actions and bot accounts are never reported. Transient network failures stay silent: one alert is sent only after 5 failed ticks in a row, plus one when the source recovers. Cursors are not advanced on a failure, so nothing is missed.

## Set up

Ask the user only for what you can't discover. Run every command yourself with the terminal tool.

1. **Prerequisites.** Check `python3 --version` (≥ 3.9). For GitLab: `glab --version` and `glab auth status --hostname <host>`. For GitHub: `gh --version` and `gh auth status`. If one is missing or logged out, tell the user the exact install or login command (`brew install glab` / `glab auth login --hostname <host>`, `brew install gh` / `gh auth login`). Never ask for or type tokens yourself.
2. **Collect settings.** Ask in one go:
   - **GitLab:** host, username, and one or more `group/project` paths. Skip if unused.
   - **GitHub:** username, and whether to watch all their repos (the default) or only some `owner/repo`. Skip if unused. If `gh auth status` lists several accounts, use `--github-account <login>`. This reads that account's token without switching the active one.
   - **Jira (optional):** browse URL with `{key}` (e.g. `https://acme.atlassian.net/browse/{key}`), key pattern (e.g. `PROJ-\d+`), and placeholder keys to ignore (e.g. `PROJ-0000`).
   - **VPN (optional, GitLab only):** whether GitLab is only reachable over a VPN (Tunnelblick, Pritunl, OpenVPN Connect, WireGuard…), and the name to show in the alert.
   - **Language** for the messages: `en` or `fa`.
3. **Install the script.** Hermes only runs cron scripts from `~/.hermes/scripts/`:
   ```bash
   SKILL_DIR="<directory containing this SKILL.md>"
   mkdir -p ~/.hermes/scripts && cp "$SKILL_DIR/scripts/mr_watch.py" ~/.hermes/scripts/mr_watch.py
   ```
4. **Write the config.** Pass only the flags that apply:
   ```bash
   python3 ~/.hermes/scripts/mr_watch.py init --lang en \
     --gitlab-host gitlab.example.com --gitlab-user jane --gitlab-project team/app --gitlab-project team/api \
     --vpn-name "Company VPN" \
     --github-user jane --github-account jane \
     --jira-url 'https://acme.atlassian.net/browse/{key}' --jira-pattern 'PROJ-\d+' --jira-ignore PROJ-0000
   ```
   This writes `~/.hermes/mr-watch/config.json`. Re-run with `--force` to change it.
5. **Verify.** Run `python3 ~/.hermes/scripts/mr_watch.py check` and fix every `✗` line before continuing.
6. **Baseline.** Run `python3 ~/.hermes/scripts/mr_watch.py` once. The first run records where "now" is and prints nothing, so the user isn't flooded with history.
7. **Schedule it.** Deliver to the chat the user is talking to you from (`origin`), or to a platform they name:
   ```bash
   hermes cron create "* * * * *" --name "mr-watch" --script mr_watch.py --no-agent \
     --interpreter "$(command -v python3)" --deliver origin
   ```
   Pin `--interpreter`: the script is stdlib-only, and pinning keeps cron independent of Hermes' own Python environment. When creating it with the `cronjob` tool instead, pass the same `schedule`, `script`, `no_agent` and `interpreter`.
8. **Confirm.** Run `hermes cron run <job_id>`, then `hermes cron list`. `Last run … ok` means it works. Tell the user what it watches and that the first real ping arrives with the next event.

## Check health

- `hermes cron list`: the job's last run should be `ok`.
- `hermes cron runs <job_id> --limit 50`: shows any `failed` runs and their stderr.
- `python3 ~/.hermes/scripts/mr_watch.py check`: auth, projects and VPN status.
- `~/.hermes/mr-watch/state.json`: `health.<source>.count` is the current streak of failed ticks. `vpn.down` counts ticks the VPN has been down.

## Update

`hermes skills update mr-watch`, then copy the script into `~/.hermes/scripts/` again (step 3). The config and state are kept.

## Remove

`hermes cron remove <job_id>`, then `rm ~/.hermes/scripts/mr_watch.py`, then `rm -r ~/.hermes/mr-watch`.

## Pitfalls

- **Always use `--no-agent`.** Without it, every minute becomes an LLM call.
- **Don't run it more often than once a minute.** The Hermes ticker fires every 60 s. `every 1m` can drift to every 2 minutes, but the cron expression `* * * * *` doesn't drift.
- **VPN detection and split tunnels.** For split-tunnel VPNs (the usual company setup), detection compares the route to the GitLab host with the route to the public internet. If the user's VPN carries *all* traffic, re-run `init` with `--vpn-interface-prefix utun` (macOS) or `tun`/`wg` (Linux).
- **GitHub reads more than one account.** If `check` says the token belongs to another account, add `--github-account <login>`.
- **Script path.** Hermes rejects cron scripts outside `~/.hermes/scripts/`. Point the job at the copy there, not at the skill directory.
- **Python.** Everything is stdlib. Don't `pip install` anything for it.
