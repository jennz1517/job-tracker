# Job Tracker (ATS -> Slack)

This project checks job listings every 15 minutes and alerts you in Slack when a **new** matching role appears at your target companies.

It uses:
- **GitHub Actions** for scheduling (no server needed)
- **SQLite** (`jobs.db`) to remember which jobs have already been seen
- **Slack incoming webhook** for alerts

---

## What this tracker does

1. Reads companies from `companies.csv`
2. Polls public ATS APIs (Greenhouse, Lever, Ashby)
3. Keeps jobs where the title includes `"product designer"` (case-insensitive)
4. Excludes titles containing: `engineer`, `manager`, `researcher`, `writer`, `content`
5. Saves seen jobs into `jobs.db` using `(ats, job_id)` as a unique key
6. Sends Slack message only for jobs that are new to the database

On the **first run**, all currently open matching jobs are treated as new.  
On the **second run and later**, only truly new postings trigger Slack.

---

## 1) Test locally with `--dry-run` (recommended first)

This prints messages to your terminal instead of sending to Slack.

```bash
cd "/Users/jennyzhu/Desktop/Vibe code/job-tracker"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python poll.py --dry-run
```

What to expect:
- You will see per-company counts
- For new jobs, you will see `[DRY RUN] Would send Slack message: ...`
- A `jobs.db` file will be created/updated

---

## 2) Test locally with a real Slack message

First, create a Slack Incoming Webhook URL in your Slack workspace.

Then run:

```bash
cd "/Users/jennyzhu/Desktop/Vibe code/job-tracker"
source .venv/bin/activate
export SLACK_WEBHOOK="https://hooks.slack.com/services/XXX/YYY/ZZZ"
python poll.py
```

Notes:
- Do **not** include quotes inside the URL itself.
- If this is your first real run with an empty `jobs.db`, expect many messages.

---

## 3) Push this to a new private GitHub repo

1. On GitHub, create a new **private** repository (for example: `job-tracker`)
2. In your terminal:

```bash
cd "/Users/jennyzhu/Desktop/Vibe code/job-tracker"
git init
git add .
git commit -m "Initial job tracker setup"
git branch -M main
git remote add origin https://github.com/<your-username>/<your-repo>.git
git push -u origin main
```

Replace `<your-username>` and `<your-repo>` with your values.

---

## 4) Add `SLACK_WEBHOOK` as a GitHub secret

1. Open your repo on GitHub
2. Go to **Settings** -> **Secrets and variables** -> **Actions**
3. Click **New repository secret**
4. Name: `SLACK_WEBHOOK`
5. Value: your full Slack webhook URL
6. Save

---

## 5) Manually trigger the workflow the first time

1. Open your repo on GitHub
2. Go to **Actions**
3. Click the workflow named **Poll ATS Jobs**
4. Click **Run workflow**

This first run initializes `jobs.db` and commits it back to the repository.

---

## 6) Add or remove companies later

Edit `companies.csv` and commit/push the change.

Format:

```csv
name,ats,identifier
Stripe,greenhouse,stripe
Ramp,ashby,ramp
Palantir,lever,palantir
```

Supported `ats` values:
- `greenhouse`
- `lever`
- `ashby`

---

## 7) GitHub Actions free-tier note (important)

Private repositories get **2,000 Actions minutes/month** on the free plan.

If you want to reduce usage, choose one:
- Make the repository **public** (public repos get free standard Actions usage), or
- Change the cron schedule in `.github/workflows/poll.yml` from:
  - `*/15 * * * *` to `*/30 * * * *`

---

## Files in this project

- `poll.py` - main polling + filtering + dedupe + Slack logic
- `companies.csv` - your target companies
- `jobs.db` - seen jobs state (auto-created)
- `requirements.txt` - Python dependency list
- `.github/workflows/poll.yml` - GitHub Actions schedule and automation
