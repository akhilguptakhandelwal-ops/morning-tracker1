# 🌅 Morning Intelligence Tracker

Auto-scrapes YouTube channels + websites → Gemini AI summary → Gmail delivery via Google Apps Script.
Runs daily at 7 AM IST on GitHub Actions. No server. No SMTP. Completely free.

---

## Architecture

```
GitHub Actions (7 AM IST daily)
  → morning_tracker.py
      → Scrape YouTube RSS + Websites
      → Gemini 2.5 Flash AI Summary
      → POST to Google Apps Script Web App
          → Gmail sends the digest
```

---

## Setup (One-time, ~15 minutes)

### Step 1 — Google Apps Script (Email Sender)

1. Go to **[script.google.com](https://script.google.com)** → **New Project**
2. Delete the default code, paste the entire contents of `google_apps_script.js`
3. In the script, change `SECRET_TOKEN` to any random string you choose  
   Example: `"MIT_akhil_2024_xK9mP3"`
4. Click **Deploy → New Deployment**
   - Type: **Web App**
   - Execute as: **Me**
   - Who has access: **Anyone**
5. Click **Deploy** → Authorise with your Google account
6. **Copy the Web App URL** — you'll need it in Step 3

### Step 2 — Get Your Gemini API Key

1. Go to **[aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey)**
2. Click **Create API Key** → Copy it

### Step 3 — Add GitHub Secrets

In your GitHub repo → **Settings → Secrets and variables → Actions → New repository secret**

Add these three secrets:

| Secret Name | Value |
|---|---|
| `GEMINI_API_KEY` | Your Gemini API key from Step 2 |
| `GAS_WEBHOOK_URL` | The Apps Script Web App URL from Step 1 |
| `GAS_SECRET_TOKEN` | The same token you put in the Apps Script |

> ⚠️ Also update `morning_tracker.py` → `send_via_apps_script()` to include the token in the payload if you enabled token validation.

### Step 4 — Push to GitHub

```bash
git init
git add .
git commit -m "Initial commit — Morning Intelligence Tracker"
git remote add origin https://github.com/YOUR_USERNAME/morning-tracker.git
git push -u origin main
```

GitHub Actions will now run automatically at **7:00 AM IST every day**.

---

## Manual Trigger

Go to your repo → **Actions** tab → **Morning Intelligence Tracker** → **Run workflow**

---

## Add More Sources / Recipients

Edit `morning_tracker.py` → `init_db()` → the seed arrays at the top:

```python
sources_seed = [
    ("youtube", "CA Rachana Ranade", "https://www.youtube.com/feeds/videos.xml?channel_id=UCd5xLBi_QU6w7RGm5TTkU2g"),
    ("website", "MCA21", "https://www.mca.gov.in"),
    # add more here
]

for email in ("you@gmail.com", "colleague@company.com"):
    # add more emails here
```

Commit and push — takes effect on next run.

---

## File Structure

```
morning-tracker/
├── .github/
│   └── workflows/
│       └── morning_tracker.yml   ← GitHub Actions schedule
├── morning_tracker.py            ← Main script
├── google_apps_script.js         ← Paste this into script.google.com
├── requirements.txt
└── README.md
```
