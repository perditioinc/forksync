# forksync

> Keep GitHub forks in sync at scale. Supports 10,000+ repos. Runs in seconds.

## How it works

1. Every night at 6am UTC, forksync checks which forks are due based on upstream activity
2. Forks are synced using `gh repo sync` — the official GitHub CLI tool
3. Every sync is verified — a fork is only marked synced if confirmed up-to-date
4. A SYNC_REPORT.md is committed showing exactly what happened

## Smart scheduling

forksync checks repos based on how active their upstream is:

| Upstream activity | Check frequency |
|-------------------|-----------------|
| Pushed in last 30 days | Nightly |
| Pushed in last year | Weekly |
| Pushed over a year ago | Monthly |

At 10,000 repos this reduces nightly API calls by 70-80% compared to checking everything every night.

## Architecture

```
GitHub Actions (nightly cron)
  → POST /run to forksync Cloud Run service

forksync service
  → GraphQL batch fetch all fork metadata (9 calls for 800 forks)
  → Firestore batch read existing fork state
  → Filter to only forks due for checking (schedule tiers)
  → Compare API concurrently (50 parallel) with ETag caching
  → gh repo sync concurrently (20 parallel) for behind forks
  → Verify each sync with compare API
  → Firestore batch write results
  → Commit SYNC_REPORT.md
```

## Setup

### Prerequisites
- GCP project with Firestore and Memorystore (Redis) enabled
- Cloud Run service deployed
- GitHub token with repo scope

### 1. Deploy the service

```bash
gcloud run deploy forksync \
  --source . \
  --region us-central1 \
  --set-env-vars GH_TOKEN=your_token,FORK_OWNER=your_username
```

### 2. Add GitHub Actions secrets

Go to Settings → Secrets → Actions:
- `FORKSYNC_SERVICE_URL` — your Cloud Run service URL
- `FORKSYNC_API_KEY` — API key for the service

`GITHUB_TOKEN` is provided automatically by GitHub Actions.

### 3. Enable Actions

Go to Actions tab → Enable workflows

Syncing starts tonight at 6am UTC.

## Configuration

Create `fork-sync.yml`:

```yaml
sync:
  fork_owner: your-github-username
  concurrency_compare: 50
  concurrency_sync: 20
  verify_after_sync: true

schedule:
  nightly_threshold_days: 30
  weekly_threshold_days: 365

gcp:
  project_id: your-project-id
  firestore_collection: forks
  redis_host: your-memorystore-host

notifications:
  slack_webhook: ""
  discord_webhook: ""
```

## CLI

```bash
pip install forksync

# Dry run
python -m forksync run --dry-run

# Live sync
python -m forksync run

# Check status
python -m forksync status

# Sync specific repos
python -m forksync run --repos vllm langchain

# View sync history
python -m forksync history
```

## Safety guarantees

- **Fast-forward only** — never syncs a fork where you're ahead of upstream
- **No force push** — ever
- **Verified syncs** — every sync confirmed via compare API post-sync
- **Conflict issues** — diverged forks get a GitHub Issue
- **Dry run support** — preview without making changes

## Performance

| Repos | API calls/night | Runtime |
|-------|-----------------|---------|
| 800 | ~200 | <30s |
| 5,000 | ~500 | <60s |
| 10,000 | ~800 | <90s |

## Use as a library

forksync is pip-installable and can be used as a library:

```python
from forksync import run_sync, get_status

# Run a full sync
results = await run_sync()

# Get status of all forks
forks = await get_status()
```

Reporium uses forksync as a library to keep its fork registry current.

## Built by
[Perditio](https://perditio.com) · Part of the [Reporium](https://reporium.com) suite
