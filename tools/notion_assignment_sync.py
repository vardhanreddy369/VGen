#!/usr/bin/env python3
"""Send assignments to a Notion database (CLI + webhook server)."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen


LOGGER = logging.getLogger("notion-assignment-sync")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

NOTION_BASE_URL = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


class NotionAPIError(RuntimeError):
    """Raised when a Notion API call fails."""


class CanvasAPIError(RuntimeError):
    """Raised when a Canvas API call fails."""


@dataclass
class AssignmentPayload:
    title: str
    due_date: Optional[str] = None
    course: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    due_bucket: Optional[str] = None
    needs_submission: Optional[bool] = None
    priority_rank: Optional[int] = None
    source_url: Optional[str] = None
    notes: Optional[str] = None
    external_id: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AssignmentPayload":
        title = _optional_string(data.get("title"))
        if not title:
            raise ValueError("`title` is required.")

        return cls(
            title=title,
            due_date=_optional_string(data.get("due_date")),
            course=_optional_string(data.get("course")),
            status=_optional_string(data.get("status")),
            priority=_optional_string(data.get("priority")),
            due_bucket=_optional_string(data.get("due_bucket")),
            needs_submission=_optional_bool(data.get("needs_submission")),
            priority_rank=_optional_int(data.get("priority_rank")),
            source_url=_optional_string(data.get("source_url")),
            notes=_optional_string(data.get("notes")),
            external_id=_optional_string(data.get("external_id")),
        )


@dataclass
class PropertyMapping:
    title: Tuple[str, str]
    due_date: Optional[Tuple[str, str]]
    course: Optional[Tuple[str, str]]
    status: Optional[Tuple[str, str]]
    priority: Optional[Tuple[str, str]]
    due_bucket: Optional[Tuple[str, str]]
    needs_submission: Optional[Tuple[str, str]]
    priority_rank: Optional[Tuple[str, str]]
    source_url: Optional[Tuple[str, str]]
    notes: Optional[Tuple[str, str]]
    external_id: Optional[Tuple[str, str]]
    status_options: Optional[List[str]] = None
    priority_options: Optional[List[str]] = None
    due_bucket_options: Optional[List[str]] = None


@dataclass
class CanvasCourse:
    id: int
    name: str


@dataclass
class CanvasAssignment:
    course_id: int
    course_name: str
    assignment_id: int
    title: str
    due_at: Optional[str]
    source_url: Optional[str]
    status: str
    notes: Optional[str]

    @property
    def external_id(self) -> str:
        return f"canvas:{self.course_id}:{self.assignment_id}"


class NotionClient:
    def __init__(
        self,
        token: str,
        database_id: str,
        notion_version: str = NOTION_VERSION,
        timeout_seconds: int = 20,
    ) -> None:
        self.token = token
        self.database_id = database_id
        self.notion_version = notion_version
        self.timeout_seconds = timeout_seconds

    def get_database_properties(self) -> Dict[str, Dict[str, Any]]:
        response = self._request("GET", f"/databases/{self.database_id}")
        properties = response.get("properties")
        if not isinstance(properties, dict):
            raise NotionAPIError("Could not read database properties from Notion.")
        return properties

    def query_database(
        self,
        filter_payload: Optional[Dict[str, Any]] = None,
        page_size: int = 1,
        start_cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"page_size": page_size}
        if filter_payload:
            payload["filter"] = filter_payload
        if start_cursor:
            payload["start_cursor"] = start_cursor
        return self._request("POST", f"/databases/{self.database_id}/query", payload)

    def create_page(self, properties: Dict[str, Any]) -> Dict[str, Any]:
        payload = {"parent": {"database_id": self.database_id}, "properties": properties}
        return self._request("POST", "/pages", payload)

    def update_page(self, page_id: str, properties: Dict[str, Any]) -> Dict[str, Any]:
        payload = {"properties": properties}
        return self._request("PATCH", f"/pages/{page_id}", payload)

    def _request(
        self, method: str, path: str, payload: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        url = f"{NOTION_BASE_URL}{path}"
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": self.notion_version,
            "Content-Type": "application/json",
        }

        request = Request(url=url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as error:
            details = error.read().decode("utf-8", errors="ignore")
            message = _extract_notion_error(details)
            raise NotionAPIError(f"Notion API error ({error.code}): {message}") from error
        except URLError as error:
            raise NotionAPIError(f"Notion API request failed: {error.reason}") from error

        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as error:
            raise NotionAPIError("Notion returned invalid JSON.") from error


class CanvasClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        timeout_seconds: int = 20,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds

    def list_courses(self, include_course_ids: Optional[Sequence[int]] = None) -> List[CanvasCourse]:
        include_ids = set(include_course_ids or [])
        raw_courses = self._get_paginated(
            "/api/v1/courses",
            params=[
                ("enrollment_type", "student"),
                ("enrollment_state", "active"),
                ("per_page", "100"),
            ],
        )

        courses: List[CanvasCourse] = []
        for raw in raw_courses:
            if not isinstance(raw, dict):
                continue
            course_id = raw.get("id")
            if not isinstance(course_id, int):
                continue
            if include_ids and course_id not in include_ids:
                continue

            name = _optional_string(raw.get("name")) or f"Course {course_id}"
            courses.append(CanvasCourse(id=course_id, name=name))
        return courses

    def list_assignments(
        self,
        course: CanvasCourse,
        include_past: bool = False,
    ) -> List[CanvasAssignment]:
        params: list[Tuple[str, str]] = [
            ("include[]", "submission"),
            ("per_page", "100"),
        ]

        raw_assignments = self._get_paginated(
            f"/api/v1/courses/{course.id}/assignments",
            params=params,
        )

        assignments: List[CanvasAssignment] = []
        seen_assignment_ids: set[int] = set()
        now_utc = datetime.now(timezone.utc)
        for raw in raw_assignments:
            if not isinstance(raw, dict):
                continue
            assignment_id = raw.get("id")
            if not isinstance(assignment_id, int):
                continue
            if assignment_id in seen_assignment_ids:
                continue
            seen_assignment_ids.add(assignment_id)

            title = _optional_string(raw.get("name"))
            if not title:
                continue

            due_at = _optional_string(raw.get("due_at"))
            # Keep old assignments out even if Canvas ignores bucket filters.
            if not include_past and due_at:
                due_at_dt = _parse_datetime(due_at)
                if due_at_dt and due_at_dt < now_utc:
                    continue

            source_url = _optional_string(raw.get("html_url"))
            status = _canvas_status_from_submission(raw.get("submission"))
            notes = _canvas_notes(raw)
            assignments.append(
                CanvasAssignment(
                    course_id=course.id,
                    course_name=course.name,
                    assignment_id=assignment_id,
                    title=title,
                    due_at=due_at,
                    source_url=source_url,
                    status=status,
                    notes=notes,
                )
            )
        return assignments

    def _get_paginated(
        self, path: str, params: Optional[Sequence[Tuple[str, str]]] = None
    ) -> List[Any]:
        results: List[Any] = []
        next_url = self._build_url(path, params)

        while next_url:
            payload, headers = self._request_json("GET", next_url)
            if not isinstance(payload, list):
                raise CanvasAPIError("Canvas returned a non-list payload for a list endpoint.")
            results.extend(payload)
            next_url = _extract_next_link(headers.get("Link"))
        return results

    def _build_url(
        self, path: str, params: Optional[Sequence[Tuple[str, str]]] = None
    ) -> str:
        url = urljoin(f"{self.base_url}/", path.lstrip("/"))
        if not params:
            return url
        return f"{url}?{urlencode(params, doseq=True)}"

    def _request_json(self, method: str, url: str) -> Tuple[Any, Dict[str, str]]:
        headers = {"Authorization": f"Bearer {self.token}"}
        request = Request(url=url, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
                response_headers = dict(response.headers.items())
        except HTTPError as error:
            details = error.read().decode("utf-8", errors="ignore")
            message = _extract_canvas_error(details)
            raise CanvasAPIError(f"Canvas API error ({error.code}): {message}") from error
        except URLError as error:
            raise CanvasAPIError(f"Canvas API request failed: {error.reason}") from error

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            raise CanvasAPIError("Canvas returned invalid JSON.") from error
        return payload, response_headers


class AssignmentSyncService:
    def __init__(self, notion: NotionClient, mapping: PropertyMapping) -> None:
        self.notion = notion
        self.mapping = mapping

    def create_assignment(self, payload: AssignmentPayload) -> Dict[str, Any]:
        existing = self._find_existing_assignment(payload.external_id)
        if existing:
            return {
                "created": False,
                "page_id": existing.get("id"),
                "url": existing.get("url"),
                "reason": "external_id already exists",
            }

        properties = self._build_notion_properties(payload)
        page = self.notion.create_page(properties)
        return {"created": True, "page_id": page.get("id"), "url": page.get("url")}

    def upsert_assignment(self, payload: AssignmentPayload) -> Dict[str, Any]:
        existing = self._find_existing_assignment(payload.external_id)
        if existing:
            existing_status = _read_page_property_text(existing, self.mapping.status)
            if existing_status and _is_completed_status(existing_status):
                # Respect manual completion in Notion; keep row in completed state.
                payload.status = existing_status
                payload.priority = "Low"
                payload.due_bucket = "Completed"
                payload.needs_submission = False
                payload.priority_rank = 4

        properties = self._build_notion_properties(payload)
        if existing:
            page_id = _optional_string(existing.get("id"))
            if not page_id:
                raise NotionAPIError("Found a matching page without an ID.")
            page = self.notion.update_page(page_id, properties)
            return {"created": False, "updated": True, "page_id": page.get("id"), "url": page.get("url")}

        page = self.notion.create_page(properties)
        return {"created": True, "updated": False, "page_id": page.get("id"), "url": page.get("url")}

    def _build_notion_properties(self, payload: AssignmentPayload) -> Dict[str, Any]:
        properties: Dict[str, Any] = {}

        title_name, _title_type = self.mapping.title
        properties[title_name] = {"title": _to_rich_text(payload.title)}

        if payload.due_date and self.mapping.due_date:
            prop_name, _prop_type = self.mapping.due_date
            properties[prop_name] = {"date": {"start": payload.due_date}}

        if payload.course and self.mapping.course:
            prop_name, prop_type = self.mapping.course
            properties[prop_name] = _value_by_property_type(prop_type, payload.course)

        if payload.status and self.mapping.status:
            prop_name, prop_type = self.mapping.status
            properties[prop_name] = _value_by_property_type(
                prop_type,
                payload.status,
                allowed_options=self.mapping.status_options,
            )

        if payload.priority and self.mapping.priority:
            prop_name, prop_type = self.mapping.priority
            properties[prop_name] = _value_by_property_type(
                prop_type,
                payload.priority,
                allowed_options=self.mapping.priority_options,
            )

        if payload.due_bucket and self.mapping.due_bucket:
            prop_name, prop_type = self.mapping.due_bucket
            properties[prop_name] = _value_by_property_type(
                prop_type,
                payload.due_bucket,
                allowed_options=self.mapping.due_bucket_options,
            )

        if payload.needs_submission is not None and self.mapping.needs_submission:
            prop_name, prop_type = self.mapping.needs_submission
            if prop_type == "checkbox":
                properties[prop_name] = {"checkbox": payload.needs_submission}

        if payload.priority_rank is not None and self.mapping.priority_rank:
            prop_name, prop_type = self.mapping.priority_rank
            if prop_type == "number":
                properties[prop_name] = {"number": payload.priority_rank}

        if payload.source_url and self.mapping.source_url:
            prop_name, prop_type = self.mapping.source_url
            properties[prop_name] = _value_by_property_type(prop_type, payload.source_url)

        if payload.notes and self.mapping.notes:
            prop_name, prop_type = self.mapping.notes
            properties[prop_name] = _value_by_property_type(prop_type, payload.notes)

        if payload.external_id and self.mapping.external_id:
            prop_name, prop_type = self.mapping.external_id
            properties[prop_name] = _value_by_property_type(prop_type, payload.external_id)

        return properties

    def _find_existing_assignment(self, external_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if not external_id or not self.mapping.external_id:
            return None

        prop_name, prop_type = self.mapping.external_id
        filter_payload = _build_external_id_filter(prop_name, prop_type, external_id)
        if not filter_payload:
            return None

        response = self.notion.query_database(filter_payload, page_size=1)
        results = response.get("results", [])
        if results:
            first = results[0]
            if isinstance(first, dict):
                return first
        return None


def load_service_from_env() -> AssignmentSyncService:
    token = _optional_string(os.getenv("NOTION_TOKEN"))
    database_id = _optional_string(os.getenv("NOTION_DATABASE_ID"))
    if not token or not database_id:
        raise ValueError("Set NOTION_TOKEN and NOTION_DATABASE_ID before running.")

    client = NotionClient(token=token, database_id=database_id)
    properties = client.get_database_properties()
    mapping = detect_property_mapping(properties)

    LOGGER.info("Detected title property: %s (%s)", mapping.title[0], mapping.title[1])
    _log_optional_mapping("Due date", mapping.due_date)
    _log_optional_mapping("Course", mapping.course)
    _log_optional_mapping("Status", mapping.status)
    _log_optional_mapping("Priority", mapping.priority)
    _log_optional_mapping("Due Bucket", mapping.due_bucket)
    _log_optional_mapping("Needs Submission", mapping.needs_submission)
    _log_optional_mapping("Priority Rank", mapping.priority_rank)
    _log_optional_mapping("Source URL", mapping.source_url)
    _log_optional_mapping("Notes", mapping.notes)
    _log_optional_mapping("External ID", mapping.external_id)

    return AssignmentSyncService(notion=client, mapping=mapping)


def sync_canvas_to_notion(
    service: AssignmentSyncService,
    canvas: CanvasClient,
    include_past: bool = False,
    include_no_due: bool = False,
    include_course_ids: Optional[Sequence[int]] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    if not service.mapping.external_id:
        raise ValueError(
            "Canvas sync requires an External ID property in Notion. "
            "Create a text column (recommended name: External ID)."
        )
    if service.mapping.external_id[1] != "rich_text":
        raise ValueError(
            "Canvas sync needs External ID to be a text property (rich_text)."
        )

    courses = canvas.list_courses(include_course_ids=include_course_ids)
    LOGGER.info("Found %d active courses in Canvas.", len(courses))

    summary: Dict[str, Any] = {
        "courses_scanned": len(courses),
        "assignments_seen": 0,
        "created": 0,
        "updated": 0,
        "errors": 0,
        "skipped_no_due": 0,
        "dry_run": dry_run,
    }

    for course in courses:
        try:
            assignments = canvas.list_assignments(course=course, include_past=include_past)
        except CanvasAPIError:
            summary["errors"] += 1
            LOGGER.exception("Failed fetching assignments for course %s", course.name)
            continue
        LOGGER.info("Course %s: %d assignments to sync.", course.name, len(assignments))
        for assignment in assignments:
            if not include_no_due and not assignment.due_at:
                summary["skipped_no_due"] += 1
                continue

            summary["assignments_seen"] += 1
            notion_status = _map_canvas_status_to_notion(
                canvas_status=assignment.status,
                mapping=service.mapping,
            )
            priority, due_bucket = _classify_priority_and_due_bucket(
                due_at=assignment.due_at,
                notion_status=notion_status,
            )
            needs_submission = _is_submission_needed(
                due_at=assignment.due_at,
                notion_status=notion_status,
            )
            priority_rank = _priority_to_rank(priority)
            payload = AssignmentPayload(
                title=assignment.title,
                due_date=assignment.due_at,
                course=assignment.course_name,
                status=notion_status,
                priority=priority,
                due_bucket=due_bucket,
                needs_submission=needs_submission,
                priority_rank=priority_rank,
                source_url=assignment.source_url,
                notes=assignment.notes,
                external_id=assignment.external_id,
            )
            if dry_run:
                continue

            try:
                result = service.upsert_assignment(payload)
            except (NotionAPIError, ValueError):
                summary["errors"] += 1
                LOGGER.exception(
                    "Failed syncing assignment '%s' from %s",
                    assignment.title,
                    assignment.course_name,
                )
                continue

            if result.get("created"):
                summary["created"] += 1
            else:
                summary["updated"] += 1

    return summary


def detect_property_mapping(properties: Dict[str, Dict[str, Any]]) -> PropertyMapping:
    title = _detect_property(
        properties=properties,
        env_name="NOTION_TITLE_PROPERTY",
        allowed_types=("title",),
        candidates=("Name", "Title", "Assignment"),
        required=True,
    )

    due_date = _detect_property(
        properties=properties,
        env_name="NOTION_DUE_DATE_PROPERTY",
        allowed_types=("date",),
        candidates=("Due", "Due Date", "Deadline"),
    )

    course = _detect_property(
        properties=properties,
        env_name="NOTION_COURSE_PROPERTY",
        allowed_types=("select", "multi_select", "rich_text"),
        candidates=("Course Tag", "Course", "Class", "Subject"),
    )

    status = _detect_property(
        properties=properties,
        env_name="NOTION_STATUS_PROPERTY",
        allowed_types=("status", "select", "rich_text"),
        candidates=("Status", "State"),
    )

    priority = _detect_property(
        properties=properties,
        env_name="NOTION_PRIORITY_PROPERTY",
        allowed_types=("status", "select", "rich_text"),
        candidates=("Priority",),
    )

    due_bucket = _detect_property(
        properties=properties,
        env_name="NOTION_DUE_BUCKET_PROPERTY",
        allowed_types=("status", "select", "rich_text"),
        candidates=("Due Bucket", "Due Status"),
    )

    needs_submission = _detect_property(
        properties=properties,
        env_name="NOTION_NEEDS_SUBMISSION_PROPERTY",
        allowed_types=("checkbox", "formula"),
        candidates=("Needs Submission", "Submit Now"),
    )

    priority_rank = _detect_property(
        properties=properties,
        env_name="NOTION_PRIORITY_RANK_PROPERTY",
        allowed_types=("number", "formula"),
        candidates=("Priority Rank", "Submission Rank"),
    )

    source_url = _detect_property(
        properties=properties,
        env_name="NOTION_SOURCE_URL_PROPERTY",
        allowed_types=("url", "rich_text"),
        candidates=("Source URL", "Source", "Link", "URL"),
    )

    notes = _detect_property(
        properties=properties,
        env_name="NOTION_NOTES_PROPERTY",
        allowed_types=("rich_text",),
        candidates=("Notes", "Details", "Description"),
    )

    external_id = _detect_property(
        properties=properties,
        env_name="NOTION_EXTERNAL_ID_PROPERTY",
        allowed_types=("rich_text", "number"),
        candidates=("External ID", "ExternalId", "Assignment ID", "AssignmentID"),
    )

    return PropertyMapping(
        title=title,
        due_date=due_date,
        course=course,
        status=status,
        priority=priority,
        due_bucket=due_bucket,
        needs_submission=needs_submission,
        priority_rank=priority_rank,
        source_url=source_url,
        notes=notes,
        external_id=external_id,
        status_options=_property_options(properties, status),
        priority_options=_property_options(properties, priority),
        due_bucket_options=_property_options(properties, due_bucket),
    )


def _detect_property(
    properties: Dict[str, Dict[str, Any]],
    env_name: str,
    allowed_types: Sequence[str],
    candidates: Sequence[str],
    required: bool = False,
) -> Optional[Tuple[str, str]]:
    explicit_name = _optional_string(os.getenv(env_name))
    if explicit_name:
        explicit_property = properties.get(explicit_name)
        if not explicit_property:
            raise ValueError(
                f"{env_name} is set to '{explicit_name}', but that property is missing."
            )
        explicit_type = explicit_property.get("type")
        if explicit_type not in allowed_types:
            allowed = ", ".join(allowed_types)
            raise ValueError(
                f"{env_name} points to '{explicit_name}', but its type is "
                f"'{explicit_type}' (expected: {allowed})."
            )
        return explicit_name, explicit_type

    for candidate in candidates:
        maybe = properties.get(candidate)
        if maybe and maybe.get("type") in allowed_types:
            return candidate, maybe["type"]

    normalized_candidates = {_normalize(value) for value in candidates}
    for name, definition in properties.items():
        prop_type = definition.get("type")
        if prop_type not in allowed_types:
            continue
        if _normalize(name) in normalized_candidates:
            return name, prop_type

    if "title" in allowed_types:
        for name, definition in properties.items():
            if definition.get("type") == "title":
                return name, "title"

    if required:
        allowed = ", ".join(allowed_types)
        raise ValueError(
            f"Could not map required property. Expected one of [{allowed}] for {env_name}."
        )
    return None


def _build_external_id_filter(
    prop_name: str, prop_type: str, external_id: str
) -> Optional[Dict[str, Any]]:
    if prop_type == "rich_text":
        return {"property": prop_name, "rich_text": {"equals": external_id}}

    if prop_type == "number":
        try:
            number_value: float | int
            if "." in external_id:
                number_value = float(external_id)
            else:
                number_value = int(external_id)
        except ValueError:
            LOGGER.warning(
                "External ID '%s' is not numeric, so duplicate check was skipped.",
                external_id,
            )
            return None
        return {"property": prop_name, "number": {"equals": number_value}}

    return None


def _property_options(
    properties: Dict[str, Dict[str, Any]],
    mapping: Optional[Tuple[str, str]],
) -> Optional[List[str]]:
    if not mapping:
        return None
    prop_name, prop_type = mapping
    definition = properties.get(prop_name, {})
    if not isinstance(definition, dict):
        return None

    if prop_type == "status":
        options = definition.get("status", {}).get("options", [])
    elif prop_type == "select":
        options = definition.get("select", {}).get("options", [])
    elif prop_type == "multi_select":
        options = definition.get("multi_select", {}).get("options", [])
    else:
        return None

    names: List[str] = []
    for option in options:
        if not isinstance(option, dict):
            continue
        name = _optional_string(option.get("name"))
        if name:
            names.append(name)
    return names or None


def _pick_allowed_option(value: str, allowed_options: Sequence[str]) -> str:
    exact_match = {item: item for item in allowed_options}
    if value in exact_match:
        return value

    lower_lookup = {item.lower(): item for item in allowed_options}
    lowered = value.lower()
    if lowered in lower_lookup:
        return lower_lookup[lowered]

    return allowed_options[0]


def _value_by_property_type(
    prop_type: str,
    value: str,
    allowed_options: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    if prop_type == "rich_text":
        return {"rich_text": _to_rich_text(value)}
    if prop_type == "select":
        selected_value = (
            _pick_allowed_option(value, allowed_options) if allowed_options else value
        )
        return {"select": {"name": selected_value}}
    if prop_type == "multi_select":
        selected_value = (
            _pick_allowed_option(value, allowed_options) if allowed_options else value
        )
        return {"multi_select": [{"name": selected_value}]}
    if prop_type == "status":
        selected_value = (
            _pick_allowed_option(value, allowed_options) if allowed_options else value
        )
        return {"status": {"name": selected_value}}
    if prop_type == "url":
        return {"url": value}
    if prop_type == "number":
        try:
            number_value: float | int
            if "." in value:
                number_value = float(value)
            else:
                number_value = int(value)
        except ValueError:
            raise ValueError(f"'{value}' is not numeric for a number property.") from None
        return {"number": number_value}
    raise ValueError(f"Unsupported Notion property type: {prop_type}")


def _to_rich_text(text: str) -> list[Dict[str, Any]]:
    return [{"type": "text", "text": {"content": text}}]


def _extract_notion_error(raw_error: str) -> str:
    if not raw_error:
        return "unknown error"
    try:
        payload = json.loads(raw_error)
    except json.JSONDecodeError:
        return raw_error
    return str(payload.get("message") or payload.get("code") or raw_error)


def _extract_canvas_error(raw_error: str) -> str:
    if not raw_error:
        return "unknown error"
    try:
        payload = json.loads(raw_error)
    except json.JSONDecodeError:
        return raw_error
    if isinstance(payload, dict):
        if "errors" in payload and isinstance(payload["errors"], list):
            messages = []
            for item in payload["errors"]:
                if isinstance(item, dict):
                    messages.append(str(item.get("message") or item))
                else:
                    messages.append(str(item))
            return "; ".join(messages)
        return str(payload.get("message") or payload.get("error") or raw_error)
    return raw_error


def _extract_next_link(link_header: Optional[str]) -> Optional[str]:
    if not link_header:
        return None

    parts = [part.strip() for part in link_header.split(",")]
    for part in parts:
        sections = [section.strip() for section in part.split(";")]
        if len(sections) < 2:
            continue
        url_part = sections[0]
        rel_parts = sections[1:]
        if 'rel="next"' not in rel_parts:
            continue
        if url_part.startswith("<") and url_part.endswith(">"):
            return url_part[1:-1]
    return None


def _parse_datetime(value: str) -> Optional[datetime]:
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _canvas_status_from_submission(submission: Any) -> str:
    if not isinstance(submission, dict):
        return "Not submitted"

    if submission.get("excused"):
        return "Excused"
    if submission.get("missing"):
        return "Missing"
    if submission.get("late"):
        return "Late"

    workflow_state = _optional_string(submission.get("workflow_state"))
    if workflow_state in {"graded"}:
        return "Graded"
    if workflow_state in {"submitted", "pending_review"}:
        return "Submitted"
    if workflow_state in {"unsubmitted"}:
        return "Not submitted"
    return "Not submitted"


def _map_canvas_status_to_notion(canvas_status: str, mapping: PropertyMapping) -> str:
    normalized = canvas_status.strip().lower()
    if normalized in {"graded", "excused"}:
        preferred = ["Done", "Completed", "Complete", "Submitted", "In progress"]
    elif normalized in {"submitted", "pending_review"}:
        preferred = ["Submitted", "In progress", "Done", "Completed", "Complete"]
    elif normalized in {"late", "missing"}:
        preferred = ["In progress", "Not started", "To do", "Todo"]
    else:
        preferred = ["Not started", "To do", "Todo", "In progress"]

    options = mapping.status_options or []
    if not options:
        return preferred[0]

    option_lookup = {option.lower(): option for option in options}
    for candidate in preferred:
        match = option_lookup.get(candidate.lower())
        if match:
            return match
    return options[0]


def _read_page_property_text(
    page: Dict[str, Any], mapping: Optional[Tuple[str, str]]
) -> Optional[str]:
    if not mapping:
        return None
    prop_name, prop_type = mapping
    properties = page.get("properties", {})
    if not isinstance(properties, dict):
        return None
    prop = properties.get(prop_name)
    if not isinstance(prop, dict):
        return None

    if prop_type == "status":
        return _optional_string((prop.get("status") or {}).get("name"))
    if prop_type == "select":
        return _optional_string((prop.get("select") or {}).get("name"))
    if prop_type == "rich_text":
        rich_text = prop.get("rich_text") or []
        if rich_text and isinstance(rich_text, list) and isinstance(rich_text[0], dict):
            return _optional_string(rich_text[0].get("plain_text"))
    return None


def _is_completed_status(status_name: str) -> bool:
    return status_name.strip().lower() in {
        "done",
        "submitted",
        "complete",
        "completed",
    }


def _classify_priority_and_due_bucket(
    due_at: Optional[str], notion_status: str
) -> Tuple[str, str]:
    status_normalized = notion_status.strip().lower()
    if status_normalized in {"done", "submitted", "complete", "completed"}:
        return "Low", "Completed"

    if not due_at:
        return "Low", "Upcoming"

    due_dt = _parse_datetime(due_at)
    if not due_dt:
        return "Low", "Upcoming"

    now_utc = datetime.now(timezone.utc)
    if due_dt < now_utc:
        return "Urgent", "Overdue"

    days_left = (due_dt.date() - now_utc.date()).days
    if days_left == 0:
        return "Urgent", "Due Today"
    if days_left <= 3:
        return "High", "Due Soon"
    if days_left <= 7:
        return "Medium", "Upcoming"
    return "Low", "Upcoming"


def _is_submission_needed(due_at: Optional[str], notion_status: str) -> bool:
    status_normalized = notion_status.strip().lower()
    if status_normalized in {"done", "submitted", "complete", "completed"}:
        return False
    return bool(due_at)


def _priority_to_rank(priority: str) -> int:
    mapping = {
        "urgent": 1,
        "high": 2,
        "medium": 3,
        "low": 4,
    }
    return mapping.get(priority.strip().lower(), 4)


def _canvas_notes(assignment: Dict[str, Any]) -> Optional[str]:
    parts: list[str] = []

    points = assignment.get("points_possible")
    if points is not None:
        parts.append(f"Points: {points}")

    lock_at = _optional_string(assignment.get("lock_at"))
    if lock_at:
        parts.append(f"Lock at: {lock_at}")

    unlock_at = _optional_string(assignment.get("unlock_at"))
    if unlock_at:
        parts.append(f"Unlock at: {unlock_at}")

    if not parts:
        return None
    return " | ".join(parts)


def _optional_string(value: Any) -> Optional[str]:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned if cleaned else None


def _optional_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y"}:
            return True
        if normalized in {"false", "0", "no", "n"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return None


def _optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        try:
            return int(cleaned)
        except ValueError:
            return None
    return None


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _log_optional_mapping(label: str, mapping: Optional[Tuple[str, str]]) -> None:
    if mapping:
        LOGGER.info("Detected %s property: %s (%s)", label, mapping[0], mapping[1])
    else:
        LOGGER.info("%s property: not found (field will be ignored).", label)


def create_http_handler(service: AssignmentSyncService) -> type[BaseHTTPRequestHandler]:
    class AssignmentWebhookHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.rstrip("/") == "/health":
                self._send_json(200, {"ok": True})
                return
            self._send_json(404, {"error": "Not found"})

        def do_POST(self) -> None:
            if self.path.rstrip("/") != "/assignment":
                self._send_json(404, {"error": "Not found"})
                return

            content_length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(content_length) if content_length > 0 else b"{}"
            try:
                payload = json.loads(body.decode("utf-8"))
            except json.JSONDecodeError:
                self._send_json(400, {"error": "Body must be valid JSON."})
                return

            if not isinstance(payload, dict):
                self._send_json(400, {"error": "JSON body must be an object."})
                return

            try:
                assignment = AssignmentPayload.from_dict(payload)
            except ValueError as error:
                self._send_json(400, {"error": str(error)})
                return

            try:
                result = service.create_assignment(assignment)
            except (NotionAPIError, ValueError) as error:
                LOGGER.exception("Could not create assignment")
                self._send_json(502, {"error": str(error)})
                return

            status_code = 201 if result["created"] else 200
            self._send_json(status_code, result)

        def log_message(self, format_str: str, *args: Any) -> None:
            LOGGER.info("%s - %s", self.address_string(), format_str % args)

        def _send_json(self, code: int, payload: Dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return AssignmentWebhookHandler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Push assignments into Notion (CLI or webhook mode)."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    send_parser = subparsers.add_parser("send", help="Create one assignment page in Notion")
    send_parser.add_argument("--title", required=True, help="Assignment title")
    send_parser.add_argument("--due-date", help="Due date, e.g. 2026-04-20")
    send_parser.add_argument("--course", help="Course/class name")
    send_parser.add_argument("--status", help="Notion status/select value")
    send_parser.add_argument("--source-url", help="Source link")
    send_parser.add_argument("--notes", help="Extra notes")
    send_parser.add_argument("--external-id", help="ID used for duplicate detection")

    serve_parser = subparsers.add_parser(
        "serve", help="Run webhook server (POST /assignment)"
    )
    serve_parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    serve_parser.add_argument("--port", type=int, default=8787, help="Port to bind to")

    sync_canvas_parser = subparsers.add_parser(
        "sync-canvas",
        help="Sync assignments from Canvas to Notion",
    )
    sync_canvas_parser.add_argument(
        "--canvas-base-url",
        default=_optional_string(os.getenv("CANVAS_BASE_URL")) or "https://webcourses.ucf.edu",
        help="Canvas base URL (default: CANVAS_BASE_URL or https://webcourses.ucf.edu)",
    )
    sync_canvas_parser.add_argument(
        "--canvas-token",
        default=_optional_string(os.getenv("CANVAS_API_TOKEN")),
        help="Canvas API token (default: CANVAS_API_TOKEN)",
    )
    sync_canvas_parser.add_argument(
        "--course-id",
        action="append",
        type=int,
        dest="course_ids",
        help="Limit sync to one course ID. Repeat for multiple course IDs.",
    )
    sync_canvas_parser.add_argument(
        "--include-past",
        action="store_true",
        help="Also sync past/overdue assignments (default: only upcoming/future/undated).",
    )
    sync_canvas_parser.add_argument(
        "--include-no-due",
        action="store_true",
        help="Also sync assignments without a due date (default: skipped).",
    )
    sync_canvas_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and count assignments without writing to Notion.",
    )

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    try:
        service = load_service_from_env()
    except (ValueError, NotionAPIError) as error:
        parser.error(str(error))
        return

    if args.command == "send":
        payload = AssignmentPayload(
            title=args.title,
            due_date=args.due_date,
            course=args.course,
            status=args.status,
            source_url=args.source_url,
            notes=args.notes,
            external_id=args.external_id,
        )
        result = service.create_assignment(payload)
        print(json.dumps(result, indent=2))
        return

    if args.command == "serve":
        handler = create_http_handler(service)
        server = ThreadingHTTPServer((args.host, args.port), handler)
        LOGGER.info("Webhook server running on http://%s:%s", args.host, args.port)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            LOGGER.info("Server stopped.")
        finally:
            server.server_close()
        return

    if args.command == "sync-canvas":
        canvas_token = _optional_string(args.canvas_token)
        if not canvas_token:
            parser.error("Canvas token is required. Pass --canvas-token or set CANVAS_API_TOKEN.")
            return

        canvas = CanvasClient(base_url=args.canvas_base_url, token=canvas_token)
        try:
            summary = sync_canvas_to_notion(
                service=service,
                canvas=canvas,
                include_past=bool(args.include_past),
                include_no_due=bool(args.include_no_due),
                include_course_ids=args.course_ids,
                dry_run=bool(args.dry_run),
            )
        except (CanvasAPIError, ValueError) as error:
            parser.error(str(error))
            return

        print(json.dumps(summary, indent=2))
        return

    parser.error(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
