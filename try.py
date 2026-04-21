import json
import re
import time
import requests
from bs4 import BeautifulSoup
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from django.db import transaction
from users.models import Course, PrerequisiteGroup

BASE = "https://catalog.csun.edu"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; Gradmate prerequisite sync)"
}

PROGRAM_DEFAULT = "BS_CS"
CATALOG_YEAR_DEFAULT = 2023

PAGE_CACHE = {}


def norm(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def code_to_url_slug(code):
    return code.lower().replace("_", "-")


def subject_abbr_from_code(code):
    return str(code).split("-", 1)[0].upper().strip()


def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    time.sleep(0.15)
    return r.text


def get_soup(url):
    return BeautifulSoup(fetch(url), "html.parser")


def get_course_page_url(code):
    abbr = subject_abbr_from_code(code).lower()
    slug = code_to_url_slug(code)
    return f"{BASE}/academics/{abbr}/courses/{slug}/"


def infer_level_from_number(code):
    number = code.split("-", 1)[1]
    m = re.match(r"(\d{2,3})", number)
    if not m:
        return None
    n = int(m.group(1))
    return "upper" if n >= 300 else "lower"


def extract_subject_title_map(subject_abbr):
    url = f"{BASE}/academics/{subject_abbr.lower()}/courses/"
    soup = get_soup(url)

    out = {}
    for a in soup.find_all("a", href=True):
        text = norm(a.get_text(" ", strip=True))

        m = re.match(
            rf"^{re.escape(subject_abbr.upper())}\s+([0-9A-Z/.,-]+)\.\s+(.*?)\s+\(([^)]+)\)$",
            text,
            flags=re.IGNORECASE,
        )
        if not m:
            continue

        raw_num = m.group(1).upper()
        title = norm(m.group(2))
        credit_raw = m.group(3)

        clean_code = f"{subject_abbr.upper()}-{raw_num.replace('/L', '')}"
        first_num = re.match(r"(\d+(?:\.\d+)?)", credit_raw)
        credits = float(first_num.group(1)) if first_num else 3.0

        out[clean_code] = {
            "title": title,
            "credits": credits,
        }

    return out


def extract_subject_full_name(subject_abbr):
    subject_abbr = subject_abbr.upper().strip()

    forced_map = {
        "ECE": "Electrical and Computer Engineering",
        "CIT": "Computer Information Technology",
        "COMP": "Computer Science",
    }
    if subject_abbr in forced_map:
        return forced_map[subject_abbr]

    existing_subjects = (
        Course.objects
        .filter(code__startswith=f"{subject_abbr}-")
        .exclude(subject="")
        .values_list("subject", flat=True)
    )

    bad_short_names = {subject_abbr, "EE", "ECE", "CIT", "COMP", "MATH", "PHIL"}

    for subj in existing_subjects:
        s = (subj or "").strip()
        if s and s.upper() not in bad_short_names and len(s) > 4:
            return s

    try:
        url = f"{BASE}/academics/{subject_abbr.lower()}/courses/"
        soup = get_soup(url)
        text = norm(soup.get_text(" ", strip=True))
        m = re.search(r"Home\s*/\s*.*?\s*/\s*(.*?)\s*/\s*Courses", text, flags=re.IGNORECASE)
        if m:
            name = norm(m.group(1))
            if name and name.upper() not in bad_short_names:
                return name
    except Exception:
        pass

    return subject_abbr


def extract_text_from_course_page(code):
    code = code.upper().strip()
    if code in PAGE_CACHE:
        return PAGE_CACHE[code]

    url = get_course_page_url(code)
    soup = get_soup(url)
    full_text = norm(soup.get_text(" ", strip=True))

    subject_abbr = subject_abbr_from_code(code)
    number = code.split("-", 1)[1]

    title = ""
    credits = None

    heading_text = ""
    h1 = soup.find(["h1", "h2"])
    if h1:
        heading_text = norm(h1.get_text(" ", strip=True))

    m = re.search(
        rf"{re.escape(subject_abbr)}\s+{re.escape(number.replace('-', ''))}\.?\s+(.*?)\s+\(([^)]+)\)",
        heading_text,
        flags=re.IGNORECASE,
    )
    if m:
        title = norm(m.group(1))
        first_num = re.match(r"(\d+(?:\.\d+)?)", m.group(2))
        if first_num:
            credits = float(first_num.group(1))

    if not title:
        try:
            subject_map = extract_subject_title_map(subject_abbr)
            if code in subject_map:
                title = subject_map[code]["title"]
                credits = subject_map[code]["credits"]
        except Exception:
            pass

    def grab(label):
        m = re.search(
            rf"\b{label}:\s*(.+?)(?=(?:\bCorequisites?:|\bRecommended Preparatory:|\bFormerly:|\bCrosslisted:|\bCredit will not be allowed|\bSchedule of Classes\b|\bTop\b|\bView Catalog Archives\b|\bResources\b|$))",
            full_text,
            flags=re.IGNORECASE,
        )
        if not m:
            return ""

        text = norm(m.group(1)).rstrip(" .;")
        text = re.split(r"\bSpring-\d{4}\b", text, maxsplit=1)[0]
        text = re.split(r"\bFall-\d{4}\b", text, maxsplit=1)[0]
        text = re.split(r"\bSchedule of Classes\b", text, maxsplit=1, flags=re.IGNORECASE)[0]
        first_sentence = re.split(r"(?<=[.!?])\s+(?=[A-Z])", text)[0]
        return norm(first_sentence).rstrip(" .;")

    prereq_text = grab("Prerequisites?")
    coreq_text = grab("Corequisites?")

    description = full_text

    if coreq_text:
        marker1 = f"Corequisite: {coreq_text}"
        marker2 = f"Corequisites: {coreq_text}"
        if marker1 in description:
            description = description.split(marker1, 1)[1]
        elif marker2 in description:
            description = description.split(marker2, 1)[1]
    elif prereq_text:
        marker1 = f"Prerequisite: {prereq_text}"
        marker2 = f"Prerequisites: {prereq_text}"
        if marker1 in description:
            description = description.split(marker1, 1)[1]
        elif marker2 in description:
            description = description.split(marker2, 1)[1]

    description = re.split(r"\bSpring-\d{4}\b", description, maxsplit=1)[0]
    description = re.split(r"\bFall-\d{4}\b", description, maxsplit=1)[0]
    description = re.split(r"\bSchedule of Classes\b", description, maxsplit=1, flags=re.IGNORECASE)[0]
    description = re.split(r"\bAvailable for graduate credit\b", description, maxsplit=1, flags=re.IGNORECASE)[0]
    description = re.split(r"\bTop\b", description, maxsplit=1)[0]
    description = re.split(r"\bView Catalog Archives\b", description, maxsplit=1)[0]
    description = re.split(r"\bResources\b", description, maxsplit=1)[0]

    description = re.sub(r"\bOne\s+\d+-hour lab per week\.?$", "", description, flags=re.IGNORECASE)
    description = re.sub(r"\bLab:\s*.*?$", "", description, flags=re.IGNORECASE)

    description = norm(description).rstrip(" .")
    if description:
        description += "."

    result = {
        "title": title or code,
        "credits": credits if credits is not None else 3.0,
        "prereq_text": prereq_text,
        "coreq_text": coreq_text,
        "description": description,
        "url": url,
    }
    PAGE_CACHE[code] = result
    return result


def prefetch_pages(codes, workers=8):
    codes = [c.upper().strip() for c in codes if c.upper().strip() not in PAGE_CACHE]
    if not codes:
        return

    print(f"Prefetching {len(codes)} pages with {workers} workers...")
    done = 0
    errors = 0

    def _fetch_one(code):
        try:
            extract_text_from_course_page(code)
            return code, None
        except Exception as e:
            return code, str(e)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_one, code): code for code in codes}
        for fut in as_completed(futures):
            _, err = fut.result()
            done += 1
            if err:
                errors += 1
            if done % 20 == 0 or done == len(codes):
                print(f"  fetched {done}/{len(codes)} ({errors} errors so far)")

    print(f"Prefetch done: {done - errors} ok, {errors} errors.")


def split_top_level_semicolons(text):
    parts = [norm(x) for x in text.split(";")]
    return [x for x in parts if x]


def extract_course_codes(raw_text, keep_lab_pair=False):
    text = norm(raw_text)
    found = []

    pattern = re.compile(r"\b([A-Z]{2,6})\s+([0-9]{2,3}[A-Z]{0,2})(/L)?\b")

    for subj, num, slash_lab in pattern.findall(text):
        subj = subj.upper()
        num = num.upper()

        if slash_lab:
            found.append(f"{subj}-{num}")
            if keep_lab_pair:
                found.append(f"{subj}-{num}L")
            continue

        if num.endswith("L"):
            if keep_lab_pair:
                found.append(f"{subj}-{num}")
            else:
                found.append(f"{subj}-{num[:-1]}")
            continue

        found.append(f"{subj}-{num}")

    out = []
    seen = set()
    for x in found:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def clear_existing_requisites(course):
    course.prerequisites.clear()
    course.corequisites.clear()
    course.prereq_groups.all().delete()
    course.prereq_mode = "ALL"
    course.save(update_fields=["prereq_mode"])


def get_course_map():
    return {c.code.upper(): c for c in Course.objects.all()}


COURSE_MAP = get_course_map()


def refresh_course_map():
    global COURSE_MAP
    COURSE_MAP = get_course_map()


def upsert_missing_course(code, program=PROGRAM_DEFAULT, catalog_year=CATALOG_YEAR_DEFAULT):
    code = code.upper().strip()

    existing = Course.objects.filter(code=code).first()
    if existing:
        print("EXISTS", code)
        return existing, False

    subject_abbr = subject_abbr_from_code(code)
    subject_full = extract_subject_full_name(subject_abbr)
    level = infer_level_from_number(code)

    try:
        page = extract_text_from_course_page(code)
        title = page["title"]
        credits = page["credits"]
        description = page["description"].lstrip(". ").strip()
    except Exception as e:
        print(f"CREATE SKIPPED {code}: could not fetch details -> {e}")
        return None, False

    course = Course.objects.create(
        program=program,
        catalog_year=catalog_year,
        code=code,
        title=title,
        credits=credits,
        subject=subject_full,
        level=level,
        section="",
        description=description,
        prereq_mode="ALL",
    )

    print("CREATED", code, "|", title, "|", credits, "|", level, "|", subject_full)
    return course, True


def ensure_courses_exist(codes, program=PROGRAM_DEFAULT, catalog_year=CATALOG_YEAR_DEFAULT):
    created_codes = []

    for code in codes:
        code = code.upper().strip()
        course, created = upsert_missing_course(
            code,
            program=program,
            catalog_year=catalog_year,
        )
        if created and course is not None:
            created_codes.append(code)

    refresh_course_map()
    print("NEWLY CREATED:", created_codes)
    return created_codes


def has_mixed_logic(prereq_text):
    text = norm(prereq_text).lower()
    return " or " in text and " and " in text


def resolve_codes_to_courses(codes, auto_add_missing=False):
    resolved = []
    missing = []

    for code in codes:
        obj = COURSE_MAP.get(code.upper())
        if obj:
            resolved.append(obj)
        else:
            missing.append(code)

    if missing and auto_add_missing:
        print("  auto-adding missing courses ->", sorted(set(missing)))
        ensure_courses_exist(sorted(set(missing)))
        refresh_course_map()

        resolved = []
        still_missing = []
        for code in codes:
            obj = COURSE_MAP.get(code.upper())
            if obj:
                resolved.append(obj)
            else:
                still_missing.append(code)
        missing = still_missing

    return resolved, missing


def parse_path_token(token):
    token = norm(token)
    m = re.fullmatch(r"([A-Z]{2,6})\s+([0-9]{2,3}[A-Z]?)(/L)?", token, flags=re.IGNORECASE)
    if not m:
        return []

    subj = m.group(1).upper()
    num = m.group(2).upper()
    return [f"{subj}-{num}"]


def split_or_tokens(part):
    cleaned = norm(part)
    cleaned = re.sub(r"(?i)^grade of .*? better in ", "", cleaned)
    cleaned = re.sub(r"(?i)^one of ", "", cleaned)
    cleaned = re.sub(r"(?i)^either ", "", cleaned)
    tokens = [norm(x) for x in re.split(r"(?i)\s+or\s+", cleaned)]
    return [x for x in tokens if x]


def parse_any_options(part):
    lowered = part.lower()
    cleaned = re.sub(r"\bor better\b", "", lowered, flags=re.IGNORECASE)

    if " or " not in cleaned and "either " not in cleaned and " any of " not in cleaned:
        return []

    tokens = split_or_tokens(part)
    options = []

    for token in tokens:
        if "," in token:
            pieces = [norm(x) for x in token.split(",") if norm(x)]
            for piece in pieces:
                codes = parse_path_token(piece)
                if codes:
                    options.extend(codes)
            continue

        codes = parse_path_token(token)
        if codes:
            options.extend(codes)
            continue

        codes = extract_course_codes(token, keep_lab_pair=False)
        if codes:
            options.extend(codes)

    deduped = []
    seen = set()
    for code in options:
        if code not in seen:
            seen.add(code)
            deduped.append(code)
    return deduped


def apply_prerequisites(course, prereq_text, dry_run=True, auto_add_missing=False):
    parts = split_top_level_semicolons(prereq_text)
    direct_courses = []
    any_groups_to_create = []
    unresolved = []
    manual_notes = []

    if not parts and prereq_text:
        parts = [prereq_text]

    for part in parts:
        part = norm(part)
        if not part:
            continue

        if (
            "lower division writing requirement" in part.lower()
            or "consent of instructor" in part.lower()
            or "junior standing" in part.lower()
            or "senior standing" in part.lower()
            or "department consent" in part.lower()
            or "advisor approval" in part.lower()
            or "permission of" in part.lower()
            or "prior approval" in part.lower()
            or "admission to" in part.lower()
            or "placement in" in part.lower()
            or "successful completion of" in part.lower()
            or "not open to students" in part.lower()
            or "multiple measures placement" in part.lower()
            or "placement test" in part.lower()
            or "exemption" in part.lower()
        ):
            manual_notes.append(part)
            continue

        options = parse_any_options(part)

        if options:
            options = [c for c in options if c != course.code.upper()]
            objs, missing = resolve_codes_to_courses(options, auto_add_missing=auto_add_missing)
            unresolved.extend(missing)
            if objs:
                any_groups_to_create.append(objs)
            continue

        codes = [c for c in extract_course_codes(part, keep_lab_pair=False) if c != course.code.upper()]
        if not codes:
            if part:
                manual_notes.append(part)
            continue

        objs, missing = resolve_codes_to_courses(codes, auto_add_missing=auto_add_missing)
        unresolved.extend(missing)
        direct_courses.extend(objs)

    direct_unique = []
    seen = set()
    for obj in direct_courses:
        if obj.id not in seen:
            seen.add(obj.id)
            direct_unique.append(obj)

    group_data = []
    for grp in any_groups_to_create:
        grp_unique = []
        seen_ids = set()
        for obj in grp:
            if obj.id not in seen_ids:
                seen_ids.add(obj.id)
                grp_unique.append(obj)
        if grp_unique:
            group_data.append(grp_unique)

    if dry_run:
        print(f"  direct prereqs -> {[x.code for x in direct_unique]}")
        print(f"  any groups     -> {[[x.code for x in grp] for grp in group_data]}")
        if unresolved:
            print(f"  missing codes  -> {sorted(set(unresolved))}")
        if manual_notes:
            print(f"  manual notes   -> {manual_notes}")
        return {
            "direct": direct_unique,
            "groups": group_data,
            "unresolved": sorted(set(unresolved)),
            "manual_notes": manual_notes,
        }

    if direct_unique:
        course.prerequisites.add(*direct_unique)

    for grp in group_data:
        g = PrerequisiteGroup.objects.create(
            for_course=course,
            name="ANY",
            min_required=1,
        )
        g.options.add(*grp)

    return {
        "direct": direct_unique,
        "groups": group_data,
        "unresolved": sorted(set(unresolved)),
        "manual_notes": manual_notes,
    }


def apply_corequisites(course, coreq_text, dry_run=True, auto_add_missing=False):
    codes = [c for c in extract_course_codes(coreq_text, keep_lab_pair=True) if c != course.code.upper()]
    objs, missing = resolve_codes_to_courses(codes, auto_add_missing=auto_add_missing)

    if dry_run:
        print(f"  coreqs         -> {[x.code for x in objs]}")
        if missing:
            print(f"  missing coreqs -> {sorted(set(missing))}")
        return {
            "coreqs": objs,
            "unresolved": sorted(set(missing)),
        }

    if objs:
        course.corequisites.add(*objs)

    return {
        "coreqs": objs,
        "unresolved": sorted(set(missing)),
    }


def sync_one(code, dry_run=True, clear_first=True, auto_add_missing=False, verbose=True):
    code = code.upper().strip()
    course = COURSE_MAP.get(code)
    if not course:
        if verbose:
            print(f"SKIP {code}: not found in local DB")
        return {"status": "missing_local"}

    try:
        data = extract_text_from_course_page(code)
    except Exception as e:
        if verbose:
            print(f"ERROR {code}: fetch failed -> {e}")
        return {"status": "fetch_error", "error": str(e)}

    if verbose:
        print("=" * 90)
        print(code, "|", course.title)
        print("URL:", data["url"])
        print("PREREQ TEXT:", data["prereq_text"] or "-")
        print("COREQ TEXT :", data["coreq_text"] or "-")

    if has_mixed_logic(data["prereq_text"]):
        if verbose:
            print("  MANUAL REVIEW -> mixed AND/OR logic detected")
        return {
            "status": "manual_review",
            "needs_manual": True,
            "reason": "mixed_logic",
            "prereq_text": data["prereq_text"],
            "coreq_text": data["coreq_text"],
            "url": data["url"],
        }

    pre = apply_prerequisites(
        course,
        data["prereq_text"],
        dry_run=True,
        auto_add_missing=auto_add_missing,
    )
    co = apply_corequisites(
        course,
        data["coreq_text"],
        dry_run=True,
        auto_add_missing=auto_add_missing,
    )

    needs_manual = bool(pre["unresolved"] or co["unresolved"])

    if dry_run:
        return {
            "status": "dry_run",
            "needs_manual": needs_manual,
            "prereq_text": data["prereq_text"],
            "coreq_text": data["coreq_text"],
            "url": data["url"],
            "pre": pre,
            "co": co,
        }

    if needs_manual:
        if verbose:
            print("  SKIPPED WRITE -> manual review needed")
        return {
            "status": "manual_review",
            "needs_manual": True,
            "reason": "unresolved_codes",
            "prereq_text": data["prereq_text"],
            "coreq_text": data["coreq_text"],
            "url": data["url"],
            "pre": pre,
            "co": co,
        }

    with transaction.atomic():
        if clear_first:
            clear_existing_requisites(course)

        apply_prerequisites(
            course,
            data["prereq_text"],
            dry_run=False,
            auto_add_missing=auto_add_missing,
        )
        apply_corequisites(
            course,
            data["coreq_text"],
            dry_run=False,
            auto_add_missing=auto_add_missing,
        )

        course.prereq_mode = "ALL"
        course.save(update_fields=["prereq_mode"])

    if verbose:
        print("  UPDATED")
    return {
        "status": "updated",
        "needs_manual": False,
        "prereq_text": data["prereq_text"],
        "coreq_text": data["coreq_text"],
        "url": data["url"],
    }


def show_course(code):
    c = Course.objects.get(code=code.upper())
    print(
        c.code,
        "| title:", c.title,
        "| subject:", c.subject,
        "| level:", c.level,
        "| credits:", c.credits,
        "| prereqs:", [x.code for x in c.prerequisites.all()],
        "| coreqs:", [x.code for x in c.corequisites.all()],
        "| groups:", [(g.name, g.min_required, [x.code for x in g.options.all()]) for g in c.prereq_groups.all()],
    )


def count_requisite_status():
    total = Course.objects.count()
    with_prereqs = Course.objects.filter(prerequisites__isnull=False).distinct().count()
    with_coreqs = Course.objects.filter(corequisites__isnull=False).distinct().count()
    with_groups = Course.objects.filter(prereq_groups__isnull=False).distinct().count()

    untouched = 0
    for c in Course.objects.all():
        if (
            c.prerequisites.count() == 0
            and c.corequisites.count() == 0
            and c.prereq_groups.count() == 0
        ):
            untouched += 1

    print("Total courses:", total)
    print("Have direct prereqs:", with_prereqs)
    print("Have coreqs:", with_coreqs)
    print("Have prereq groups:", with_groups)
    print("Untouched:", untouched)


def sync_subject(subject_abbr, dry_run=True, limit=None, only_empty=False, auto_add_missing=False, verbose=True):
    subject_abbr = subject_abbr.upper().strip()
    qs = Course.objects.filter(code__startswith=f"{subject_abbr}-").order_by("code")

    if only_empty:
        candidates = []
        for c in qs:
            if (
                c.prerequisites.count() == 0
                and c.corequisites.count() == 0
                and c.prereq_groups.count() == 0
            ):
                candidates.append(c.id)
        qs = Course.objects.filter(id__in=candidates).order_by("code")

    if limit:
        qs = qs[:limit]

    results = {
        "processed": 0,
        "updated": 0,
        "manual_review": 0,
        "fetch_error": 0,
        "missing_local": 0,
        "skipped_lab": 0,
        "other": 0,
        "codes_updated": [],
        "codes_manual_review": [],
        "codes_fetch_error": [],
        "codes_missing_local": [],
        "codes_skipped_lab": [],
    }

    non_lab = [c.code for c in qs if not c.code.upper().endswith("L")]
    prefetch_pages(non_lab)

    for c in qs:
        if c.code.upper().endswith("L"):
            if verbose:
                print(f"SKIP {c.code}: lab course skipped for subject batch")
            results["processed"] += 1
            results["skipped_lab"] += 1
            results["codes_skipped_lab"].append(c.code)
            continue

        result = sync_one(
            c.code,
            dry_run=dry_run,
            clear_first=True,
            auto_add_missing=auto_add_missing,
            verbose=verbose,
        )
        results["processed"] += 1
        status = result.get("status")

        if status == "updated":
            results["updated"] += 1
            results["codes_updated"].append(c.code)
        elif status == "manual_review":
            results["manual_review"] += 1
            results["codes_manual_review"].append(c.code)
        elif status == "fetch_error":
            results["fetch_error"] += 1
            results["codes_fetch_error"].append(c.code)
        elif status == "missing_local":
            results["missing_local"] += 1
            results["codes_missing_local"].append(c.code)
        elif status == "dry_run":
            pass
        else:
            results["other"] += 1

    print("\n" + "=" * 90)
    print("SUMMARY FOR", subject_abbr)
    print("processed     =", results["processed"])
    print("updated       =", results["updated"])
    print("manual_review =", results["manual_review"])
    print("fetch_error   =", results["fetch_error"])
    print("missing_local =", results["missing_local"])
    print("skipped_lab   =", results["skipped_lab"])
    print("other         =", results["other"])

    if results["codes_updated"]:
        print("\nUPDATED:")
        for code in results["codes_updated"]:
            print(" ", code)

    if results["codes_manual_review"]:
        print("\nMANUAL REVIEW:")
        for code in results["codes_manual_review"]:
            print(" ", code)

    if results["codes_fetch_error"]:
        print("\nFETCH ERROR:")
        for code in results["codes_fetch_error"]:
            print(" ", code)

    if results["codes_skipped_lab"]:
        print("\nSKIPPED LAB:")
        for code in results["codes_skipped_lab"]:
            print(" ", code)

    return results


def sync_all(dry_run=True, only_empty=False, subject_filter=None, auto_add_missing=False, verbose=True):
    qs = Course.objects.all().order_by("code")
    if subject_filter:
        subject_filter = subject_filter.upper().strip()
        qs = qs.filter(code__startswith=f"{subject_filter}-")

    results = {
        "processed": 0,
        "updated": 0,
        "manual_review": 0,
        "fetch_error": 0,
        "missing_local": 0,
        "skipped_lab": 0,
        "other": 0,
    }

    updated_codes = []
    manual_codes = []
    error_codes = []
    skipped_lab_codes = []

    non_lab = [c.code for c in qs if not c.code.upper().endswith("L")]
    prefetch_pages(non_lab)

    for c in qs:
        if only_empty:
            if (
                c.prerequisites.count() > 0
                or c.corequisites.count() > 0
                or c.prereq_groups.count() > 0
            ):
                continue

        if c.code.upper().endswith("L"):
            if verbose:
                print(f"SKIP {c.code}: lab course skipped for all-subject batch")
            results["processed"] += 1
            results["skipped_lab"] += 1
            skipped_lab_codes.append(c.code)
            continue

        result = sync_one(
            c.code,
            dry_run=dry_run,
            clear_first=True,
            auto_add_missing=auto_add_missing,
            verbose=verbose,
        )
        results["processed"] += 1
        status = result.get("status")

        if status == "updated":
            results["updated"] += 1
            updated_codes.append(c.code)
        elif status == "manual_review":
            results["manual_review"] += 1
            manual_codes.append(c.code)
        elif status == "fetch_error":
            results["fetch_error"] += 1
            error_codes.append(c.code)
        elif status == "missing_local":
            results["missing_local"] += 1
        elif status == "dry_run":
            pass
        else:
            results["other"] += 1

    print("\n" + "=" * 90)
    print("FINAL SUMMARY")
    print("processed     =", results["processed"])
    print("updated       =", results["updated"])
    print("manual_review =", results["manual_review"])
    print("fetch_error   =", results["fetch_error"])
    print("missing_local =", results["missing_local"])
    print("skipped_lab   =", results["skipped_lab"])
    print("other         =", results["other"])

    print("\nUPDATED CODES:")
    for code in updated_codes:
        print(" ", code)

    print("\nMANUAL REVIEW CODES:")
    for code in manual_codes:
        print(" ", code)

    print("\nFETCH ERROR CODES:")
    for code in error_codes:
        print(" ", code)

    print("\nSKIPPED LAB CODES:")
    for code in skipped_lab_codes:
        print(" ", code)

    results["updated_codes"] = updated_codes
    results["manual_codes"] = manual_codes
    results["error_codes"] = error_codes
    results["skipped_lab_codes"] = skipped_lab_codes
    return results


def dry_run_to_files(subject_filter=None, only_empty=False, auto_add_missing=True):
    result = sync_all(
        dry_run=True,
        only_empty=only_empty,
        subject_filter=subject_filter,
        auto_add_missing=auto_add_missing,
        verbose=False,
    )
    out_dir = Path("dry_run_reports")
    out_dir.mkdir(exist_ok=True)

    summary_path = out_dir / "dry_run_summary.json"
    with open(summary_path, "w") as f:
        json.dump(result, f, indent=2)

    with open(out_dir / "updated_candidates.txt", "w") as f:
        for code in result.get("updated_codes") or []:
            f.write(f"{code}\n")

    with open(out_dir / "manual_review_codes.txt", "w") as f:
        for code in result.get("manual_codes") or []:
            f.write(f"{code}\n")

    with open(out_dir / "fetch_error_codes.txt", "w") as f:
        for code in result.get("error_codes") or []:
            f.write(f"{code}\n")

    print("Saved reports to:", out_dir.resolve())
    print("Summary:", summary_path.resolve())
    return result