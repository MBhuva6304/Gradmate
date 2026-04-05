from django.core.management.base import BaseCommand
from users.models import ProgramRequirement, RequirementBlock, Course


class Command(BaseCommand):
    help = "Attach existing courses to requirement blocks from the BS_CS 2023 mapping. No auto-create."

    def add_arguments(self, parser):
        parser.add_argument("--program", default="BS_CS")
        parser.add_argument("--catalog", type=int, default=2023)
        parser.add_argument("--mode", choices=["add", "set"], default="add")

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
            return (
                (code or "")
                .strip()
                .upper()
                .replace(" ", "")
                .replace("-", "")
                .replace("/", "")
            )

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

            existing_ids = set(block.courses.values_list("id", flat=True))
            found = []
            missing = []
            seen = set()

            for raw_code in codes:
                key = normalize(raw_code)
                if not key or key in seen:
                    continue
                seen.add(key)

                course = course_map.get(key)
                if not course:
                    missing.append(raw_code)
                    continue

                if mode == "set":
                    found.append(course)
                else:
                    if course.id not in existing_ids:
                        found.append(course)

            if mode == "set":
                block.courses.set(found)
                self.stdout.write(self.style.SUCCESS(
                    f"{block_name}: set {len(found)} course(s)"
                ))
            else:
                if found:
                    block.courses.add(*found)
                self.stdout.write(self.style.SUCCESS(
                    f"{block_name}: added {len(found)} new course(s)"
                ))

            if missing:
                self.stdout.write(self.style.WARNING(
                    f"{block_name}: NOT FOUND in database ({len(missing)}):"
                ))
                for code in missing:
                    self.stdout.write(f"  - {code}")

        block_to_codes = {
            # ---------------- GE ----------------
            "A1 Oral Communication": [
                "AAS-151", "AFRS-151", "CAS-151", "CHS-151", "COMS-151", "QS-151",
            ],
            "A2 Written Communication": [
                "AAS-113B", "AAS-114B", "AAS-115",
                "AFRS-113B", "AFRS-114B", "AFRS-115",
                "CAS-113B", "CAS-114B", "CAS-115",
                "CHS-113B", "CHS-114B", "CHS-115",
                "ENGL-113B", "ENGL-114B", "ENGL-115",
                "LING-113B",
                "QS-113B", "QS-114B", "QS-115",
            ],
            "A3 Critical Thinking": [
                "AAS-201", "AFRS-204", "AIS-210", "CHS-202", "COMS-225",
                "ENGL-215", "GEH-111HON", "HIST-202", "JS-220",
                "PHIL-100", "PHIL-200", "PHIL-230", "QS-201", "RS-204",
            ],
            "B1 Physical Science": [
                "ASTR-152", "ASTR-154", "CHEM-100", "CHEM-101", "CHEM-102",
                "CHEM-103", "CHEM-104", "CHEM-110", "GEOG-101", "GEOG-101A",
                "GEOG-103", "GEOG-103A", "GEOG-112", "GEOL-101", "GEOL-107",
                "GEOL-110", "GEOL-113", "GEOL-117", "GEOL-122", "GEOL-125",
                "PHYS-100A", "PHYS-100B", "PHYS-220A", "PHYS-220B",
                "SCI-111", "SUST-111",
            ],
            "B2 Life Science": [
                "ANTH-151", "BIOL-100", "BIOL-101", "BIOL-106", "BIOL-107",
                "BIOL-218", "BIOL-292", "GEOL-110", "GEOL-113", "GEOL-125",
            ],
            "B3 Laboratory Activity": [
                "ASTR-154L", "BIOL-100L", "BIOL-101L", "BIOL-106L", "BIOL-107L",
                "BIOL-218L", "BIOL-292L", "CHEM-100L", "CHEM-101L", "CHEM-102L",
                "CHEM-103L", "CHEM-104L", "CHEM-110L", "GEOG-101AL", "GEOG-103AL",
                "GEOG-112L", "GEOL-102", "GEOL-107L", "GEOL-112", "GEOL-117L",
                "GEOL-123", "PHYS-100AL", "PHYS-100BL", "PHYS-220AL", "PHYS-220BL",
                "SCI-111L", "SUST-111L",
            ],
            "B4 Math / Quantitative Reasoning": [
                "COMP-102", "COMP-102L",
                "MATH-102", "MATH-103", "MATH-105", "MATH-106", "MATH-131",
                "MATH-140", "MATH-140BUS", "MATH-140SCI", "MATH-141", "MATH-141L",
                "MATH-150A", "MATH-255A", "PHIL-135",
            ],
            "B5 Upper Division Scientific Inquiry": [
                "ANTH-341", "ASTR-352", "BIOL-306", "BIOL-323", "BIOL-324",
                "BIOL-325", "BIOL-327", "BIOL-341", "BIOL-362", "BIOL-366",
                "BIOL-375", "CM-336", "CM-336L", "EOH-353", "FCS-315", "FCS-323",
                "FCS-324", "GEH-333HON", "GEOG-311", "GEOG-316", "GEOG-365",
                "GEOG-366", "GEOL-300", "GEOL-324", "GEOL-327", "GEOL-344",
                "HSCI-336", "HSCI-337", "HSCI-345", "KIN-309", "LING-303",
                "LING-310", "MATH-331", "MSE-303", "PHIL-325", "PHYS-305",
                "PHYS-331", "QS-369", "RS-366",
            ],
            "C1 Arts": [
                "AFRS-246", "AFRS-280", "AFRS-351", "ANTH-232", "ART-100", "ART-100L",
                "ART-110", "ART-114", "ART-124A", "ART-140", "ART-141", "ART-305",
                "CHS-111", "CHS-310", "COMS-104", "COMS-305", "CTVA-210", "CTVA-215",
                "CTVA-309", "CTVA-323", "ENGL-208", "FCS-111", "FLIT-151", "FLIT-250",
                "HUM-101", "HUM-105", "HUM-106", "JS-300", "KIN-139A", "KIN-144A",
                "KIN-236", "KIN-236L", "KIN-380", "KIN-380L", "LING-240", "MUS-105",
                "MUS-106HH", "MUS-107", "MUS-108", "MUS-306", "PHIL-314", "TH-110",
                "TH-111", "TH-310",
            ],
            "C2 Humanities": [
                "AAS-220", "AAS-321", "AFRS-245", "AFRS-343", "AFRS-344", "AFRS-346",
                "AFRS-352", "AIS-301", "AIS-318", "ANTH-222", "ANTH-326", "CAS-201",
                "CHS-201", "CHS-350", "CHS-351", "CHS-380", "CHS-381", "CHS-382",
                "CLAS-315", "CTVA-215", "DH-320", "ENGL-254", "ENGL-255", "ENGL-258",
                "ENGL-259", "ENGL-275", "ENGL-300", "ENGL-316", "ENGL-318", "ENGL-322",
                "ENGL-333", "ENGL-364", "FLIT-151", "FLIT-331", "FLIT-381", "GWS-100",
                "GWS-230", "GWS-351", "HIST-150", "HIST-151", "HIST-303", "HIST-304",
                "HUM-101", "HUM-105", "HUM-106", "JS-100", "JS-255", "JS-300", "JS-333",
                "LING-200", "PHIL-150", "PHIL-165", "PHIL-170", "PHIL-201", "PHIL-202",
                "PHIL-240", "PHIL-250", "PHIL-260", "PHIL-265", "PHIL-280", "PHIL-310",
                "PHIL-314", "PHIL-325", "PHIL-330", "PHIL-337", "PHIL-349", "PHIL-353",
                "PHIL-354", "QS-101", "QS-303", "RS-100", "RS-101", "RS-304", "RS-307",
                "RS-310", "RS-356", "RS-361", "RS-362", "RS-370", "SUST-240", "TH-333",
            ],
            "C3 American History": [
                "AFRS-271", "AFRS-272", "AIS-250", "CHS-245", "ECON-175",
                "HIST-270", "HIST-271", "HIST-370", "HIST-371", "PHIL-317", "RS-256",
            ],
            "D1 Social Sciences": [
                "AAS-210", "AAS-350", "AFRS-201", "AFRS-220", "AFRS-221", "AFRS-304",
                "AFRS-361", "AIS-222", "ANTH-150", "ANTH-151", "ANTH-152", "ANTH-153",
                "ANTH-212", "ANTH-250", "ANTH-262", "ANTH-302", "ANTH-305", "ANTH-319",
                "ANTH-341", "CADV-150", "CAS-309", "CAS-310", "CAS-368", "CAS-369",
                "CHS-261", "CHS-331", "CHS-345", "CHS-346", "CHS-347", "CHS-361",
                "CHS-362", "CHS-366", "COMS-312", "COMS-323", "ECON-101", "ECON-160",
                "ECON-161", "ECON-310", "ECON-311", "ECON-360", "FCS-253", "FCS-256",
                "FCS-318", "FCS-340", "FCS-357", "FLIT-325", "GEH-333HON", "GEOG-107",
                "GEOG-150", "GEOG-170", "GEOG-301", "GEOG-321", "GEOG-330", "GEOG-351",
                "GEOG-370", "GWS-110", "GWS-220", "GWS-222", "GWS-300", "GWS-320",
                "GWS-340", "GWS-351", "GWS-370", "HIST-110", "HIST-111", "HIST-305",
                "HIST-341", "HIST-342", "HIST-350", "HIST-380", "HIST-389", "HSCI-132",
                "HSCI-345", "HSCI-369", "JOUR-365", "JS-318", "LING-230", "LING-309",
                "MKT-350", "PHIL-305", "PHIL-391", "POLS-156", "POLS-225", "POLS-310",
                "POLS-350", "POLS-380", "PSY-150", "PSY-312", "PSY-352", "PSY-365",
                "RS-240", "SOC-150", "SOC-200", "SOC-305", "SOC-324", "SUST-300",
                "URBS-150", "URBS-310", "URBS-380",
            ],
            "D3 Constitution of the U.S.": [
                "AAS-347", "AFRS-161", "CHS-260", "CHS-445", "POLS-155", "POLS-355", "RS-255",
            ],
            "D4 California State and Local Government": [
                "AAS-347", "AFRS-161", "CHS-260", "CHS-445", "POLS-155", "POLS-355", "RS-255",
                "POLS-403", "POLS-490CA",
            ],
            "E Lifelong Learning": [
                "AAS-230", "AAS-390", "AAS-390F", "AFRS-337", "AIS-301", "ART-151", "ART-201",
                "BIOL-327", "BIOL-375", "BLAW-280", "BLAW-368", "BUS-104", "CADV-310",
                "CAS-270", "CAS-270F", "CCE-200", "CD-133", "CD-361", "CHS-270SOC", "CHS-270F",
                "CHS-347", "CHS-360", "CHS-390", "CJS-340", "CM-336", "CM-336L", "COMP-100",
                "COMP-300", "COMS-150", "COMS-251", "COMS-323", "COMS-360", "CTVA-100",
                "CTVA-323", "ENGL-253", "ENGL-306", "ENGL-313", "ENGL-315", "ENT-101",
                "EOH-101", "EOH-353", "FCS-120", "FCS-171", "FCS-207", "FCS-260", "FCS-315",
                "FCS-323", "FCS-324", "FCS-330", "FCS-340", "FIN-102", "FIN-302", "FLIT-234",
                "GEOG-206", "GEOG-206L", "GEOL-104", "GWS-305", "GWS-305CS", "HIST-366",
                "HSCI-131", "HSCI-170", "HSCI-231", "HSCI-336", "HSCI-337", "IS-212",
                "JOUR-100", "JOUR-390", "JS-390CS", "KIN-115A", "KIN-117", "KIN-118",
                "KIN-123A", "KIN-124A", "KIN-125A", "KIN-126A", "KIN-128", "KIN-129A",
                "KIN-130A", "KIN-131A", "KIN-132A", "KIN-133A", "KIN-135A", "KIN-142B",
                "KIN-147", "KIN-148", "KIN-149", "KIN-152A", "KIN-153", "KIN-172",
                "KIN-177A", "KIN-178A", "KIN-179A", "KIN-185A", "KIN-195A", "LING-310",
                "ME-100", "MSE-303", "PHIL-165", "PHIL-180", "PHIL-250", "PHIL-260",
                "PHIL-280", "PHIL-305", "PHIL-337", "QS-302", "RTM-251", "RTM-278",
                "RTM-310", "RTM-310L", "RTM-352", "RTM-353", "RTM-353L", "SCI-100",
                "SUST-310", "TH-243", "UNIV-100",
            ],
            "F Comparative Cultural Studies": [
                "AAS-100", "AAS-340", "AAS-345", "AAS-360", "AFRS-100", "AFRS-226",
                "AFRS-300", "AFRS-320", "AFRS-322", "AFRS-324", "AFRS-325", "AFRS-366",
                "AIS-101", "AIS-304", "AIS-318", "AIS-333", "ANTH-108", "ANTH-308",
                "ANTH-310", "ANTH-315", "ANTH-345", "ARAB-101", "ARAB-102", "ARMN-101",
                "ARMN-102", "ARMN-310", "ARMN-360", "ART-112", "ART-315", "BLAW-391",
                "CAS-100", "CAS-102", "CAS-311", "CAS-365", "CHIN-101", "CHIN-102",
                "CHS-100", "CHS-101", "CHS-246", "CHS-333", "CHS-364", "CHS-365",
                "CLAS-101L", "COMS-356", "COMS-360", "ENGL-311", "ENGL-318", "ENGL-371",
                "FLIT-150", "FLIT-370", "FLIT-371", "FLIT-380", "FREN-101", "FREN-102",
                "GEOG-318", "GEOG-322", "GEOG-324", "GEOG-326", "GEOG-334", "GWS-100",
                "GWS-110", "GWS-300", "GWS-351", "HEBR-101", "HEBR-102", "HIST-161",
                "HIST-185", "HIST-192", "HIST-210", "HIST-349A", "HIST-349B", "HIST-369",
                "ITAL-101", "ITAL-102", "ITAL-201", "JAPN-101", "JAPN-102", "JAPN-201",
                "JAPN-202", "JAPN-204", "JOUR-371", "JOUR-372", "JS-210", "JS-306",
                "JS-330", "JS-335", "JS-378", "KIN-385", "KOR-101", "KOR-102", "LING-250",
                "LING-325", "MSE-302", "MUS-309", "MUS-310", "PERS-101", "PERS-102",
                "PERS-201", "PHIL-333", "PHIL-343", "PHIL-344", "PHIL-348", "POLS-197",
                "POLS-321", "POLS-332", "QS-101", "QS-208", "QS-301", "QS-303", "QS-304",
                "RS-150", "RS-306", "RS-365", "RS-378", "RS-380", "RS-385", "RS-390",
                "RTM-310", "RTM-310L", "RTM-330", "RUSS-101", "RUSS-102", "RUSS-201",
                "RUSS-202", "SOC-306", "SOC-307", "SOC-335", "SPAN-101", "SPAN-102",
                "SPAN-103", "SPAN-220A", "SPAN-220B", "SPED-200SL", "TH-325", "URBS-350",
            ],
            "Ethnic Studies": [
                "AAS-100", "AAS-210", "AAS-220", "AAS-230", "AAS-311", "AAS-321",
                "AAS-340", "AAS-345", "AAS-355", "AAS-360", "AAS-390", "AAS-390F",
                "AAS-440", "AAS-453", "AAS-455",
                "AFRS-100", "AFRS-161", "AFRS-168", "AFRS-171", "AFRS-201", "AFRS-220",
                "AFRS-221", "AFRS-245", "AFRS-246", "AFRS-272", "AFRS-304", "AFRS-320",
                "AFRS-322", "AFRS-324", "AFRS-325", "AFRS-343", "AFRS-351", "AFRS-362",
                "AFRS-366", "AFRS-367", "AFRS-417", "AFRS-420",
                "AIS-101", "AIS-222", "AIS-250", "AIS-301",
                "CAS-100", "CAS-102", "CAS-201", "CAS-309", "CAS-311", "CAS-365", "CAS-368", "CAS-369",
                "CHS-100", "CHS-111", "CHS-201", "CHS-246", "CHS-270F", "CHS-270SOC",
                "CHS-306", "CHS-310", "CHS-331", "CHS-345", "CHS-346", "CHS-347",
                "CHS-350", "CHS-351", "CHS-362", "CHS-364", "CHS-365", "CHS-380",
                "CHS-381", "CHS-382", "CHS-390", "CHS-401", "CHS-405", "CHS-417",
                "CHS-430", "CHS-431", "CHS-432", "CHS-434", "CHS-448", "CHS-453",
                "CHS-460", "CHS-467", "CHS-473", "CHS-480", "CHS-480F", "CHS-486A",
            ],
            "Basic Skills Information Competence": [
                "AAS-113B", "AAS-114B", "AAS-115",
                "AFRS-113B", "AFRS-114B", "AFRS-115",
                "CAS-113B", "CAS-114B", "CAS-115",
                "CHS-113B", "CHS-114B", "CHS-115",
                "COMS-151", "ENGL-113B", "ENGL-114B", "ENGL-115", "ENGL-215",
                "LING-113B", "QS-113B", "QS-114B", "QS-115",
            ],
            "Subject Explorations Information Competence": [
                "AAS-350", "ART-305", "ART-315", "ASTR-352",
                "BIOL-325", "BIOL-327", "BIOL-362", "BIOL-366", "BIOL-375",
                "CADV-150", "CADV-310", "CD-361", "CHS-261", "CM-336", "CM-336L",
                "COMP-100", "COMP-300", "COMS-323", "COMS-356", "COMS-360",
                "CTVA-100", "CTVA-210", "DH-320", "ENGL-311", "ENGL-313", "ENGL-315", "ENGL-371",
                "FCS-120", "FCS-207", "FCS-323", "FCS-324", "FCS-330", "FCS-340",
                "FIN-302", "FLIT-234", "FLIT-381", "GEOG-206", "GEOG-206L",
                "GEOG-365", "GEOL-327", "GEOL-344", "GWS-300", "HIST-161", "HIST-192",
                "HIST-342", "HIST-349A", "HIST-349B", "HIST-366", "HIST-370", "HIST-371",
                "IS-212", "JOUR-365", "JOUR-371", "JOUR-372", "JS-300", "JS-378",
                "MKT-350", "MSE-302", "MSE-303", "MUS-309", "MUS-310", "PHIL-165",
                "PHIL-265", "PHIL-280", "PHIL-349", "PHYS-305", "PSY-312", "PSY-352",
                "PSY-365", "QS-302", "RS-304", "RS-306", "RS-366", "RS-378", "RS-390",
                "RTM-251", "RTM-310", "RTM-310L", "RTM-352", "SCI-100", "TH-325", "TH-333", "UNIV-100",
            ],

            # ---------------- Major ----------------
            "Lower Division Algorithms & Programming": [
                "COMP-110", "COMP-110L", "COMP-111A", "COMP-111AL", "COMP-111B", "COMP-111BL",
            ],
            "Lower Division Major": [
                "COMP-122", "COMP-182", "COMP-222", "COMP-256",
                "COMP-256L", "COMP-282", "MATH-150A", "MATH-150B", "MATH-262", "PHIL-230",
            ],
            "Lower Division Life Science": [
                "BIOL-106", "BIOL-106L", "BIOL-107", "BIOL-107L", "GEOL-110", "GEOL-112",
            ],
            "Lower Division Physical Science": [
                "CHEM-101", "CHEM-101D", "CHEM-101L",
                "GEOG-101", "GEOG-102",
                "GEOG-103", "GEOG-105",
                "GEOL-101", "GEOL-102",
                "GEOL-110", "GEOL-112",
                "PHYS-220A", "PHYS-220AL",
            ],
            "Computer Science Upper Division Core": [
                "COMP-310", "COMP-322", "COMP-322L", "COMP-324", "COMP-333",
                "COMP-380", "COMP-380L", "COMP-482", "MATH-482",
                "COMP-490", "COMP-490L", "COMP-491", "COMP-491L", "MATH-340",
            ],
            "Senior Electives": [
                "COMP-410", "COMP-424", "COMP-429", "COMP-440", "COMP-467",
                "COMP-529", "COMP-529L", "COMP-541", "COMP-583",
            ],

            # ---------------- UD helper sections ----------------
            "C Upper Division Arts and Humanities": [
                "AAS-321", "AFRS-343", "AFRS-344", "AFRS-346", "AFRS-351", "AFRS-352",
                "AIS-301", "AIS-318", "ANTH-326", "ART-305", "CHS-310", "CHS-350",
                "CHS-351", "CHS-380", "CHS-381", "CHS-382", "CLAS-315", "COMS-305",
                "CTVA-309", "CTVA-323", "DH-320", "ENGL-300", "ENGL-316", "ENGL-318",
                "ENGL-322", "ENGL-333", "ENGL-364", "FLIT-331", "FLIT-381", "GWS-351",
                "HIST-303", "HIST-304", "HIST-370", "HIST-371", "JS-300", "JS-333",
                "KIN-380", "KIN-380L", "MUS-306", "PHIL-310", "PHIL-314", "PHIL-317",
                "PHIL-325", "PHIL-330", "PHIL-337", "PHIL-349", "PHIL-353", "PHIL-354",
                "QS-303", "RS-304", "RS-307", "RS-310", "RS-356", "RS-361", "RS-362",
                "RS-370", "TH-310", "TH-333",
            ],
            "D Upper Division Social Sciences": [
                "AAS-347", "AAS-350", "AFRS-304", "AFRS-361", "ANTH-302", "ANTH-305",
                "ANTH-319", "ANTH-341", "CAS-309", "CAS-310", "CAS-368", "CAS-369",
                "CHS-331", "CHS-345", "CHS-346", "CHS-347", "CHS-361", "CHS-362",
                "CHS-366", "COMS-312", "COMS-323", "ECON-310", "ECON-311", "ECON-360",
                "FCS-318", "FCS-340", "FCS-357", "FLIT-325", "GEH-333HON", "GEOG-301",
                "GEOG-321", "GEOG-330", "GEOG-351", "GEOG-370", "GWS-300", "GWS-320",
                "GWS-340", "GWS-351", "GWS-370", "HIST-305", "HIST-341", "HIST-342",
                "HIST-350", "HIST-380", "HIST-389", "HSCI-345", "HSCI-369", "JOUR-365",
                "JS-318", "LING-309", "MKT-350", "PHIL-305", "PHIL-391", "POLS-310",
                "POLS-350", "POLS-355", "POLS-380", "POLS-403", "PSY-312", "PSY-352",
                "PSY-365", "SOC-305", "SOC-324", "SUST-300", "URBS-310", "URBS-380",
            ],
            "F Upper Division Comparative Cultural Studies": [
                "AAS-340", "AAS-345", "AAS-360", "AFRS-300", "AFRS-320", "AFRS-322",
                "AFRS-324", "AFRS-325", "AFRS-366", "AIS-304", "AIS-318", "AIS-333",
                "ANTH-308", "ANTH-310", "ANTH-315", "ANTH-345", "ARMN-310", "ARMN-360",
                "ART-315", "BLAW-391", "CAS-311", "CAS-365", "CHS-333", "CHS-364",
                "CHS-365", "COMS-356", "COMS-360", "ENGL-311", "ENGL-318", "ENGL-371",
                "FLIT-370", "FLIT-371", "FLIT-380", "GEOG-318", "GEOG-322", "GEOG-324",
                "GEOG-326", "GEOG-334", "GWS-300", "GWS-351", "HIST-349A", "HIST-349B",
                "HIST-369", "JS-306", "JS-330", "JS-335", "JS-378", "KIN-385", "LING-325",
                "MSE-302", "MUS-309", "MUS-310", "PHIL-333", "PHIL-343", "PHIL-344",
                "PHIL-348", "POLS-321", "POLS-332", "QS-301", "QS-303", "QS-304",
                "RS-306", "RS-365", "RS-378", "RS-380", "RS-385", "RS-390", "RTM-330",
                "SOC-306", "SOC-307", "SOC-335", "TH-325", "URBS-350",
            ],
            "F Upper Division Comparative Cultural Studies 2": [
                "AAS-340", "AAS-345", "AAS-360", "AFRS-300", "AFRS-320", "AFRS-322",
                "AFRS-324", "AFRS-325", "AFRS-366", "AIS-304", "AIS-318", "AIS-333",
                "ANTH-308", "ANTH-310", "ANTH-315", "ANTH-345", "ARMN-310", "ARMN-360",
                "ART-315", "BLAW-391", "CAS-311", "CAS-365", "CHS-333", "CHS-364",
                "CHS-365", "COMS-356", "COMS-360", "ENGL-311", "ENGL-318", "ENGL-371",
                "FLIT-370", "FLIT-371", "FLIT-380", "GEOG-318", "GEOG-322", "GEOG-324",
                "GEOG-326", "GEOG-334", "GWS-300", "GWS-351", "HIST-349A", "HIST-349B",
                "HIST-369", "JS-306", "JS-330", "JS-335", "JS-378", "KIN-385", "LING-325",
                "MSE-302", "MUS-309", "MUS-310", "PHIL-333", "PHIL-343", "PHIL-344",
                "PHIL-348", "POLS-321", "POLS-332", "QS-301", "QS-303", "QS-304",
                "RS-306", "RS-365", "RS-378", "RS-380", "RS-385", "RS-390", "RTM-330",
                "SOC-306", "SOC-307", "SOC-335", "TH-325", "URBS-350",
            ],
        }

        for block_name, codes in block_to_codes.items():
            attach(block_name, codes)

        self.stdout.write(self.style.SUCCESS("Done. Block/course attachment finished."))