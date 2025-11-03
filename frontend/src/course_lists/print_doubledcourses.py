import json
from collections import Counter, defaultdict

PATH = "catalog.json"

with open(PATH, "r", encoding="utf-8") as f:
    data = json.load(f)

courses = data.get("courses", [])

# ---- Normalize to dict[id] -> course; also collect raw ids for duplicate detection in lists ----
id_counts = Counter()
courses_by_id = {}

if isinstance(courses, list):
    for c in courses:
        cid = c["id"]
        id_counts[cid] += 1
        # keep first occurrence; we only need one copy for the rest of checks
        courses_by_id.setdefault(cid, c)
elif isinstance(courses, dict):
    courses_by_id = courses
    id_counts = Counter(courses.keys())
else:
    raise TypeError("'courses' must be a list or a dict")

# ---- 1) Duplicate IDs (most important) ----
dupe_ids = [cid for cid, n in id_counts.items() if n > 1]
if dupe_ids:
    print("❌ Duplicate course IDs found (count > 1):")
    for cid in sorted(dupe_ids):
        print(f"  - {cid}  (x{id_counts[cid]})")
else:
    print("✅ No duplicate course IDs.")

# ---- 2) Same 'code' used by multiple different IDs (unusual) ----
code_map = defaultdict(list)
for cid, c in courses_by_id.items():
    code_map[c.get("code", "").strip()].append(cid)

dupe_codes = {code: ids for code, ids in code_map.items() if code and len(ids) > 1}
if dupe_codes:
    print("\n⚠️  Same 'code' appears on multiple course IDs:")
    for code in sorted(dupe_codes):
        print(f"  - {code}: {', '.join(sorted(dupe_codes[code]))}")
else:
    print("\n✅ No duplicate 'code' values across different IDs.")

# ---- 3) Same (subject, number) across different IDs (also unusual) ----
sn_map = defaultdict(list)
for cid, c in courses_by_id.items():
    subj = (c.get("subject") or "").strip()
    num  = (c.get("number")  or "").strip()
    if subj and num:
        sn_map[(subj, num)].append(cid)

dupe_sn = {k: v for k, v in sn_map.items() if len(v) > 1}
if dupe_sn:
    print("\n⚠️  Same (subject, number) used by multiple IDs:")
    for (subj, num), ids in sorted(dupe_sn.items()):
        print(f"  - ({subj}, {num}): {', '.join(sorted(ids))}")
else:
    print("\n✅ No duplicate (subject, number) pairs across different IDs.")

# ---- Optional: summarize counts ----
total = sum(id_counts.values()) if isinstance(courses, list) else len(courses_by_id)
print(f"\nTotal course entries scanned: {total}")