# YouTube setup, step by step

This connects shorts-engine to a YouTube channel. Steps 1–2 are scripted. Steps 3–5 need the Google Cloud Console in a browser. Step 7, the API audit, is a form that Google reviews.

Use the **Google account that owns the channel** for every step. If you're signed in to several Google accounts, check the avatar in the top-right of each Console page. A `&authuser=<email>` URL parameter forces the account.

---

## 1. Project, API and API key (scripted)

```bash
deploy/gcloud-setup.sh <google-account-email>   # optional 2nd arg: existing project id
```

The script:
- uses its own gcloud configuration (`shorts-engine`, override with `GCLOUD_CONFIG`), so your default gcloud account and project stay untouched
- creates a dedicated project, e.g. `shorts-engine-xxxxxx` (override with `GCLOUD_PROJECT` or `PROJECT_PREFIX`; the API audit is per project, so keep it dedicated)
- enables **YouTube Data API v3**
- creates an API key **restricted to the YouTube Data API** and writes it to `.env` as `YOUTUBE_API_KEY` (never printed, file is `chmod 600`)

The API key is only used for read-only trend lookups. It can't upload anything.

## 2. Check it

```bash
.venv/bin/shorts-engine doctor
```

`API key works (channel lookup: <your channel>)` should show **ok**. The OAuth lines stay red until steps 3–6 are done.

## 3. OAuth consent screen (Google Auth Platform)

Open `https://console.cloud.google.com/auth/overview?project=<project-id>` and click **Get started**.

| Wizard step | Value |
|---|---|
| App information → App name | `Shorts Engine` (any name) |
| App information → User support email | the channel owner's email |
| Audience | **External** (Internal only exists for Workspace orgs) |
| Contact information | the channel owner's email |
| Finish | tick the *Google API Services: User Data Policy* box → **Continue** → **Create** |

Don't upload a logo and don't add scopes under *Data access*. Either one triggers Google's brand or scope verification review. The tool requests its scopes at login.

## 4. Publish the app (important)

**Audience** → *Publishing status: Testing* → **Publish app** → **Confirm**.

While the app is in *Testing*, Google expires refresh tokens after **7 days**, so the daemon would lose its login before the campaign ends. In production but unverified, the login lasts. The only cost is an "unverified app" warning at login (step 6) and a 100-user cap, which doesn't matter for a single-owner tool.

## 5. Desktop OAuth client

**Clients** → **+ Create client**
- Application type: **Desktop app**
- Name: `shorts-engine`
- **Create** → in the pop-up click **Download JSON**

Google shows the client secret **only at creation**. If you close the pop-up without downloading, delete the client and create a new one.

Save the file as:

```
secrets/client_secret.json
```

## 6. Log in as the channel

```bash
.venv/bin/shorts-engine auth
```

A browser window opens:
1. Pick the Google account that owns the channel. `youtube.account` in `config.yaml` pre-selects it.
2. If the account has several channels (Brand Accounts), **pick the target channel**. The upload login is tied to the channel you choose here.
3. *"Google hasn't verified this app"* → **Advanced** → **Go to <your app name> (unsafe)**. It's your own app.
4. Allow both permissions: *manage your YouTube videos (upload)* and *view your YouTube account*.

The command then prints the channel it logged in to. If that doesn't match `youtube.channel_id`, it deletes the login and stops. Uploads also re-check the channel before every batch, so nothing can go to the wrong channel.

## 7. API audit (start today, it takes weeks)

Uploads from unaudited API projects are **locked to private**: the scheduled `publishAt` never makes them public. Only the audit removes that.

Form: <https://support.google.com/youtube/contact/yt_api_form> → *"I'd like to request an API audit / quota extension"*.

Have these ready:
- **Project number**: Console home page, or `CLOUDSDK_ACTIVE_CONFIG_NAME=shorts-engine gcloud projects describe <project-id> --format='value(projectNumber)'`
- **Channel**: URL and ID (`youtube.channel_id`)
- **Use case**, described honestly. For example: *"Internal, single-user command-line tool. It uploads and schedules Shorts to my own channel only: clips cut from my own long-form videos, plus AI-assisted explainers that I review before publishing. No third-party users, and no data from other users is stored."*
- **API methods used**: `videos.insert`, `videos.list`, `channels.list` (mine), `search.list`
- **Data handling**: OAuth token stored locally with `chmod 600`. Upload stats are kept in a local SQLite file. Nothing is shared.
- **Screenshots or a short screen recording** of `shorts-engine auth`, `run --dry-run` and the resulting upload. Reviewers often ask for these.
- Links to a **privacy policy** and **terms of service**, if the form asks. A simple page on your site or a GitHub Pages README works for an internal tool.

Answer any follow-up emails from the YouTube API team promptly. An unanswered question stalls the review.

## 8. Quota

The defaults per project per day are **100 `videos.insert` calls**, **100 `search.list` calls**, and 10,000 units for everything else. They reset at midnight Pacific Time.

- The default campaign ramp (1 → 3 uploads/day) is far below this. You only need an extension (same form) to go above 100.
- Trend refreshes use `len(trends.search_keywords)` search calls each. With 14 keywords every 6 h that's about 56 a day. `doctor` warns if you go over 100.
- If a limit is hit, the uploader stops cleanly, keeps the clips *ready*, and retries on the next top-up.

## 9. Finish the rest

```bash
# AI originals + spoken commentary (recommended): llm.provider: openai|azure in config.yaml, key in .env
# Source videos for clips: copy your long-form recordings into data/inbox/
.venv/bin/shorts-engine doctor           # everything ok?
.venv/bin/shorts-engine run --dry-run    # renders today's batch, shows the schedule, uploads nothing
.venv/bin/shorts-engine run              # first real upload batch
```

Then keep it running with `deploy/com.shorts-engine.daemon.plist` (macOS) or `deploy/shorts-engine.service` (Linux).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Error 403: access_denied` at login | The app is still in Testing and your account isn't a test user. Publish it (step 4) or add yourself under Audience → Test users |
| Login works but `channel ... config expects UC...` | You picked another channel or account in step 6. Run `auth` again and pick the right one |
| `YouTube login expired or revoked` | The app is still in Testing (7-day tokens), or you removed access in your Google account. Publish the app, then `auth` again |
| Uploads stay private after their publish time | The API audit isn't approved yet (step 7) |
| `quotaExceeded` / `uploadLimitExceeded` | Daily limit reached. Clips stay ready and are retried after the reset (midnight Pacific Time) |
| Console page says you lack permission | You're signed in to a different Google account in that tab |
