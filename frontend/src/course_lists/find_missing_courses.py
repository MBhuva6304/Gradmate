import json, sys, collections

PATH = sys.argv[1] if len(sys.argv) > 1 else "catalog.json"

# ----- load -----
with open(PATH, "r", encoding="utf-8") as f:
    data = json.load(f)

# Support courses as list or dict
if isinstance(data.get("courses"), dict):
    courses_by_id = data["courses"]
elif isinstance(data.get("courses"), list):
    courses_by_id = {c["id"]: c for c in data["courses"] if isinstance(c, dict) and "id" in c}
else:
    courses_by_id = {}

defined_ids = set(courses_by_id.keys())

# ----- helpers -----
def add_ref(bucket, cid, where):
    if isinstance(cid, str) and cid.strip():
        bucket[cid].add(where)

def refs_from_expr(expr, where, bucket):
    """Collect course IDs from prerequisites/corequisites logical expressions."""
    if not expr:
        return
    if isinstance(expr, dict):
        # recursion on logic nodes
        if "all" in expr and isinstance(expr["all"], list):
            for i, x in enumerate(expr["all"]):
                refs_from_expr(x, f"{where}.all[{i}]", bucket)
        if "any" in expr and isinstance(expr["any"], list):
            for i, x in enumerate(expr["any"]):
                refs_from_expr(x, f"{where}.any[{i}]", bucket)
        if "not" in expr:
            refs_from_expr(expr["not"], f"{where}.not", bucket)
        # atom
        if isinstance(expr.get("course"), str):
            add_ref(bucket, expr["course"], where)
        if isinstance(expr.get("courseId"), str):  # if you prefer 'courseId'
            add_ref(bucket, expr["courseId"], where)
    elif isinstance(expr, list):
        # allow shorthand lists like ["COMP-110", {...}]
        for i, x in enumerate(expr):
            if isinstance(x, str):
                add_ref(bucket, x, f"{where}[{i}]")
            else:
                refs_from_expr(x, f"{where}[{i}]", bucket)

def walk_requirements(node, path, bucket):
    """Recurse requirements to collect eligible.courseIds."""
    if not isinstance(node, dict):
        return
    elig = node.get("eligible", {})
    if isinstance(elig, dict):
        ids = elig.get("courseIds")
        if isinstance(ids, list):
            for i, cid in enumerate(ids):
                add_ref(bucket, cid, f"{path}.eligible.courseIds[{i}]")
    # children/options
    for key in ("children", "options"):
        arr = node.get(key, [])
        if isinstance(arr, list):
            for i, ch in enumerate(arr):
                walk_requirements(ch, f"{path}.{key}[{i}]", bucket)

# ----- collect all references to course IDs -----
refs = collections.defaultdict(set)

# 1) requirements
reqs = data.get("requirements", {})
for i, area in enumerate(reqs.get("areas", [])):
    walk_requirements(area, f"requirements.areas[{i}]", refs)

# 2) crosslistClusters
for i, cl in enumerate(data.get("crosslistClusters", []) or []):
    for j, cid in enumerate(cl.get("members", []) or []):
        add_ref(refs, cid, f"crosslistClusters[{i}].members[{j}]")

# 3) per-course references
for cid, c in courses_by_id.items():
    refs_from_expr(c.get("prerequisites"), f"courses.{cid}.prerequisites", refs)
    refs_from_expr(c.get("corequisites"), f"courses.{cid}.corequisites", refs)
    if isinstance(c.get("pairedWith"), str):
        add_ref(refs, c["pairedWith"], f"courses.{cid}.pairedWith")
    # optional legacy lists if present
    for field in ("opens", "crossListedWith"):
        vals = c.get(field, [])
        if isinstance(vals, list):
            for j, v in enumerate(vals):
                add_ref(refs, v, f"courses.{cid}.{field}[{j}]")

# ----- compute undefined -----
referenced_ids = set(refs.keys())
undefined = sorted(referenced_ids - defined_ids)

# ----- output -----
print("\n=== Undefined course IDs (referenced but not defined in 'courses') ===")
if not undefined:
    print("(none) ✅")
else:
    for mid in undefined:
        locations = "; ".join(sorted(refs[mid]))
        print(f"- {mid}  <-- referenced at: {locations}")

# Non-zero exit if any undefined (useful in CI)
