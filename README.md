# forksync

> Keep your GitHub forks automatically synced with upstream. Nightly. Zero cost. Zero effort.

## How it works

1. Every night at 6am UTC, forksync checks all your forks
2. Forks that can be fast-forwarded are synced automatically
3. Forks with conflicts get a GitHub Issue for manual review
4. A SYNC_REPORT.md is committed showing exactly what happened

## Setup (2 minutes)

### 1. Fork this repo

### 2. Add secrets
Go to Settings → Secrets → Actions:
- `GH_USERNAME` — your GitHub username

That's it. `GITHUB_TOKEN` is provided automatically by GitHub Actions.

### 3. Enable Actions
Go to Actions tab → Enable workflows

Syncing starts tonight at 6am UTC.

### 4. Optional: customize
Copy `fork-sync.yml.example` to `fork-sync.yml` to skip repos or
configure notifications.

## How it's efficient

forksync uses GitHub's **GraphQL API** to fetch all fork statuses in a single
batched query — typically 7-10 API calls for 700 forks, compared to 700 REST
calls with a naive approach.

## Safety guarantees

- **Fast-forward only** — never syncs a fork where you're ahead of upstream
- **No force push** — ever
- **No local git** — all operations go through GitHub's API (`merge-upstream`)
- **Conflict issues** — diverged forks get a GitHub Issue, not a silent failure
- **Dry run support** — preview changes without making them

## Notifications

Add optional secrets for notifications:
- `SLACK_WEBHOOK_URL` — Slack incoming webhook
- `DISCORD_WEBHOOK_URL` — Discord webhook

## Manual sync

Go to Actions → Nightly Fork Sync → Run workflow
Toggle "Dry run" to preview without making changes.

Or use the CLI:

```bash
# Install dependencies
pip install -r requirements.txt

# Dry run — show what would happen
python -m forksync run --dry-run

# Check status only
python -m forksync status

# Sync specific repos
python -m forksync run --repos vllm LightRAG

# View sync history
python -m forksync history

# Show config
python -m forksync config
```

## Configuration

Copy `fork-sync.yml.example` to `fork-sync.yml`:

```yaml
sync:
  strategy: fast_forward_only
  conflict_issues: true
  skip:
    - my-customized-fork
  always_sync:
    - vllm

notifications:
  slack:
    enabled: false
    webhook_url: ""  # or set SLACK_WEBHOOK_URL env var
```

## Built by
[Perditio](https://perditio.com) · Part of the [Reporium](https://reporium.com) suite
