#!/usr/bin/env python3
"""
Job polling script for Greenhouse, Lever, and Ashby.

This script:
1) Reads target companies from companies.csv
2) Fetches job postings from each company's public ATS API
3) Filters to Product Designer roles (with exclusions)
4) Stores all seen jobs in SQLite for deduplication across runs
5) Sends Slack alerts only for jobs that are brand new to the database
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


# File paths are kept in constants so they are easy to find/change later.
COMPANIES_CSV = Path("companies.csv")
DB_PATH = Path("jobs.db")


# We want any "product designer" roles, but we want to skip these title terms.
INCLUDE_TERM = "product designer"
EXCLUDE_TERMS = {
    "engineer",
    "staff",
    "principal",
    "manager",
    "researcher",
    "writer",
    "content",
    "intern",
    "new grad",
    "junior",
    "apprentice",
    "co-op",
}


# Jobs older than this many days are considered stale and ignored.
MAX_JOB_AGE_DAYS = 90


NY_WORD_RE = re.compile(r"\bny\b", re.IGNORECASE)
US_WORD_RE = re.compile(r"\bus\b", re.IGNORECASE)


@dataclass
class Company:
    """Represents one row from companies.csv."""

    name: str
    ats: str
    identifier: str


@dataclass
class JobPosting:
    """Normalized job data, so all ATS providers share one format."""

    ats: str
    job_id: str
    company_name: str
    title: str
    url: str
    posted_date: str


def parse_args() -> argparse.Namespace:
    """
    Parse command-line flags.

    --dry-run means:
    - still fetch jobs
    - still write to SQLite
    - but DO NOT send Slack messages
    """
    parser = argparse.ArgumentParser(description="Poll ATS APIs for new jobs.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print Slack messages instead of sending them.",
    )
    return parser.parse_args()


def read_companies(path: Path) -> list[Company]:
    """
    Load company targets from CSV.

    Expected columns:
    - name (human-readable company name)
    - ats (greenhouse, lever, or ashby)
    - identifier (the board slug used by that ATS provider)
    """
    companies: list[Company] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        required = {"name", "ats", "identifier"}
        if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
            raise ValueError("companies.csv must have columns: name,ats,identifier")

        for row in reader:
            name = (row.get("name") or "").strip()
            ats = (row.get("ats") or "").strip().lower()
            identifier = (row.get("identifier") or "").strip()

            if not name or not ats or not identifier:
                # Skip incomplete rows so one bad row doesn't crash the script.
                print(f"Skipping invalid CSV row: {row}")
                continue

            if ats not in {"greenhouse", "lever", "ashby"}:
                print(f"Skipping unsupported ATS '{ats}' for company '{name}'")
                continue

            companies.append(Company(name=name, ats=ats, identifier=identifier))

    return companies


def init_db(conn: sqlite3.Connection) -> None:
    """
    Create the jobs table if it does not exist yet.

    PRIMARY KEY (ats, job_id) is the dedup key requested:
    - ats avoids collisions between providers
    - job_id uniquely identifies a posting within each provider
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            ats TEXT NOT NULL,
            job_id TEXT NOT NULL,
            company_name TEXT NOT NULL,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            posted_date TEXT NOT NULL,
            first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (ats, job_id)
        )
        """
    )
    conn.commit()


def should_track_title(title: str) -> bool:
    """
    Return True when title matches our role filter rules.

    Rule:
    - must contain "product designer" (case-insensitive)
    - must NOT contain any excluded terms (case-insensitive)
    """
    lower = title.lower()
    if INCLUDE_TERM not in lower:
        return False
    return not any(term in lower for term in EXCLUDE_TERMS)


def to_date_string(value: str) -> str:
    """
    Convert an ISO-ish datetime string into YYYY-MM-DD.

    If parsing fails, we return the original value (best effort).
    """
    if not value:
        return "unknown"
    cleaned = value.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(cleaned)
        return parsed.date().isoformat()
    except ValueError:
        return value


def lever_ms_to_date(value: Any) -> str:
    """
    Convert Lever createdAt (Unix milliseconds) to YYYY-MM-DD.

    If conversion fails, return "unknown".
    """
    try:
        ms = int(value)
        date_value = dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc).date()
        return date_value.isoformat()
    except (TypeError, ValueError, OSError):
        return "unknown"


def is_older_than_days(date_text: str, max_age_days: int) -> bool:
    """
    Check whether a job date is older than the allowed age.

    Important rule:
    - If date is missing/unknown/unparseable, return False (keep the job).
    - Only skip when we can parse a real date AND it is older than max_age_days.
    """
    if not date_text or date_text.lower() == "unknown":
        return False

    try:
        # Most normalized values are already YYYY-MM-DD.
        job_date = dt.date.fromisoformat(date_text)
    except ValueError:
        try:
            # Best-effort fallback for full ISO datetime strings.
            job_date = dt.datetime.fromisoformat(
                date_text.replace("Z", "+00:00")
            ).date()
        except ValueError:
            # If parsing fails, keep the job instead of risking false exclusions.
            return False

    cutoff = dt.date.today() - dt.timedelta(days=max_age_days)
    return job_date < cutoff


def normalize_location_strings(raw_value: Any) -> list[str]:
    """
    Normalize location data into a clean list of strings.

    Why this helper exists:
    - Different ATS providers shape location data differently.
    - Sometimes it's one string, sometimes a list, sometimes a dict.
    - We convert everything into a simple list so filtering logic is consistent.
    """
    if raw_value is None:
        return []

    if isinstance(raw_value, str):
        text = raw_value.strip()
        return [text] if text else []

    if isinstance(raw_value, dict):
        # Common dictionary keys seen in ATS payloads.
        candidates = [
            raw_value.get("name"),
            raw_value.get("locationName"),
            raw_value.get("label"),
            raw_value.get("text"),
        ]
        return [str(v).strip() for v in candidates if v and str(v).strip()]

    if isinstance(raw_value, list):
        locations: list[str] = []
        for item in raw_value:
            locations.extend(normalize_location_strings(item))
        return locations

    text = str(raw_value).strip()
    return [text] if text else []


def build_combined_location(parts: list[str]) -> str:
    """
    Combine all location snippets into one lowercase searchable string.
    """
    cleaned = [part.strip() for part in parts if part and part.strip()]
    return " | ".join(cleaned).lower()


def should_include_location(combined_location: str) -> bool:
    """
    Include-list location filter.

    A job is included when ANY rule below matches:
    Existing NY patterns:
    - 'new york city'
    - 'new york, ny'
    - 'new york'
    - 'nyc'
    - regex r'\bny\b'

    Remote-US patterns:
    - 'remote - united states'
    - 'remote (united states)'
    - 'remote, united states'
    - both 'remote within' and 'united states'
    - 'remote - us'
    - 'remote (us)'
    - 'remote, us'
    - regex r'\bremote us\b'
    """
    if not combined_location:
        return False

    # Existing NY patterns.
    if "new york city" in combined_location:
        return True
    if "new york, ny" in combined_location:
        return True
    if "new york" in combined_location:
        return True
    if "nyc" in combined_location:
        return True
    if NY_WORD_RE.search(combined_location):
        return True

    # New remote-US patterns.
    if "remote - united states" in combined_location:
        return True
    if "remote (united states)" in combined_location:
        return True
    if "remote, united states" in combined_location:
        return True
    if "remote within" in combined_location and "united states" in combined_location:
        return True
    if "remote - us" in combined_location:
        return True
    if "remote (us)" in combined_location:
        return True
    if "remote, us" in combined_location:
        return True
    if re.search(r"\bremote us\b", combined_location, flags=re.IGNORECASE):
        return True

    return False


def fetch_greenhouse(client: httpx.Client, company: Company) -> list[JobPosting]:
    """Fetch and normalize Greenhouse jobs."""
    url = f"https://boards-api.greenhouse.io/v1/boards/{company.identifier}/jobs"
    response = client.get(url, timeout=20.0)
    response.raise_for_status()
    payload = response.json()

    jobs: list[JobPosting] = []
    for item in payload.get("jobs", []):
        title = str(item.get("title", "")).strip()
        if not should_track_title(title):
            continue
        location_name = (item.get("location") or {}).get("name")
        combined_location = build_combined_location(
            normalize_location_strings(location_name)
        )
        if not should_include_location(combined_location):
            print(
                f"Skipping non-US (location: '{combined_location}'): "
                f"{title} @ {company.name}"
            )
            continue
        jobs.append(
            JobPosting(
                ats="greenhouse",
                job_id=str(item.get("id", "")),
                company_name=company.name,
                title=title,
                url=str(item.get("absolute_url", "")).strip(),
                posted_date=to_date_string(str(item.get("updated_at", "")).strip()),
            )
        )
    return jobs


def fetch_lever(client: httpx.Client, company: Company) -> list[JobPosting]:
    """Fetch and normalize Lever jobs."""
    url = f"https://api.lever.co/v0/postings/{company.identifier}"
    response = client.get(url, timeout=20.0)
    response.raise_for_status()
    payload = response.json()

    jobs: list[JobPosting] = []
    for item in payload:
        title = str(item.get("text", "")).strip()
        if not should_track_title(title):
            continue
        combined_location = build_combined_location(
            normalize_location_strings((item.get("categories") or {}).get("location"))
        )
        if not should_include_location(combined_location):
            print(
                f"Skipping non-US (location: '{combined_location}'): "
                f"{title} @ {company.name}"
            )
            continue
        jobs.append(
            JobPosting(
                ats="lever",
                job_id=str(item.get("id", "")),
                company_name=company.name,
                title=title,
                url=str(item.get("hostedUrl", "")).strip(),
                posted_date=lever_ms_to_date(item.get("createdAt")),
            )
        )
    return jobs


def fetch_ashby(client: httpx.Client, company: Company) -> list[JobPosting]:
    """Fetch and normalize Ashby jobs."""
    url = f"https://api.ashbyhq.com/posting-api/job-board/{company.identifier}"
    response = client.get(url, timeout=20.0)
    response.raise_for_status()
    payload = response.json()

    jobs: list[JobPosting] = []
    for item in payload.get("jobs", []):
        title = str(item.get("title", "")).strip()
        if not should_track_title(title):
            continue
        # Ashby may provide one primary location and optional secondary locations.
        # Some boards use `locationName`, while others use `location`.
        ashby_parts = normalize_location_strings(item.get("locationName"))
        for sec in item.get("secondaryLocations") or []:
            sec_dict = sec or {}
            ashby_parts.extend(
                normalize_location_strings(
                    sec_dict.get("locationName") or sec_dict.get("location")
                )
            )

        combined_location = build_combined_location(ashby_parts)
        if not should_include_location(combined_location):
            print(
                f"Skipping non-US (location: '{combined_location}'): "
                f"{title} @ {company.name}"
            )
            continue
        jobs.append(
            JobPosting(
                ats="ashby",
                job_id=str(item.get("id", "")),
                company_name=company.name,
                title=title,
                url=str(item.get("jobUrl", "")).strip(),
                posted_date=to_date_string(str(item.get("publishedAt", "")).strip()),
            )
        )
    return jobs


def fetch_jobs_for_company(client: httpx.Client, company: Company) -> list[JobPosting]:
    """Route to the correct ATS fetcher based on company.ats."""
    if company.ats == "greenhouse":
        return fetch_greenhouse(client, company)
    if company.ats == "lever":
        return fetch_lever(client, company)
    if company.ats == "ashby":
        return fetch_ashby(client, company)
    return []


def insert_if_new(conn: sqlite3.Connection, job: JobPosting) -> bool:
    """
    Insert job row if unseen.

    Returns:
    - True if inserted (new)
    - False if already existed
    """
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO jobs (ats, job_id, company_name, title, url, posted_date)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (job.ats, job.job_id, job.company_name, job.title, job.url, job.posted_date),
    )
    return cursor.rowcount == 1


def format_slack_message(job: JobPosting) -> str:
    """Create the Slack message body in the requested format."""
    return (
        f"*{job.title}* at *{job.company_name}*\n"
        f"Posted: {job.posted_date}\n"
        f"{job.url}"
    )


def send_slack_message(client: httpx.Client, webhook_url: str, text: str) -> None:
    """Send one message to Slack incoming webhook."""
    response = client.post(webhook_url, json={"text": text}, timeout=20.0)
    response.raise_for_status()


def main() -> int:
    """Main workflow."""
    args = parse_args()

    if not COMPANIES_CSV.exists():
        print("companies.csv not found in current directory.")
        return 1

    try:
        companies = read_companies(COMPANIES_CSV)
    except Exception as exc:
        print(f"Failed to read companies.csv: {exc}")
        return 1

    if not companies:
        print("No valid companies found in companies.csv. Nothing to do.")
        return 0

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    webhook_url = os.getenv("SLACK_WEBHOOK", "").strip()
    if not args.dry_run and not webhook_url:
        print("SLACK_WEBHOOK is not set. Set it before running without --dry-run.")
        return 1

    total_seen = 0
    total_new = 0

    # One reusable HTTP client is more efficient than creating one per request.
    with httpx.Client(headers={"User-Agent": "job-tracker/1.0"}) as client:
        for company in companies:
            try:
                jobs = fetch_jobs_for_company(client, company)
                print(f"{company.name} ({company.ats}): {len(jobs)} matching jobs")
                total_seen += len(jobs)
            except Exception as exc:
                # Important requirement: one company failing should not stop the run.
                print(f"Error fetching {company.name} ({company.ats}): {exc}")
                continue

            for job in jobs:
                # Skip malformed entries that are missing a unique ID or URL.
                if not job.job_id or not job.url:
                    print(
                        f"Skipping malformed job from {job.company_name}: "
                        f"id='{job.job_id}', url='{job.url}'"
                    )
                    continue

                # Skip stale jobs (older than 90 days) only when date is available.
                if is_older_than_days(job.posted_date, MAX_JOB_AGE_DAYS):
                    print(
                        f"Skipping old job ({job.posted_date}): "
                        f"{job.title} @ {job.company_name}"
                    )
                    continue

                is_new = insert_if_new(conn, job)
                if not is_new:
                    continue

                total_new += 1
                message = format_slack_message(job)

                if args.dry_run:
                    print("\n[DRY RUN] Would send Slack message:")
                    print(message)
                else:
                    try:
                        send_slack_message(client, webhook_url, message)
                        print(f"Slack sent: {job.title} @ {job.company_name}")
                    except Exception as exc:
                        # We keep going so one Slack failure does not block other alerts.
                        print(
                            f"Error sending Slack for {job.company_name} "
                            f"job {job.job_id}: {exc}"
                        )

    conn.commit()
    conn.close()

    print(f"\nDone. Matching jobs seen this run: {total_seen}. New jobs: {total_new}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
