# LinkedIn job bot

Watches LinkedIn for new postings and sends the relevant ones to a Telegram chat. It runs on GitHub Actions, so there is nothing to host. It runs two searches side by side:

- **🎨 Design**: roles that fit the portfolio (branding, UI/UX, web, icons, 3D illustration). Part-time, student and freelance roles get a ⭐.
- **🎓 CS student**: student positions and internships in software, QA, data, DevOps, security and similar.

GitHub starts the bot once an hour. When a check finds new jobs, the bot posts two things:

1. **A short list**: one line per job with the title as a link, the company, the city and tags such as part-time.
2. **A text file**: the same jobs in full, each with its link, LinkedIn's employment type and seniority, why it matched, and the whole description.

Each file holds only that check's new jobs, so the files in the chat add up to the full history. Scraping is done by [JobSpy](https://github.com/speedyapply/JobSpy).

## Setup

1. In Telegram, open **@BotFather**, send `/newbot`, and copy the token it gives you.
2. Put these files in a GitHub repository. The workflow file `.github/workflows/jobwatch.yml` has to be on the default branch.
   - **Public repository**: Actions minutes are free.
   - **Private repository**: you get 2,000 free minutes a month. A run takes roughly 4 to 6 minutes, so change `cron` in the workflow file to `"17 */3 * * *"` (every 3 hours).
3. In the repository, go to **Settings → Secrets and variables → Actions → New repository secret** and add `BOT_TOKEN` with the token.
4. Test without sending anything: **Actions → Job watch → Run workflow**, choose mode `dry-run`. The log shows what would be sent and what was filtered out. This is also where you find out whether LinkedIn answers GitHub's servers at all.
5. Open your bot in Telegram and send `/start`. Then **Run workflow** again with mode `check`. The bot replies at the start of the run and posts the jobs from the last 24 hours at the end of it. The first run takes 10 to 20 minutes.
6. The bot's reply includes your chat ID. Add it as a second secret named `CHAT_ID`. It works without it, but with it the bot keeps posting to you even if its history is ever reset.

From then on it runs by itself. Each run's result is on the run's page under **Actions**.

## In Telegram

The bot only wakes up for each check, so there is one command, and it is answered at the next run:

- `/status`: the last check's result and totals.

To check right now, use **Run workflow** on the Actions page.

If every search fails three runs in a row, the bot says so in the chat, and again when LinkedIn answers.

## What to expect from GitHub

- **Runs can start late.** GitHub delays scheduled runs when it is busy, sometimes by a long while, and can drop one. The bot looks back over the whole gap since its last successful search, so a late or skipped run doesn't lose postings.
- **GitHub pauses schedules in quiet public repositories.** After 60 days without repository activity it disables the scheduled workflow. Re-enable it on the Actions page.
- **The bot's memory is a cache.** What it has already seen is a small SQLite file kept in GitHub's Actions cache, which deletes entries nobody has used for 7 days. If the bot stops running for a week it starts fresh and sends the last 24 hours again. Your history is not affected: that is the files in the chat. The cache file holds no token, but it does hold your Telegram chat ID, and a workflow run started by a pull request can read caches. In a public repository, don't approve workflow runs on pull requests from people you don't know.
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
- **A smaller first batch**: lower `first_run_hours`.
- **Check more or less often**: `cron` in `.github/workflows/jobwatch.yml`.

## How it works

Each check runs every search term for each location, asking LinkedIn only for postings since that term last succeeded (plus an hour of overlap). LinkedIn orders results by relevance, not date, so the short window is what keeps new postings from being buried.

New postings are judged on the title first. Every posting that might be relevant is then opened once to read its description and LinkedIn's "Employment type" and "Seniority level". That settles the vague titles, is how part-time and internship roles are tagged, and is the text that goes into the file. At most 60 postings are opened per check; the rest wait for the next one.

A job is sent only once its description has been read. If the posting's page can't be read, the bot tries again on the next two checks, then sends the job without a description and says so in the file.

Every posting the bot has judged is remembered for 120 days, so nothing is sent twice. The same title, company and location under a new LinkedIn id within 14 days counts as a repost and is skipped.

## Good to know

- LinkedIn's terms don't allow automated access. The bot reads the public, logged-out job pages at a modest pace and never touches a LinkedIn account, but LinkedIn can still throttle or block an IP for a while. The bot backs off when that happens.
- `requirements.txt` pins JobSpy to one version on purpose. JobSpy's public API can only fetch descriptions for a whole search at a time, so single postings are read through one of its internal methods. If a future JobSpy changes that method, the bot keeps working on titles alone, without descriptions, and logs a warning.
- Filtering is by keywords, so it will sometimes be wrong. The file says why each job matched, which makes the term to change easy to find.

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
  monitor.py           one check: search, read, decide, send
  storage.py           what has been seen (SQLite)
  formatting.py        the list message and the text file
  telegram.py          posting, and the /start and /status commands
  console.py           output for the dry run
```
