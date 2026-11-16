# Damir Job Radar v3  

https://damir-job-radar-v3.streamlit.app/

Automatic vacancy search for construction, infrastructure, WASH, procurement, project management and heritage roles, with explainable 0–100 fit scoring against Damir Hadžić's profile.

Three files. No API keys, no secrets, no database, no GitHub Actions, no paid services.

```text
app.py
requirements.txt
README.md
```

## What it does

- **Searches the internet automatically** when you open the app, then again every 6 hours (or press **Search now**).
- Searches UNDP, IOM, WFP, UNHCR, UNICEF and Arbeitnow (Europe) for your keywords.
- Reads the full vacancy text for the most relevant titles (up to 30 per site).
- Removes duplicates and expired vacancies.
- Scores every job and explains why: strengths, gaps and items to verify.
- Flags eligibility problems: national/locally recruited posts outside Bosnia and Herzegovina, nationality restrictions, licences, languages you do not list.
- Tabs: Best matches, All results, New (7 days), Closing soon, My tracker, Analyze vacancy, Import CSV, Sources, Help.
- Tracker: Saved / Applied / Interview / Rejected / Not interested, with download and restore.
- Exports: filtered CSV, Markdown shortlist, and a ready CV-tailoring prompt for every vacancy.
- Add any other career site in the **Sources** tab: Workday, Oracle Recruiting Cloud, Greenhouse, Lever, Ashby, or an RSS/Atom feed.

## Upload to GitHub

1. Create a new repository, for example `damir-job-radar-v3`.
2. **Add file → Upload files**.
3. Drag in `app.py`, `requirements.txt`, `README.md` (the files, not the ZIP).
4. **Commit changes**.

## Deploy on Streamlit Community Cloud

1. Open https://share.streamlit.io and sign in with GitHub.
2. **Create app** → choose the repository, branch `main`, main file `app.py`.
3. **Advanced settings** → Python **3.12**.
4. **Deploy**. No secrets are needed.

The first search takes up to about a minute. After that, results are cached for 6 hours.

## Run on your computer (Windows PowerShell)

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
streamlit run app.py
```

Opens at http://localhost:8501. Stop with `Ctrl + C`.

## Scoring (100 points)

| Component | Points |
|---|---:|
| Title / role family | 20 |
| Technical competencies (distinct matches; repetition adds nothing) | 25 |
| Organization / sector | 15 |
| Experience | 15 |
| Education | 10 |
| Location and eligibility | 5 |
| Language | 5 |
| Contract | 5 |

85+ Excellent · 75+ Strong · 65+ Good · 50+ Possible · below 50 Low priority.

- **Disqualified**: unrelated role family (software, medical, driver, internship…) or an explicit nationality restriction. Hidden from the main tabs; still in "Download all".
- **Review required**: strong match with a possible blocker (national post outside BiH, licence, unlisted language).
- **Verify**: unknowns. Never counted as matched. French and Italian appear here until you set your level.

## Change what it searches

- **Keywords**: sidebar, one per line.
- **Sources**: Sources tab. Untick to disable, or add a row with a public careers URL, then **Save sources and search**.
- **Profile, keywords, scoring terms**: top of `app.py`, section "SETTINGS YOU MAY EDIT".
- Save your keywords and sources with **Download my settings**; load them again after a restart.

## Limits (read before relying on it)

- The career sites' public endpoints are not official APIs. They can change without notice. When a source breaks, the Sources tab shows the reason and the other sources keep working.
- UNICEF is read from its public listing page (best effort). Keep your UNICEF email job alerts as a backup.
- Not searched automatically: LinkedIn, Indeed, UN Careers (Inspira), UNOPS. Paste those vacancies into **Analyze vacancy**.
- Streamlit Community Cloud does not keep files between restarts. Download your tracker and settings regularly.
- Scoring is keyword based. Always read the vacancy before applying.

## Troubleshooting

| Problem | Fix |
|---|---|
| A source shows "Failed" | Open the Sources tab for the reason. Press Search now later. If it persists, untick it. |
| All sources failed | The host may be blocking outbound requests. Run locally to confirm. Use Analyze vacancy / Import CSV meanwhile. |
| Few results | Lower the minimum score, add keywords, enable "Read full vacancy texts". |
| `ModuleNotFoundError` on deploy | Make sure `requirements.txt` is in the repository root, then reboot the app. |
| Tracker empty after restart | Upload your downloaded tracker CSV in My tracker. |
