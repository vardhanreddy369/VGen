# UCF Canvas -> Notion Quickstart

This syncs assignments from `https://webcourses.ucf.edu` into your Notion database.

## 1) Create your Notion integration

- Go to Notion integrations and create an internal integration.
- Copy the integration token.
- Open your assignments database in Notion and click **Share**.
- Invite your integration to that database.
- Copy the database ID from the database URL.

## 2) Ensure database columns exist

Required:

- `Name` (Title)
- `External ID` (Text)
- `Status` (Status)
- `Priority` (Select)
- `Due Bucket` (Select)
- `Course Tag` (Select or Text)
- `Needs Submission` (Formula or Checkbox)
- `Priority Rank` (Formula or Number)

Recommended:

- `Due` (Date)
- `Course` (Select/Text)
- `Status` (Status/Select/Text)
- `Source URL` (URL/Text)
- `Notes` (Text)

## 3) Create Canvas API token

- Open [webcourses.ucf.edu](https://webcourses.ucf.edu)
- `Account -> Settings -> Approved Integrations -> + New Access Token`
- Copy the token

## 4) Set env vars

```bash
export NOTION_TOKEN="secret_xxx"
export NOTION_DATABASE_ID="your_database_id"
export CANVAS_BASE_URL="https://webcourses.ucf.edu"
export CANVAS_API_TOKEN="your_canvas_token"
```

## 5) Run sync

```bash
python tools/notion_assignment_sync.py sync-canvas
```

By default, sync skips assignments without due dates.
Use `--include-no-due` if you want undated assignments too.

## 6) Optional recurring sync (every 15 min)

```bash
*/15 * * * * cd /Users/srivardhanreddygutta/VGen && /usr/bin/env python tools/notion_assignment_sync.py sync-canvas >> /tmp/canvas_notion_sync.log 2>&1
```

## 7) Recommended views (one-time in Notion UI)

Create view `All Assignments`:

- Filter: `Needs Submission` is checked
- Sort 1: `Due` ascending
- Sort 2: `Priority Rank` ascending

Create view `Completed`:

- Filter: `Needs Submission` is unchecked
- Sort: `Due` descending
