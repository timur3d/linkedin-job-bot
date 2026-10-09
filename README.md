# LinkedIn job bot

Watches LinkedIn for new postings and sends the relevant ones to a Telegram chat. It runs on GitHub Actions, so there is nothing to host. It runs two searches side by side:

- **🎨 Design**: roles that fit the portfolio (branding, UI/UX, web, icons, 3D illustration). Part-time, student and freelance roles get a ⭐.
- **🎓 CS student**: student positions and internships in software, QA, data, DevOps, security and similar.

GitHub starts the bot once an hour. Each run handles what you did in Telegram since the last one, checks LinkedIn, sends new jobs, and saves its history for the next run. Scraping is done by [JobSpy](https://github.com/speedyapply/JobSpy).

## Setup

1. In Telegram, open **@BotFather**, send `/newbot`, and copy the token it gives you.
2. Put these files in a GitHub repository. The workflow file `.github/workflows/jobwatch.yml` has to be on the default branch.
   - **Public repository**: Actions minutes are free.
   - **Private repository**: you get 2,000 free minutes a month. A run takes roughly 4 to 6 minutes, so change `cron` in the workflow file to `"17 */3 * * *"` (every 3 hours).
3. In the repository, go to **Settings → Secrets and variables → Actions → New repository secret** and add `BOT_TOKEN` with the token.
4. Test without sending anything: **Actions → Job watch → Run workflow**, choose mode `dry-run`. The log shows what would be sent and what was filtered out. This is also where you find out whether LinkedIn answers GitHub's servers at all.
5. Open your bot in Telegram and send `/start`. Then **Run workflow** again with mode `check`. The bot replies in the chat and posts the jobs from the last 24 hours.
6. The bot's reply includes your chat ID. Add it as a second secret named `CHAT_ID`. It works without it, but with it the bot keeps posting to you even if its history is ever reset.

From then on it runs by itself. Each run's result is on the run's page under **Actions**.

## In Telegram

| | |
|---|---|
| `/status` | Last check and totals |
| `/applied` | Jobs you marked as applied |
| **Mark applied** | Tracks where you sent a CV; tap again to undo |
| **Hide** | Removes a job you don't want |

Nothing is listening between runs, so buttons and commands take effect at the next run. Tapping a button twice while you wait counts once. To check right now, use **Run workflow** on the Actions page.

If every search fails three runs in a row, the bot says so in the chat, and again when LinkedIn answers.

## What to expect from GitHub

- **Runs can start late.** GitHub delays scheduled runs when it is busy, sometimes by a long while, and can drop one. The bot looks back over the whole gap since its last successful search, so a late or skipped run doesn't lose postings.
- **GitHub pauses schedules in quiet public repositories.** After 60 days without repository activity it disables the scheduled workflow. Re-enable it on the Actions page.
- **The history is a cache.** It is a small SQLite file kept in GitHub's Actions cache, which deletes entries nobody has used for 7 days. If the bot stops running for a week it starts fresh: it sends the last 24 hours again and forgets what you marked as applied. The file holds no token, but it does hold the jobs you were sent, the ones you marked as applied, and your Telegram chat ID, and a workflow run started by a pull request can read caches. In a public repository, don't approve workflow runs on pull requests from people you don't know.
- **LinkedIn may refuse GitHub's servers.** If the dry run fails every search, add a `PROXIES` secret (comma-separated `user:pass@host:port`).
- **A wrong setting stops the run.** If the token is missing or rejected, or `config.yaml` has a mistake, the run fails and its page says what to fix.

## Tuning the search

Edit `config.yaml` in the repository; GitHub's web editor is enough. The next run uses it. The comments at the top explain how terms match. Each track has:

- `search_terms`: what is typed into LinkedIn's search.
- `exclude_title`: titles to drop outright (chip design, interior design, PhD positions...).
- `rules`: what makes a job relevant. Clear titles pass on the title alone. Vague ones ("Designer", "Student", "Software Engineer") pass only if the posting's text confirms it.

To check a change before you rely on it, run the workflow in `dry-run` mode: the log lists what would be sent, what was filtered out, and why.

Common edits:

- **Hide senior design roles**: in the `design` track set `exclude_title: [not_visual_design, senior]`.
- **Skip an area**: `exclude_locations: ["Haifa", "North District", "חיפה"]`.
- **Add motion design**: uncomment the two lines under `design_roles`.
- **Fewer messages on day one**: lower `first_run_hours`.
- **Check more or less often**: `cron` in `.github/workflows/jobwatch.yml`.

## How it works

Each check runs every search term for each location, asking LinkedIn only for postings since that term last succeeded (plus an hour of overlap). LinkedIn orders results by relevance, not date, so the short window is what keeps new postings from being buried.

New postings are judged on the title first. Anything that might be relevant is opened once to read the description and LinkedIn's "Employment type" and "Seniority level", which is how part-time and internship roles are tagged. Postings that can't be read are retried on the next checks.

Every posting is recorded in the history file, so nothing is sent twice. The same title, company and location under a new LinkedIn id within 14 days counts as a repost and is skipped.

## Good to know

- LinkedIn's terms don't allow automated access. The bot reads the public, logged-out job pages at a modest pace and never touches a LinkedIn account, but LinkedIn can still throttle or block an IP for a while. The bot backs off when that happens.
- `requirements.txt` pins JobSpy to one version on purpose. JobSpy's public API can only fetch descriptions for a whole search at a time, so single postings are read through one of its internal methods. If a future JobSpy changes that method, the bot keeps working on titles alone and logs a warning.
- Filtering is by keywords, so it will sometimes be wrong. Each message says why it matched, which makes the term to change easy to find.

## Files

```
.github/workflows/jobwatch.yml   the scheduled run
config.yaml                      what to search for and how to judge it
requirements.txt                 the three libraries the run installs
jobwatch/
  __main__.py          what the workflow calls (--once, --dry-run)
  config.py            loads and validates config.yaml and the secrets
  linkedin.py          JobSpy calls
  matching.py          relevance rules
  monitor.py           one check: search, decide, send
  storage.py           the history (SQLite)
  formatting.py        message text
  telegram.py          commands, buttons, catching up between runs
  console.py           output for the dry run
```
