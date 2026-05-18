"""
Read the parsed JSON artifact and sync spiders to Airtable.

Runs in the privileged workflow (workflow_run, base-repo context).
The JSON is treated as untrusted input — it was produced by a workflow
running on a fork PR's code. Validate before doing anything with it.
"""

import json
import sys
from pathlib import Path

from decouple import config
from pyairtable import Api
from pyairtable.formulas import match

"""
AirTable field names for the slug and agency name.
These should match the field names in the AirTable base.
"""
# Spiders table field names
SLUG_FIELD = "Slug"
AGENCY_FIELD = "Agency name"
PROGRAM_FIELD = "Program"

# Backlog table field names
BACKLOG_AGENCY_FIELD = "Agency name"
BACKLOG_PROGRAM_LOOKUP_FIELD = "Program"

# Validation limits
MAX_FILES = 10  # PR can't touch more than this many spider files
MAX_SPIDERS_PER_FILE = 200  # spider_configs lists shouldn't go above this


def required_config(key: str) -> str:
    value = config(key, default="")
    if not value.strip():
        sys.exit(f"[ERROR] Required env var {key} is missing or empty")
    return value


def validate_artifact(data) -> list[dict]:
    """
    Validate the JSON artifact's structure and return the file entries.
    """
    if not isinstance(data, dict) or "files" not in data:
        sys.exit("[ERROR] Invalid artifact: missing 'files' key")

    files = data["files"]
    if not isinstance(files, list):
        sys.exit("[ERROR] Invalid artifact: 'files' is not a list")
    if len(files) > MAX_FILES:
        sys.exit(f"[ERROR] Too many files ({len(files)} > {MAX_FILES})")

    cleaned = []
    for entry in files:
        if not isinstance(entry, dict):
            continue
        spiders = entry.get("spiders")
        if not isinstance(spiders, list):
            continue
        if len(spiders) > MAX_SPIDERS_PER_FILE:
            sys.exit(
                f"[ERROR] Too many spiders in artifact ({len(spiders)} > {MAX_SPIDERS_PER_FILE})"  # noqa
            )

        clean_spiders = []
        for s in spiders:
            if not isinstance(s, dict):
                continue
            name = s.get("name")
            agency = s.get("agency")
            if not isinstance(name, str) or not name:
                continue
            if agency is not None and not isinstance(agency, str):
                continue
            clean_spiders.append({"name": name, "agency": agency})

        cleaned.append(
            {
                "path": entry.get("path", "<unknown>"),
                "spiders": clean_spiders,
            }
        )

    return cleaned


def find_program_for_agency(agency: str, backlog_table) -> str | None:
    """
    Look up `agency` in the Backlog table and return the linked Program record ID.

    The Backlog table's Program field is a *lookup*, it returns
    the linked program's record ID. This function Returns the
    Programs record ID, or None if the agency isn't in Backlog or
    the looked-up Program name doesn't resolve to a Programs record.
    """
    backlog_records = backlog_table.all(
        formula=match({BACKLOG_AGENCY_FIELD: agency}),
        fields=[BACKLOG_PROGRAM_LOOKUP_FIELD],
        max_records=1,
    )
    if not backlog_records:
        return None
    # Lookup fields always return a list, even when the source link is single.
    program_links = backlog_records[0]["fields"].get(BACKLOG_PROGRAM_LOOKUP_FIELD) or []
    if not program_links:
        return None
    return program_links[0]


def sync_to_airtable(
    spiders: list[dict], table, table_records, program_record_id: str | None = None
) -> dict:
    """
    Sync spiders to Airtable, keyed on agency name.
    Agency name is considered the source of truth for
    matching records.

    Workflow for each spider:
      - If the agency is already in the table and the slug matches: skip.
      - If the agency is in the table but the slug differs: update the slug.
      - If the agency is not in the table: create a new record.

    If `program_record_id` is provided, every created or updated record also
    has its Program field set to link to that record. If None, the Program
    field is left untouched.

    Note:
    If the agency name for the same slug changes, a new record
    will be created and the table will end up with multiple
    records for the same slug but different agency names.
    There would have to be some manual cleanup to remove the
    old record with the outdated slug, but this is a safer
    approach than accidentally overwriting an existing record
    with a new slug that belongs to a different agency.

    Returns a summary: {'created': [...], 'updated': [...], 'skipped': [...]}.
    """
    existing: dict[str, tuple[str, str]] = {}
    for r in table_records:
        agency = r["fields"].get(AGENCY_FIELD)
        if agency:
            existing[agency] = (r["id"], r["fields"].get(SLUG_FIELD, ""))

    to_create, to_update, skipped = [], [], []

    for spider in spiders:
        slug = spider["name"]
        agency = spider.get("agency")
        if not agency:
            # Can't key on agency if it's missing - log and move on.
            print(f"[INFO] skipping spider '{slug}' - no agency name")
            continue

        if agency in existing:
            record_id, current_slug = existing[agency]
            if current_slug == slug:
                skipped.append(agency)
            else:
                fields = {SLUG_FIELD: slug}
                if program_record_id:
                    fields[PROGRAM_FIELD] = [program_record_id]
                to_update.append({"id": record_id, "fields": fields})
        else:
            fields = {SLUG_FIELD: slug, AGENCY_FIELD: agency}
            if program_record_id:
                fields[PROGRAM_FIELD] = [program_record_id]
            to_create.append(fields)
            existing[agency] = ("", slug)

    created = []
    if to_create:
        created = [r["fields"].get(AGENCY_FIELD) for r in table.batch_create(to_create)]

    updated = []
    if to_update:
        table.batch_update(to_update)
        updated = [u["fields"][SLUG_FIELD] for u in to_update]

    return {"created": created, "updated": updated, "skipped": skipped}


def main():
    if len(sys.argv) != 2:
        sys.exit("Usage: sync_from_json.py <artifact_json_path>")

    pat = required_config("CS_AIRTABLE_PAT")
    base_id = required_config("CS_AIRTABLE_BASE_ID")
    slugs_table_name = required_config("CS_SLUGS_TABLE_ID")
    backlog_table_name = required_config("CS_BACKLOG_TABLE_ID")

    artifact_path = Path(sys.argv[1])
    data = json.loads(artifact_path.read_text())
    file_entries = validate_artifact(data)

    api = Api(pat)
    slugs_table = api.table(base_id, slugs_table_name)
    backlog_table = api.table(base_id, backlog_table_name)

    slugs_table_records = slugs_table.all(fields=[SLUG_FIELD, AGENCY_FIELD])

    overall = {"created": [], "updated": [], "skipped": []}
    for entry in file_entries:
        spiders = entry["spiders"]
        if not spiders:
            continue

        program_id = None
        for spider in spiders:
            agency = spider.get("agency")
            if agency and (
                program_id := find_program_for_agency(agency, backlog_table)
            ):
                break

        result = sync_to_airtable(spiders, slugs_table, slugs_table_records, program_id)
        for key in overall:
            overall[key].extend(result[key])

    print(f"[INFO] Created {len(overall['created'])}: {overall['created']}")
    print(f"[INFO] Updated {len(overall['updated'])}: {overall['updated']}")
    print(
        f"[INFO] Skipped {len(overall['skipped'])} (already up to date): {overall['skipped']}"  # noqa
    )


if __name__ == "__main__":
    main()
