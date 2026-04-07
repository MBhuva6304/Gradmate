from collections import defaultdict


def assign_course_to_blocks(course, eligible_blocks):
    grouped = defaultdict(list)
    for block in eligible_blocks:
        group_name = getattr(block, "count_group", "group4") or "group4"
        grouped[group_name].append(block)

    applied = []

    group1_blocks = grouped.get("group1", [])
    if group1_blocks:
        applied.append(group1_blocks[0])

    for gname in ("group2", "group3", "group4"):
        applied.extend(grouped.get(gname, []))

    return {
        "eligible_blocks": eligible_blocks,
        "applied_blocks": applied,
    }


def build_course_counting_map(courses, requirement):
    result = {}
    if not requirement:
        return result

    blocks = list(requirement.blocks.prefetch_related("courses").all())

    course_to_blocks = defaultdict(list)
    for block in blocks:
        for block_course in block.courses.all():
            course_to_blocks[block_course.id].append(block)

    for course in courses:
        eligible = course_to_blocks.get(course.id, [])
        assigned = assign_course_to_blocks(course, eligible)

        result[course.id] = {
            "eligible_blocks": assigned["eligible_blocks"],
            "applied_blocks": assigned["applied_blocks"],
            "eligible_names": [b.name for b in assigned["eligible_blocks"]],
            "applied_names": [b.name for b in assigned["applied_blocks"]],
            "eligible_count": len(assigned["eligible_blocks"]),
            "applied_count": len(assigned["applied_blocks"]),
            "is_multi_count": len(assigned["applied_blocks"]) > 1,
        }

    return result


def apply_program_specific_rules(course, requirement, student_profile, eligible_blocks):
    extra_blocks = []

    if student_profile.program == "BS_CS" and student_profile.catalog_year == 2023:
        if course.code == "COMP-310":
            extra_blocks.append("B5 Upper Division Scientific Inquiry")

        if course.code in {"COMP-110", "COMP-110L", "COMP-111B", "COMP-111BL"}:
            extra_blocks.append("E Lifelong Learning")

    return eligible_blocks, extra_blocks