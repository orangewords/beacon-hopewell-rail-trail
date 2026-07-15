# Municipal Meeting Minutes Monitor

_Written with Claude Opus 4.6_

Automatically checks local government websites for new meeting minutes, agendas, and documents. Sends you an email when something changes. Runs daily via GitHub Actions — no server required.

## Monitored sites

| Municipality | What's tracked |
|---|---|
| City of Beacon | Agendas & Minutes page, Agenda portal |
| Town of Fishkill | IQM2 citizen portal |
| Village of Fishkill | Main site, Boards & Committees page |
| Town of East Fishkill | Agendas & Minutes page |

## How it works

1. A GitHub Actions cron job runs `monitor.py` once a day (default: 9 AM Eastern).
2. The script fetches each site, extracts the text content and all document/meeting links.
3. It compares the current state to a saved snapshot from the previous run.
4. If the page content changed **or** new document links appeared, you get an email.
5. Updated snapshots are committed back to the repo so the next run has a fresh baseline.

## Setup

### 1. Create the GitHub repo

```bash
git clone <this-repo>
cd meeting-monitor
git remote set-url origin https://github.com/YOUR_USERNAME/meeting-monitor.git
git push -u origin main
```

Or just click **Use this template** if you've made it a template repo.

### 2. Set up Resend (email delivery)

1. Sign up at https://resend.com (free tier — 3,000 emails/month)
2. Go to https://resend.com/api-keys and create an API key
3. Copy the key (it starts with `re_`)

### 3. Add secrets to GitHub

Go to your repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret** and add:

| Secret | Value |
|---|---|
| `RESEND_API_KEY` | The API key from step 2 |
| `EMAIL_TO` | The address you want reports sent to |

That's it — just two secrets. On the free tier (no custom domain), emails arrive from `onboarding@resend.dev`. If you later add a verified domain in Resend, you can set an `EMAIL_FROM` secret to use your own sender address.

### 4. Test it

Go to the **Actions** tab → **Check Meeting Minutes** → **Run workflow** to trigger a manual run. The first run saves a baseline snapshot for every site and sends an initial report.

## Customization

### Add or remove sites

Edit `sites.json`:

```json
[
  {
    "name": "Human-readable name",
    "url": "https://example.gov/minutes/"
  }
]
```

### Change the schedule

Edit the cron expression in `.github/workflows/check.yml`:

```yaml
schedule:
  - cron: "0 13 * * *"   # 9 AM Eastern (UTC-4)
```

Useful examples:
- `"0 13 * * 1-5"` — weekdays only
- `"0 13 * * 1,3,5"` — Mon / Wed / Fri
- `"0 */6 * * *"` — every 6 hours

## Run locally

```bash
pip install -r requirements.txt

# Without email (prints report to terminal):
python monitor.py

# With email:
RESEND_API_KEY=re_your_key_here \
EMAIL_TO=you@example.com \
python monitor.py
```

## Known limitations

- **JavaScript-heavy sites**: Some municipal platforms (notably IQM2) load content dynamically. The script fetches raw HTML, so if a site requires JavaScript to render its meeting list, changes to that content may not be detected. If you find a site isn't being picked up, open an issue and it can be adapted to use a headless browser.
- **Rate limits**: The script waits 2 seconds between requests to be respectful. GitHub Actions free tier provides 2,000 minutes/month, which is far more than this needs.
- **First run**: The initial run will flag every site as "new" and save a baseline. Actual change detection starts from the second run onward.

## License

MIT
