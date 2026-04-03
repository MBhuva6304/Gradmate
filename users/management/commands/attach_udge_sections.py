from django.core.management.base import BaseCommand
from users.models import ProgramRequirement, RequirementBlock, Course


class Command(BaseCommand):
    help = "Attach UDGE helper section courses to existing requirement blocks without auto-creating missing courses"

    def add_arguments(self, parser):
        parser.add_argument("--program", default="BS_CS")
        parser.add_argument("--catalog", type=int, default=2023)
        parser.add_argument("--mode", choices=["add", "set"], default="set")

    def handle(self, *args, **options):
        program = options["program"]
        catalog = options["catalog"]
        mode = options["mode"]

        req = ProgramRequirement.objects.filter(
            program=program,
            catalog_year=catalog,
        ).first()

        if not req:
            self.stdout.write(self.style.ERROR(
                f"ProgramRequirement not found for program={program}, catalog_year={catalog}"
            ))
            return

        def normalize(code: str) -> str:
            return (code or "").strip().upper().replace(" ", "").replace("-", "")

        def build_course_map():
            course_map = {}
            qs = Course.objects.filter(program=program, catalog_year=catalog)
            for c in qs:
                if c.code:
                    course_map[normalize(c.code)] = c
            return course_map

        course_map = build_course_map()

        def find_block(name: str):
            return RequirementBlock.objects.filter(requirement=req, name=name).first()

        def attach(block_name: str, codes: list[str]):
            block = find_block(block_name)
            if not block:
                self.stdout.write(self.style.WARNING(f"Missing block: {block_name}"))
                return

            found = []
            missing = []
            seen = set()

            for raw_code in codes:
                key = normalize(raw_code)
                if not key or key in seen:
                    continue
                seen.add(key)

                course = course_map.get(key)
                if course:
                    found.append(course)
                else:
                    missing.append(raw_code)

            if mode == "set":
                block.courses.set(found)
            else:
                block.courses.add(*found)

            self.stdout.write(self.style.SUCCESS(
                f"{block_name}: attached {len(found)} course(s)"
            ))

            if missing:
                self.stdout.write(self.style.WARNING(
                    f"{block_name}: NOT FOUND in database ({len(missing)}):"
                ))
                for code in missing:
                    self.stdout.write(f"  - {code}")

                # -------------------------------------------------
        # Ethnic Studies
        # -------------------------------------------------
        ethnic_studies_codes = [
            "AAS-100", "AAS-210", "AAS-220", "AAS-230", "AAS-311", "AAS-321",
            "AAS-340", "AAS-345", "AAS-355", "AAS-360", "AAS-390", "AAS-390F",
            "AAS-440", "AAS-453", "AAS-455",

            "AFRS-100", "AFRS-161", "AFRS-168", "AFRS-171", "AFRS-201", "AFRS-220",
            "AFRS-221", "AFRS-245", "AFRS-246", "AFRS-272", "AFRS-304", "AFRS-320",
            "AFRS-322", "AFRS-324", "AFRS-325", "AFRS-343", "AFRS-351", "AFRS-362",
            "AFRS-366", "AFRS-367", "AFRS-417", "AFRS-420",

            "AIS-101", "AIS-222", "AIS-250", "AIS-301",

            "CAS-100", "CAS-102", "CAS-201", "CAS-309", "CAS-311", "CAS-365",
            "CAS-368", "CAS-369",

            "CHS-100", "CHS-111", "CHS-201", "CHS-246", "CHS-270F", "CHS-270SOC",
            "CHS-306", "CHS-310", "CHS-331", "CHS-345", "CHS-346", "CHS-347",
            "CHS-350", "CHS-351", "CHS-362", "CHS-364", "CHS-365", "CHS-380",
            "CHS-381", "CHS-382", "CHS-390", "CHS-401", "CHS-405", "CHS-417",
            "CHS-430", "CHS-431", "CHS-432", "CHS-434", "CHS-448", "CHS-453",
            "CHS-460", "CHS-467", "CHS-473", "CHS-480", "CHS-480F", "CHS-486A",
        ]

        # -------------------------------------------------
        # Basic Skills Information Competence
        # -------------------------------------------------
        basic_skills_ic_codes = [
            "AAS-113B", "AAS-114B", "AAS-115",
            "AFRS-113B", "AFRS-114B", "AFRS-115",
            "CAS-113B", "CAS-114B", "CAS-115",
            "CHS-113B", "CHS-114B", "CHS-115",
            "COMS-151",
            "ENGL-113B", "ENGL-114B", "ENGL-115", "ENGL-215",
            "LING-113B",
            "QS-113B", "QS-114B", "QS-115",
        ]

        # -------------------------------------------------
        # Subject Explorations Information Competence
        # -------------------------------------------------
        subject_explorations_ic_codes = [
            "AAS-350",
            "ART-305", "ART-315",
            "ASTR-352",

            "BIOL-325", "BIOL-327", "BIOL-362", "BIOL-366", "BIOL-375",

            "CADV-150", "CADV-310",
            "CD-361",
            "CHS-261",
            "CM-336", "CM-336L",

            "COMP-100", "COMP-300",
            "COMS-323", "COMS-356", "COMS-360",
            "CTVA-100", "CTVA-210",
            "DH-320",
            "ENGL-311", "ENGL-313", "ENGL-315", "ENGL-371",

            "FCS-120", "FCS-207", "FCS-323", "FCS-324", "FCS-330", "FCS-340",
            "FIN-302",
            "FLIT-234", "FLIT-381",

            "GEOG-206", "GEOG-206L", "GEOG-365",
            "GEOL-327", "GEOL-344",

            "GWS-300",
            "HIST-161", "HIST-192", "HIST-342", "HIST-349A", "HIST-349B", "HIST-366", "HIST-370", "HIST-371",

            "IS-212",
            "JOUR-365", "JOUR-371", "JOUR-372",
            "JS-300", "JS-378",
            "MKT-350",
            "MSE-302", "MSE-303",
            "MUS-309", "MUS-310",

            "PHIL-165", "PHIL-265", "PHIL-280", "PHIL-349",
            "PHYS-305",

            "PSY-312", "PSY-352", "PSY-365",
            "QS-302",

            "RS-304", "RS-306", "RS-366", "RS-378", "RS-390",

            "RTM-251", "RTM-310", "RTM-310L", "RTM-352",
            "SCI-100",
            "TH-325", "TH-333",
            "UNIV-100",
        ]

        attach("Ethnic Studies", ethnic_studies_codes)
        attach("Basic Skills Information Competence", basic_skills_ic_codes)
        attach("Subject Explorations Information Competence", subject_explorations_ic_codes)

        self.stdout.write(self.style.SUCCESS("Done. No missing course was auto-created."))