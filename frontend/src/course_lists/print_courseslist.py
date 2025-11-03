import json

# Load catalog.json
with open("catalog.json", "r", encoding="utf-8") as f:
    data = json.load(f)

courses = data.get("courses", [])

print("Courses found in catalog.json:\n")

# Works for list or dict formats — sorted alphabetically by course ID
if isinstance(courses, list):
    for c in sorted(courses, key=lambda x: x["id"]):
        print(f"- {c['id']}: {c.get('title', '')}")

elif isinstance(courses, dict):
    for cid in sorted(courses.keys()):
        c = courses[cid]
        print(f"- {cid}: {c.get('title', '')}")

else:
    print("⚠️  'courses' is not a list or dictionary.")

print(f"\nTotal courses in catalog.json: {len(courses)}")