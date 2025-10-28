# sort_courses.py
import json, sys, pathlib

def sort_courses_inplace(path: str):
    p = pathlib.Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))

    if "courses" not in data:
        raise SystemExit("No 'courses' key found in catalog.json.")

    courses = data["courses"]

    # Case 1: courses is a list of course objects
    if isinstance(courses, list):
        for i, c in enumerate(courses):
            if not isinstance(c, dict) or "id" not in c:
                raise SystemExit(f"Item {i} in courses has no 'id'.")
        data["courses"] = sorted(courses, key=lambda c: c["id"])

    # Case 2: courses is a dict keyed by id
    elif isinstance(courses, dict):
        data["courses"] = {k: courses[k] for k in sorted(courses.keys())}

    else:
        raise SystemExit("'courses' must be a list or a dict.")

    # Write back (pretty) + backup
    backup = p.with_suffix(p.suffix + ".bak")
    backup.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Sorted courses by id. Updated: {p.name}  |  Backup: {backup.name}")

if __name__ == "__main__":
    # Default to catalog.json unless a path is passed
    infile = sys.argv[1] if len(sys.argv) > 1 else "catalog.json"
    sort_courses_inplace(infile)