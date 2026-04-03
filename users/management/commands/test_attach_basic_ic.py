from django.core.management.base import BaseCommand
from users.models import ProgramRequirement, RequirementBlock, Course


class Command(BaseCommand):
    help = "Attach courses to the Basic Skills Information Competence block only"

    def handle(self, *args, **options):
        req = ProgramRequirement.objects.filter(
            program="BS_CS",
            catalog_year=2023,
        ).first()

        if not req:
            self.stdout.write(self.style.ERROR("ProgramRequirement not found"))
            return

        block = RequirementBlock.objects.filter(
            requirement=req,
            name="Basic Skills Information Competence",
        ).first()

        if not block:
            self.stdout.write(self.style.ERROR("RequirementBlock not found"))
            return

        course_codes = [
            "COMP-100",
            "COMP-300",
            "COMS-323",
            "COMS-356",
            "COMS-360",
            "CTVA-100",
            "CTVA-210",
            "ENGL-311",
            "ENGL-313",
            "ENGL-315",
            "ENGL-371",
        ]

        def normalize(code: str) -> str:
            return (code or "").strip().upper().replace(" ", "").replace("-", "")

        all_courses = Course.objects.filter(
            program="BS_CS",
            catalog_year=2023,
        )

        course_map = {}
        for c in all_courses:
            if c.code:
                course_map[normalize(c.code)] = c

        found = []
        missing = []

        for code in course_codes:
            c = course_map.get(normalize(code))
            if c:
                found.append(c)
            else:
                missing.append(code)

        block.courses.add(*found)

        self.stdout.write(self.style.SUCCESS(
            f"Attached {len(found)} courses to Basic Skills Information Competence"
        ))

        if missing:
            self.stdout.write(self.style.WARNING(
                f"Missing courses: {missing}"
            ))