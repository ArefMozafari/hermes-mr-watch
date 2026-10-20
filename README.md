# hermes-mr-watch

**Review pings from GitLab and GitHub, in your Hermes chat, within a minute.**

A [Hermes Agent](https://hermes-agent.nousresearch.com) skill. You install it, and Hermes sets up a cron job that runs every minute without an LLM. The job tells you when someone needs you on a merge request or pull request, and stays silent the rest of the time.

[![Tests](https://github.com/ArefMozafari/hermes-mr-watch/actions/workflows/test.yml/badge.svg)](https://github.com/ArefMozafari/hermes-mr-watch/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

```
💬 Comment on your MR
Show notification banner on the chat list
🔢 !1026 · 🎫 PROJ-3292
👤 Sara
📄 chat_list_banner.dart
nit: extract this into a widget
━━━━━━━━━━━━━━━
✅ Approved
Fix crash on cold start
🔢 acme/app#41 · 🎫 PROJ-3301
👤 boss
```

## What it watches

| | GitLab | GitHub |
|---|:-:|:-:|
| Review requested from you | ✓ | ✓ |
| Comments on your MRs/PRs, with file and excerpt | ✓ | ✓ |
| Approved · changes requested · merged · closed · reopened | ✓ | ✓ |
| Replies in threads where you commented | ✓ | ✓ |
| `@you` mentions | ✓ | ✓ |
| Jira ticket link (from branch, title or description) | optional | optional |
| **"Your VPN is down, connect it"** | optional | — |

- **Only what concerns you.** Your own actions and bots are skipped. Busy repositories with hundreds of events a day still produce a handful of messages.
- **No LLM, no tokens.** It runs as a `--no-agent` cron script, and Hermes delivers its output as-is.
- **VPN-aware.** If your GitLab is only reachable through a company VPN (Tunnelblick, Pritunl, OpenVPN Connect, WireGuard…), you get one *"connect your VPN"* message instead of a stream of timeouts. When the VPN is back, the watcher catches up on everything you missed.
- **Quiet about network blips.** It alerts once after 5 failed minutes in a row, and once when the source recovers. Cursors only advance on success, so no event is lost.
- **Several GitLab projects, and all or selected GitHub repos.**
- **English or Persian** messages.
- **Standard library only.** It uses the `glab` / `gh` you are already logged into, so no tokens are stored.

## Install

### New to Hermes? (one-time, ~5 min)

1. **Install Hermes:** `curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash`, then run `hermes setup` and pick a model provider. On macOS you can install the [desktop app](https://hermes-agent.nousresearch.com/docs/getting-started/installation) instead.
2. **Connect Telegram:** run `hermes plugins enable telegram-platform`, then `hermes gateway setup` → **Telegram**. Choose the QR option (scan it in Telegram, and Hermes creates the bot for you) or paste a token from [@BotFather](https://t.me/BotFather). Say **yes** to allow-listing your own account, so only you can use the bot.
3. **Keep it running:** run `hermes gateway install`, so the bot and the cron jobs survive reboots. Then send your bot a message. If it answers, you're ready.

Full guides: [installation](https://hermes-agent.nousresearch.com/docs/getting-started/installation), [Telegram](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/telegram).

### Install mr-watch
```bash
hermes skills install ArefMozafari/hermes-mr-watch/skills/mr-watch
```

Then tell Hermes:

> set up mr-watch

Hermes asks for your GitLab host, projects and username, your GitHub username, and optionally your Jira URL and VPN. Then it checks that `glab` / `gh` are logged in, writes the config, takes a silent baseline, and schedules the job. It also runs the job once to confirm everything works.

**Requirements:** [Hermes Agent](https://hermes-agent.nousresearch.com) with a chat platform connected, macOS or Linux, Python 3.9+, [`glab`](https://gitlab.com/gitlab-org/cli) and/or [`gh`](https://cli.github.com) logged in.

<details>
<summary>Manual setup (without asking Hermes)</summary>

```bash
SKILL=~/.hermes/skills/mr-watch          # or wherever `hermes skills install` put it
cp "$SKILL/scripts/mr_watch.py" ~/.hermes/scripts/

python3 ~/.hermes/scripts/mr_watch.py init --lang en \
  --gitlab-host gitlab.example.com --gitlab-user jane --gitlab-project team/app \
  --vpn-name "Company VPN" \
  --github-user jane \
  --jira-url 'https://acme.atlassian.net/browse/{key}' --jira-pattern 'PROJ-\d+'

python3 ~/.hermes/scripts/mr_watch.py check     # every line should be ✓
python3 ~/.hermes/scripts/mr_watch.py           # silent baseline

hermes cron create "* * * * *" --name mr-watch --script mr_watch.py --no-agent \
  --interpreter "$(command -v python3)" --deliver telegram
```

Run `python3 mr_watch.py init --help` for every option.
</details>

## How the VPN check works

Company VPNs are usually *split tunnels*: only company addresses go through the tunnel. Each tick, mr-watch asks the OS which network interface carries traffic to your GitLab host, and compares it with the interface used for the public internet. If they differ, the VPN is up. If they are the same, your GitLab traffic is leaving through your normal connection, which means the VPN is down. If it stays down for 2 minutes, you get the alert. This works the same whichever VPN app you use.

For full-tunnel VPNs, pass `--vpn-interface-prefix utun` (macOS) or `tun` / `wg` (Linux).

## Files

| Path | What |
|---|---|
| `~/.hermes/scripts/mr_watch.py` | the script the cron job runs |
| `~/.hermes/mr-watch/config.json` | your settings |
| `~/.hermes/mr-watch/state.json` | cursors and health counters |

## Update and uninstall

```bash
hermes skills update mr-watch && cp ~/.hermes/skills/mr-watch/scripts/mr_watch.py ~/.hermes/scripts/
```

```bash
hermes cron remove <job_id> && rm ~/.hermes/scripts/mr_watch.py && rm -r ~/.hermes/mr-watch
```

## Development

```bash
python3 -m unittest discover -s tests -v                          # offline, fake API responses
uvx --from skills-ref==0.1.1 agentskills validate skills/mr-watch # Agent Skills spec
```

## License

[MIT](LICENSE)
