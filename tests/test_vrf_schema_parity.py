"""Every column the web layer names must exist on the table.

A select naming a column that is not there does not fail loudly. PostgREST
rejects the query, supabase-js hands back `data: null` with the error in a
field callers routinely ignore, and the code carries on with an empty list.
That is how `select("platform, status")` on `vrf_accounts` silently classified
every importable link as metadata-only: no exception, no log, just a feature
that never worked.

The snapshot below is the schema as it stands in Supabase. It is checked in
because the schema is not: there are no migration files in this repository, so
without it these tests would have nothing to compare against. Refresh it when
you add a column:

    select table_name, string_agg(column_name, ' ' order by column_name)
    from information_schema.columns
    where table_schema = 'public' and table_name like 'vrf_%'
    group by table_name order by table_name;
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "web"

#: public.vrf_* as of 2026-09-06.
SCHEMA: dict[str, set[str]] = {
    "vrf_accounts": {
        "access_token_encrypted", "account_handle", "account_ref",
        "connected_at", "id", "platform", "refresh_token_encrypted",
        "revoked_at", "scopes", "token_expires_at", "user_id",
    },
    "vrf_assets": {
        "created_at", "duration_seconds", "height", "id", "ingest_detail",
        "ingest_status", "needs_attribution", "new_version_probability",
        "original_viral_score", "probability_confidence", "processed_path",
        "processing_plan", "publish_state", "rights_attested_at",
        "rights_evidence", "rights_holder", "rights_source", "score_breakdown",
        "source_author", "source_platform", "source_url", "source_video_id",
        "storage_path", "thumbnail_url", "title", "user_id", "width",
    },
    "vrf_metrics": {
        "collected_at", "comments", "engagement_rate", "id", "likes",
        "platform", "predicted_probability", "publish_job_id", "shares",
        "user_id", "views",
    },
    "vrf_publish_jobs": {
        "asset_id", "attempts", "caption", "created_at", "error", "id", "logs",
        "mode", "next_attempt_at", "platform", "post_id", "post_url",
        "scheduled_for", "status", "updated_at", "user_id",
    },
    "vrf_sources": {
        "analysis", "author", "comments", "discovered_at", "duration_seconds",
        "followers", "id", "language", "likes", "niche", "platform",
        "posted_at", "score_breakdown", "score_confidence", "shares",
        "thumbnail_url", "title", "url", "user_id", "video_id", "views",
        "viral_score",
    },
    "vrf_tasks": {
        "attempts", "completed_at", "created_at", "error", "id", "kind",
        "payload", "result", "started_at", "status", "user_id",
    },
}

#: `.from("table")` followed, eventually, by `.select(...)`. The select may be
#: several lines later, may concatenate literals, and in the repositories is
#: usually a `const FIELDS` naming a dozen columns -- which is exactly where a
#: typo hides, so those are resolved rather than skipped.
_FROM = re.compile(r'\.from\(\s*"(vrf_\w+)"\s*\)')
_SELECT = re.compile(
    r'\.select\(\s*(?P<literal>(?:"(?:[^"\\]|\\.)*"\s*\+?\s*)+)'
    r'|\.select\(\s*(?P<name>[A-Z_][A-Z0-9_]*)\s*[,)]'
)
_FIELD_CONST = re.compile(
    r'const\s+([A-Z_][A-Z0-9_]*)\s*=\s*((?:"(?:[^"\\]|\\.)*"\s*\+?\s*)+);'
)

#: Not columns: PostgREST modifiers and embedded-resource syntax.
_NOT_A_COLUMN = re.compile(r"[(){}!:*]")


def _join(literals: str) -> str:
    return "".join(
        part[1:-1] for part in re.findall(r'"(?:[^"\\]|\\.)*"', literals)
    )


def _sources() -> list[Path]:
    roots = [WEB / "lib", WEB / "app" / "api" / "reels", WEB / "app" / "dashboard" / "reels"]
    files: list[Path] = []
    for root in roots:
        if root.exists():
            files.extend(p for p in root.rglob("*.ts"))
            files.extend(p for p in root.rglob("*.tsx"))
    return files


def _selects() -> list[tuple[Path, str, list[str]]]:
    """Every (file, table, columns) the web layer asks Postgres for."""
    found: list[tuple[Path, str, list[str]]] = []
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        constants = {
            name: _join(value) for name, value in _FIELD_CONST.findall(text)
        }
        for match in _FROM.finditer(text):
            table = match.group(1)
            tail = text[match.end() : match.end() + 900]
            select = _SELECT.search(tail)
            if not select:
                continue
            if select.group("literal"):
                joined = _join(select.group("literal"))
            else:
                joined = constants.get(select.group("name"), "")
            columns = [
                column.strip()
                for column in joined.split(",")
                if column.strip() and not _NOT_A_COLUMN.search(column)
            ]
            if columns:
                found.append((path, table, columns))
    return found


def test_the_web_layer_selects_from_known_tables():
    for path, table, _ in _selects():
        assert table in SCHEMA, f"{path.name} reads unknown table {table!r}"


def test_every_selected_column_exists():
    """The regression that prompted this file."""
    problems: list[str] = []
    for path, table, columns in _selects():
        for column in columns:
            if column not in SCHEMA[table]:
                problems.append(
                    f"{path.relative_to(WEB)}: {table}.{column} does not exist"
                )
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("table", sorted(SCHEMA))
def test_every_table_is_owned_by_a_user(table):
    """Tenant isolation depends on it; RLS policies are written against it."""
    assert "user_id" in SCHEMA[table]


def test_token_columns_are_never_selected_outside_the_worker_path():
    """The browser-facing layer has no reason to hold ciphertext, let alone
    pass it to a client component."""
    for path, table, columns in _selects():
        if table != "vrf_accounts":
            continue
        leaked = [c for c in columns if c.endswith("_encrypted")]
        assert not leaked, f"{path.relative_to(WEB)} selects {leaked}"


def test_the_snapshot_covers_every_table_the_code_uses():
    used = {table for _, table, _ in _selects()}
    assert used <= set(SCHEMA)
