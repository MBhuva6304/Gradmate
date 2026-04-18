import re
import time
import requests
from bs4 import BeautifulSoup

from django.db import transaction
from users.models import Course, PrerequisiteGroup

BASE = "https://catalog.csun.edu"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; Gradmate prerequisite sync)"
}


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


def extract_text_from_course_page(code):
    url = get_course_page_url(code)
    soup = get_soup(url)

    title = ""
    h1 = soup.find(["h1", "h2"])
    if h1:
        title = norm(h1.get_text(" ", strip=True))

    full_text = norm(soup.get_text(" ", strip=True))

    def grab(label):
        m = re.search(
            rf"\b{label}:\s*(.+?)(?=(?:\bCorequisites?:|\bRecommended Preparatory:|\bFormerly:|\bCrosslisted:|\bCredit will not be allowed|\bSchedule of Classes\b|\bTop\b|\bView Catalog Archives\b|\bResources\b|$))",
            full_text,
            flags=re.IGNORECASE,
        )
        if not m:
            return ""

        text = norm(m.group(1)).rstrip(" .;")
        text = re.split(r"\bIntroduction to\b", text, maxsplit=1, flags=re.IGNORECASE)[0]
        text = re.split(r"\bSpring-\d{4}\b", text, maxsplit=1)[0]
        text = re.split(r"\bFall-\d{4}\b", text, maxsplit=1)[0]
        text = re.split(r"\bSchedule of Classes\b", text, maxsplit=1, flags=re.IGNORECASE)[0]

        first_sentence = re.split(r"(?<=[.!?])\s+(?=[A-Z])", text)[0]
        return norm(first_sentence).rstrip(" .;")

    prereq_text = grab("Prerequisites?")
    coreq_text = grab("Corequisites?")

    return {
        "title": title,
        "prereq_text": prereq_text,
        "coreq_text": coreq_text,
        "url": url,
    }


def split_top_level_semicolons(text):
    parts = [norm(x) for x in text.split(";")]
    return [x for x in parts if x]


def extract_course_codes(raw_text, keep_lab_pair=False):
    text = norm(raw_text)
    found = []

    # Handles:
    # COMP 110
    # COMP 110L
    # COMP 111A
    # COMP 111AL
    # COMP 110/L
    pattern = re.compile(r"\b([A-Z]{2,6})\s+([0-9]{2,3}[A-Z]{0,2})(/L)?\b")

    for subj, num, slash_lab in pattern.findall(text):
        subj = subj.upper()
        num = num.upper()

        # case 1: explicit slash-lab like COMP 110/L
        if slash_lab:
            found.append(f"{subj}-{num}")
            if keep_lab_pair:
                found.append(f"{subj}-{num}L")
            continue

        # case 2: explicit lab code already written like COMP 110L or COMP 111AL
        if num.endswith("L"):
            if keep_lab_pair:
                found.append(f"{subj}-{num}")
            else:
                found.append(f"{subj}-{num[:-1]}")
            continue

        # normal case
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


def resolve_codes_to_courses(codes):
    resolved = []
    missing = []
    for code in codes:
        obj = COURSE_MAP.get(code.upper())
        if obj:
            resolved.append(obj)
        else:
            missing.append(code)
    return resolved, missing


def parse_path_token(token):
    """
    COMP 110/L  -> ['COMP-110']
    COMP 111B/L -> ['COMP-111B']
    MATH 150A   -> ['MATH-150A']
    """
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
    """
    Returns list of alternative single-course options.

    Examples:
      'COMP 110/L or COMP 111B/L'
      -> ['COMP-110', 'COMP-111B']

      'MATH 103 , MATH 104, MATH 105 , MATH 150A or MATH 255A'
      -> ['MATH-103', 'MATH-104', 'MATH-105', 'MATH-150A', 'MATH-255A']
    """
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


def apply_prerequisites(course, prereq_text, dry_run=True):
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

        # Skip non-course conditions for now, but note them
        if (
            "lower division writing requirement" in part.lower()
            or "consent of instructor" in part.lower()
            or "junior standing" in part.lower()
            or "senior standing" in part.lower()
        ):
            manual_notes.append(part)
            continue

        options = parse_any_options(part)

        if options:
            options = [c for c in options if c != course.code.upper()]
            objs, missing = resolve_codes_to_courses(options)
            unresolved.extend(missing)
            if objs:
                any_groups_to_create.append(objs)
            continue

        codes = [c for c in extract_course_codes(part, keep_lab_pair=False) if c != course.code.upper()]
        if not codes:
            if part:
                manual_notes.append(part)
            continue

        objs, missing = resolve_codes_to_courses(codes)
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


def apply_corequisites(course, coreq_text, dry_run=True):
    codes = [c for c in extract_course_codes(coreq_text, keep_lab_pair=True) if c != course.code.upper()]
    objs, missing = resolve_codes_to_courses(codes)

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


def sync_one(code, dry_run=True, clear_first=True):
    code = code.upper().strip()
    course = COURSE_MAP.get(code)
    if not course:
        print(f"SKIP {code}: not found in local DB")
        return {"status": "missing_local"}

    try:
        data = extract_text_from_course_page(code)
    except Exception as e:
        print(f"ERROR {code}: fetch failed -> {e}")
        return {"status": "fetch_error", "error": str(e)}

    print("=" * 90)
    print(code, "|", course.title)
    print("URL:", data["url"])
    print("PREREQ TEXT:", data["prereq_text"] or "-")
    print("COREQ TEXT :", data["coreq_text"] or "-")

    pre = apply_prerequisites(course, data["prereq_text"], dry_run=True)
    co = apply_corequisites(course, data["coreq_text"], dry_run=True)

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
        print("  SKIPPED WRITE -> manual review needed")
        return {
            "status": "manual_review",
            "needs_manual": True,
            "prereq_text": data["prereq_text"],
            "coreq_text": data["coreq_text"],
            "url": data["url"],
            "pre": pre,
            "co": co,
        }

    with transaction.atomic():
        if clear_first:
            clear_existing_requisites(course)

        apply_prerequisites(course, data["prereq_text"], dry_run=False)
        apply_corequisites(course, data["coreq_text"], dry_run=False)

        course.prereq_mode = "ALL"
        course.save(update_fields=["prereq_mode"])

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


def sync_subject(subject_abbr, dry_run=True, limit=None, only_empty=False):
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
        "other": 0,
        "codes_updated": [],
        "codes_manual_review": [],
        "codes_fetch_error": [],
        "codes_missing_local": [],
    }

    for c in qs:
        result = sync_one(c.code, dry_run=dry_run, clear_first=True)
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

    return results


def sync_all(dry_run=True, only_empty=False, subject_filter=None):
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
        "other": 0,
    }

    updated_codes = []
    manual_codes = []
    error_codes = []

    for c in qs:
        if only_empty:
            if (
                c.prerequisites.count() > 0
                or c.corequisites.count() > 0
                or c.prereq_groups.count() > 0
            ):
                continue

        result = sync_one(c.code, dry_run=dry_run, clear_first=True)
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

    return results