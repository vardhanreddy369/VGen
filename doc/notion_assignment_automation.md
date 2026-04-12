# Assignment -> Notion Automation

This adds a webhook + CLI tool at:

- `tools/notion_assignment_sync.py`

It creates assignment pages in your Notion database.

Supported modes:

- Generic webhook (`serve`)
- One-time manual insert (`send`)
- Canvas sync (`sync-canvas`)

Canvas sync behavior (default):

- skips assignments with no due date
- maps Canvas statuses into your Notion `Status`
- auto-fills `Priority`, `Due Bucket`, and `Course Tag` when those columns exist
- auto-fills/maintains `Needs Submission` and `Priority Rank` when those columns exist
- keeps manually completed items in completed state on future syncs

## 1) Create/prepare your Notion database

Create a Notion database (table) with at least:

- `Name` (type: `Title`) - required

Recommended optional properties:

- `Due` (type: `Date`)
- `Course` (type: `Select`, `Multi-select`, or `Text`)
- `Status` (type: `Status`, `Select`, or `Text`)
- `Source URL` (type: `URL` or `Text`)
- `Notes` (type: `Text`)
- `External ID` (type: `Text` or `Number`) for duplicate protection

Share the database with your Notion integration.

## 2) Environment variables

Set these before running:

```bash
export NOTION_TOKEN="secret_xxx"
export NOTION_DATABASE_ID="your_database_id"
```

Optional overrides (if your column names are different):

```bash
export NOTION_TITLE_PROPERTY="Task Name"
export NOTION_DUE_DATE_PROPERTY="Deadline"
export NOTION_COURSE_PROPERTY="Subject"
export NOTION_STATUS_PROPERTY="Progress"
export NOTION_SOURCE_URL_PROPERTY="Link"
export NOTION_NOTES_PROPERTY="Details"
export NOTION_EXTERNAL_ID_PROPERTY="Assignment Key"
```

## 3) Test with one assignment (CLI mode)

```bash
python tools/notion_assignment_sync.py send \
  --title "Math HW 5" \
  --due-date "2026-04-20" \
  --course "Math" \
  --status "Not started" \
  --source-url "https://classroom.example.com/hw5" \
  --notes "Do questions 1-10" \
  --external-id "classroom-hw5"
```

## 4) Run webhook mode

```bash
python tools/notion_assignment_sync.py serve --host 0.0.0.0 --port 8787
```

Health check:

```bash
curl http://localhost:8787/health
```

Create assignment through webhook:

```bash
curl -X POST http://localhost:8787/assignment \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Physics Lab Report",
    "due_date": "2026-04-22",
    "course": "Physics",
    "status": "Not started",
    "source_url": "https://lms.example.com/lab-3",
    "notes": "Include graphs",
    "external_id": "lms-physics-lab-3"
  }'
```

## 5) Connect your assignment source

Point your source app to:

- `POST http://<your-machine-or-server>:8787/assignment`

Any app that can send a webhook will work (Zapier, n8n, Make, Google Apps Script, LMS automation, email parser, etc.).

## 6) Sync directly from Canvas (UCF Webcourses)

Set Canvas token (from Canvas -> Account -> Settings -> New Access Token):

```bash
export CANVAS_BASE_URL="https://webcourses.ucf.edu"
export CANVAS_API_TOKEN="your_canvas_token"
```

Run sync:

```bash
python tools/notion_assignment_sync.py sync-canvas
```

Optional:

```bash
# include old assignments
python tools/notion_assignment_sync.py sync-canvas --include-past

# only specific courses (repeat flag)
python tools/notion_assignment_sync.py sync-canvas --course-id 12345 --course-id 67890

# preview only
python tools/notion_assignment_sync.py sync-canvas --dry-run

# include assignments with no due date
python tools/notion_assignment_sync.py sync-canvas --include-no-due
```

Important:

- For `sync-canvas`, your Notion database should have `External ID` as a text property.
- Script uses `External ID` to upsert, so reruns update existing rows instead of duplicating.
