# print_subjects.py
import json, sys
print("\n")
path = sys.argv[1] if len(sys.argv) > 1 else "catalog.json"

with open(path, "r", encoding="utf-8") as f:
    data = json.load(f)

# Support both shapes: courses:[... ] or courses:{ id: {...} }
if isinstance(data.get("courses"), dict):
    course_iter = data["courses"].values()
else:
    course_iter = data.get("courses", [])

# Deduplicate case-insensitively, preserve first-seen formatting
unique = {}
for c in course_iter:
    subj = c.get("subject")
    if not isinstance(subj, str):
        continue
    key = " ".join(subj.split()).casefold()  # trim & normalize
    unique.setdefault(key, subj.strip())

# Print subjects (one per line, sorted)
for subj in sorted(unique.values(), key=lambda s: s.casefold()):
    print(subj)

# Optional: show count
print(f"\nTotal subjects: {len(unique)}")
print("\n")
