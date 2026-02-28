# 🎯 Co-op Job Application Assistant

A Python CLI tool that scrapes co-op/internship listings from **Sheridan Works** and **Indeed Canada**, tracks your application status locally, and opens job application forms in a real browser with your personal info pre-filled — so you can apply to dozens of jobs in one session without re-typing anything.

> **Built for Sheridan College students** applying to Summer 2026 co-op positions.

---

## ✨ Features

- 🔍 **Scrapes Sheridan Works** — logs in via Microsoft SSO, handles MFA, pulls all Summer 2026 co-op postings for your program
- 🔍 **Scrapes Indeed Canada** — Cloudflare bypass, persistent Chrome profile, filtered to the Greater Toronto Area
- 📋 **Tracks every job** with statuses: `new → seen → applied / skipped`
- 🚀 **`apply-all` mode** — loops through all new jobs one-by-one, sorted by deadline (most urgent first), opening each in a browser
- 🪟 **Dual-tab apply** — if a Sheridan posting links out to an external ATS (Workday, Greenhouse, Lever…), both the Sheridan page *and* the external form open side-by-side in the same window
- ✍️ **Auto-prefill** — fills name, email, phone, LinkedIn, GitHub, location, and availability on any form
- 📎 **Resume attach** — automatically sets your PDF on any `<input type="file">` it finds
- 🤖 **Workday-aware** — fills Workday's `data-automation-id` fields directly for faster prefill
- 🔗 **`set-url`** — attach an external application URL to any Sheridan job so both tabs always open
- 🔒 **Never auto-submits** — you always review and click Submit yourself

---

## 📁 Project Structure

```
.
├── main.py                  # CLI entry point
├── requirements.txt
├── .env.example             # Copy to .env and fill in your details
│
├── scrapers/
│   ├── base.py              # BaseScraper (human_delay, human_type, etc.)
│   ├── sheridan.py          # Sheridan Works scraper (Microsoft SSO login)
│   └── indeed.py            # Indeed Canada scraper (Cloudflare bypass)
│
├── browser/
│   └── apply.py             # Opens job pages, prefills fields, attaches resume
│
├── models/
│   └── job.py               # Job dataclass + make_job_id()
│
├── storage/
│   └── jobs.py              # load/save/merge/filter jobs (jobs.json)
│
└── ui/
    └── display.py           # Rich terminal tables and job detail panels
```

---

## 🚀 Setup

### 1. Clone & create a virtual environment

```bash
git clone https://github.com/Segzman/coop-job-assistant.git
cd coop-job-assistant
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure your `.env`

```bash
cp .env.example .env
```

Open `.env` and fill in your details:

```env
# Sheridan Works (Microsoft SSO)
SHERIDAN_USERNAME=yourname@sheridancollege.ca
SHERIDAN_PASSWORD=YourPassword

# Your personal info for form pre-fill
APPLICANT_NAME=Your Full Name
APPLICANT_EMAIL=your@email.com
APPLICANT_PHONE=+1-416-000-0000
APPLICANT_PHONE_DIGITS=4160000000
APPLICANT_LINKEDIN=https://www.linkedin.com/in/yourprofile/
APPLICANT_GITHUB=https://github.com/yourusername
APPLICANT_LOCATION=Oakville, ON, Canada
APPLICANT_AVAILABILITY=April 2026
APPLICANT_RESUME_PATH=/absolute/path/to/your/resume.pdf

# Indeed (optional — keeps your session alive)
INDEED_EMAIL=your@email.com
INDEED_PASSWORD=YourIndeedPassword

# Scraper settings
INDEED_MAX_PAGES=2
```

---

## 🖥️ Usage

All commands use the Python from your virtual environment:

```bash
source .venv/bin/activate   # or: alias py=".venv/bin/python"
```

### Scrape jobs

```bash
# Scrape both platforms
python main.py scrape

# Sheridan Works only (faster, no Cloudflare)
python main.py scrape --sheridan

# Indeed only
python main.py scrape --indeed
```

> **Sheridan Works:** A browser window opens for Microsoft SSO login. Complete MFA on your phone, then the scraper takes over and pulls all co-op listings for your program.

### List jobs

```bash
# Show all new jobs (default)
python main.py list

# Filter by status
python main.py list --status applied
python main.py list --status all

# Filter by platform
python main.py list --platform sheridan
```

### Apply to all jobs (batch mode ⭐)

```bash
python main.py apply-all
```

- Shows every new/seen Sheridan job sorted by **deadline ascending**
- Jobs expiring today or tomorrow are flagged with a red ⚠️
- For each job: choose `apply`, `skip`, or `quit`
- Browser opens, fields are pre-filled — **you review and submit**
- After closing the browser, set the status (`applied` / `skipped`) and optional notes

### Apply to a single job

```bash
python main.py apply <job_id>
```

### Attach an external application URL

Many Sheridan postings link out to Workday, Greenhouse, etc. Attach the URL once and both tabs will open automatically every time:

```bash
python main.py set-url <job_id> https://company.wd3.myworkdayjobs.com/...
```

### Update a job's status manually

```bash
python main.py status <job_id> applied
python main.py status <job_id> skipped
```

---

## 🔐 Security & Privacy

- Your `.env` file (credentials + personal info) is **gitignored** and never committed
- Session cookies (`data/`) are **gitignored** and stay local
- The tool **never auto-submits** any form — every submission is 100% manual

---

## 🛠️ Tech Stack

| Tool | Purpose |
|---|---|
| [Playwright](https://playwright.dev/python/) | Browser automation (headful) |
| [python-dotenv](https://github.com/theskumar/python-dotenv) | `.env` credential loading |
| [Rich](https://github.com/Textualize/rich) | Terminal UI (tables, panels, prompts) |
| Microsoft SSO / Shibboleth | Sheridan Works authentication |
| Persistent Chrome Profile | Cloudflare & Indeed session persistence |

---

## 📝 License

MIT
