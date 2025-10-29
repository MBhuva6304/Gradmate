import json
import sys

PATH = sys.argv[1] if len(sys.argv) > 1 else "catalog.json"

# --- Load catalog ---
with open(PATH, "r") as f:
    data = json.load(f)

# Support either: courses = [...] or courses = {id: {...}}
if isinstance(data["courses"], dict):
    courses = data["courses"]
else:
    courses = {c["id"]: c for c in data["courses"]}

def prereq_course_refs(expr):
    """Collect course IDs from a prerequisites/corequisites expression (supports all/any/not)."""
    if not expr:
        return []
    out = []
    if isinstance(expr, dict):
        if "all" in expr and isinstance(expr["all"], list):
            for x in expr["all"]:
                out += prereq_course_refs(x)
        if "any" in expr and isinstance(expr["any"], list):
            for x in expr["any"]:
                out += prereq_course_refs(x)
        if "not" in expr:
            out += prereq_course_refs(expr["not"])
        if "course" in expr and isinstance(expr["course"], str):
            out.append(expr["course"])
    elif isinstance(expr, list):
        for x in expr:
            if isinstance(x, str):
                out.append(x)
            else:
                out += prereq_course_refs(x)
    return out

# --- Build reverse index (computed_opens) from prerequisites ---
computed_opens = {cid: [] for cid in courses}
for target_id, course in courses.items():
    for req_id in prereq_course_refs(course.get("prerequisites")):
        if req_id in computed_opens:
            computed_opens[req_id].append(target_id)

# Merge with any explicit "opens" fields (if present), then sort/dedupe
opens_final = {}
for cid, course in courses.items():
    explicit = set(course.get("opens", []))
    inferred = set(computed_opens.get(cid, []))
    merged = sorted(explicit | inferred)
    if merged:
        opens_final[cid] = merged

# Helper: show human-friendly course code like "AAS 151" (fallback to id)
def show_code(cid):
    c = courses.get(cid, {})
    return c.get("code", cid.replace("-", " "))

# --- Print report ---
print("Open Classes,\n")
for cid in sorted(opens_final, key=lambda x: show_code(x)):
    line = f"{show_code(cid)} : " + ", ".join(show_code(x) for x in opens_final[cid])
    print(line)