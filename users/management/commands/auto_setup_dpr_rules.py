from django.core.management.base import BaseCommand
from django.db import transaction

from users.models import ProgramRequirement, RequirementBlock, Course


class Command(BaseCommand):
    help = "Auto-setup DPR requirement blocks for BS_CS catalog 2022 based on the uploaded DPR"

    PROGRAM = "BS_CS"
    CATALOG_YEAR = 2023

    def handle(self, *args, **options):
        with transaction.atomic():
            req, _ = ProgramRequirement.objects.get_or_create(
                program=self.PROGRAM,
                catalog_year=self.CATALOG_YEAR,
            )

            # IMPORTANT:
            # Clear old blocks first so names like "A1" and
            # "A1 Oral Communication" do not both remain.
            RequirementBlock.objects.filter(requirement=req).delete()

            def set_block(name, min_required, course_codes, allow_double_count=False):
                block = RequirementBlock.objects.create(
                    requirement=req,
                    name=name,
                    min_required=min_required,
                    allow_double_count=allow_double_count,
                )

                courses = []
                missing = []

                for code in course_codes:
                    course = Course.objects.filter(
                        code__iexact=code,
                        program=self.PROGRAM,
                        catalog_year=self.CATALOG_YEAR,
                    ).first()

                    if not course:
                        course = Course.objects.filter(code__iexact=code).first()

                    if course:
                        courses.append(course)
                    else:
                        missing.append(code)

                block.courses.set(courses)

                self.stdout.write(
                    self.style.SUCCESS(
                        f"Block '{name}' saved with {len(courses)} courses"
                    )
                )

                if missing:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Missing in Course table for '{name}': {', '.join(missing)}"
                        )
                    )

                return block

            # =====================================================
            # GENERAL EDUCATION / GRADUATION
            # =====================================================

            set_block(
                "A1 Oral Communication",
                1,
                [
                    "COMS-151",
                    "COMS-356",
                    "AAS-151",
                    "AFRS-151",
                    "CAS-151",
                    "CHS-151",
                    "QS-151",
                ],
            )

            set_block(
                "A2 Written Communication",
                1,
                [
                    "LING-113B",
                    "ENGL-113B",
                    "ENGL-114B",
                    "ENGL-115",
                    "AAS-113B",
                    "AAS-114B",
                    "AAS-115",
                    "AFRS-113B",
                    "AFRS-114B",
                    "AFRS-115",
                    "CAS-113B",
                    "CAS-114B",
                    "CAS-115",
                    "CHS-113B",
                    "CHS-114B",
                    "CHS-115",
                    "QS-113B",
                    "QS-114B",
                    "QS-115",
                ],
            )

            set_block(
                "A3 Critical Thinking",
                1,
                [
                    "COMP-310",
                ],
                allow_double_count=True,
            )

            set_block(
                "B1 Physical Science",
                1,
                [
                    "GEOL-101",
                    "GEOL-107",
                    "GEOL-110",
                    "GEOL-113",
                    "GEOL-117",
                    "GEOL-122",
                    "GEOL-125",
                    "ASTR-152",
                    "ASTR-154",
                    "CHEM-100",
                    "CHEM-101",
                    "CHEM-102",
                    "CHEM-103",
                    "CHEM-104",
                    "CHEM-110",
                    "GEOG-101",
                    "GEOG-101A",
                    "GEOG-103",
                    "GEOG-103A",
                    "GEOG-112",
                    "PHYS-100A",
                    "PHYS-100B",
                    "PHYS-220A",
                    "PHYS-220B",
                    "SCI-111",
                    "SUST-111",
                ],
            )

            set_block(
                "B2 Life Science",
                1,
                [
                    "ANTH-151",
                    "BIOL-100",
                    "BIOL-101",
                    "BIOL-106",
                    "BIOL-218",
                    "BIOL-292",
                    "GEOL-110",
                    "GEOL-113",
                    "GEOL-125",
                ],
            )

            set_block(
                "B3 Lab Activity",
                1,
                [
                    "GEOL-102",
                    "GEOL-112",
                    "BIOL-106L",
                    "BIOL-107L",
                ],
            )

            set_block(
                "B4 Math / Quantitative Reasoning",
                1,
                [
                    "MATH-105",
                    "MATH-150A",
                    "MATH-150B",
                    "MATH-340",
                ],
            )

            set_block(
                "B5 Upper Division Scientific Inquiry",
                1,
                [
                    "COMP-310",
                ],
                allow_double_count=True,
            )

            set_block(
                "C1 Arts",
                1,
                [
                    "TH-110",
                    "TH-111",
                    "TH-310",
                    "MUS-105",
                    "MUS-106HH",
                    "MUS-107",
                    "MUS-108",
                    "MUS-306",
                    "MUS-309",
                    "ART-100",
                    "ART-100L",
                    "ART-110",
                    "ART-114",
                    "ART-124A",
                    "ART-140",
                    "ART-141",
                    "ART-305",
                    "COMS-104",
                    "COMS-305",
                    "CTVA-210",
                    "CTVA-215",
                    "CTVA-309",
                    "CTVA-323",
                    "ENGL-208",
                    "FCS-111",
                    "FLIT-151",
                    "FLIT-250",
                    "HUM-101",
                    "HUM-105",
                    "HUM-106",
                    "JS-300",
                    "KIN-139A",
                    "KIN-144A",
                    "KIN-236",
                    "KIN-236L",
                    "KIN-380",
                    "KIN-380L",
                    "LING-240",
                    "PHIL-314",
                ],
            )

            set_block(
                "C2 Humanities",
                1,
                [
                    "HUM-101",
                    "HUM-105",
                    "HUM-106",
                    "HIST-271",
                    "SPAN-101",
                    "AAS-220",
                    "AAS-231",
                    "AFRS-245",
                    "AFRS-343",
                    "AFRS-344",
                    "AFRS-346",
                    "AFRS-352",
                ],
            )

            set_block(
                "D1 Social Sciences",
                2,
                [
                    "AAS-210",
                    "AAS-230",
                    "AAS-231",
                    "HIST-271",
                    "HSCI-132",
                    "HSCI-345",
                    "HSCI-369",
                    "JOUR-365",
                    "LING-230",
                    "LING-309",
                    "MKT-350",
                    "PHIL-305",
                    "PHIL-391",
                    "POLS-156",
                    "POLS-225",
                    "POLS-310",
                    "POLS-350",
                    "POLS-380",
                    "PSY-150",
                    "PSY-312",
                    "PSY-352",
                    "PSY-365",
                    "RS-240",
                    "SOC-150",
                    "SOC-200",
                    "SOC-305",
                    "SOC-324",
                    "SUST-300",
                    "URBS-150",
                    "URBS-310",
                    "URBS-380",
                ],
            )

            set_block(
                "D3 Constitution of the U.S.",
                1,
                [
                    "POLS-155",
                    "POLS-355",
                    "RS-255",
                    "AAS-347",
                    "AFRS-161",
                    "CHS-260",
                    "CHS-445",
                ],
                allow_double_count=True,
            )

            set_block(
                "D4 California State and Local Government",
                1,
                [
                    "POLS-155",
                    "POLS-355",
                    "POLS-403",
                    "POLS-490CA",
                    "RS-255",
                    "AAS-347",
                    "AFRS-161",
                    "CHS-260",
                    "CHS-445",
                ],
                allow_double_count=True,
            )

            set_block(
                "E Lifelong Learning",
                1,
                [
                    "AIS-301",
                    "AIS-318",
                    "ANTH-222",
                    "ANTH-326",
                    "CAS-201",
                    "CHS-201",
                    "CHS-350",
                    "CHS-351",
                    "CHS-380",
                    "CHS-381",
                    "CHS-382",
                    "CLAS-315",
                    "CTVA-215",
                    "DH-320",
                    "ENGL-254",
                    "ENGL-255",
                    "ENGL-258",
                    "ENGL-259",
                    "ENGL-275",
                    "ENGL-300",
                    "ENGL-316",
                    "ENGL-318",
                    "ENGL-322",
                    "ENGL-333",
                    "ENGL-364",
                    "FLIT-151",
                    "FLIT-295A",
                    "FLIT-331",
                    "FLIT-381",
                    "GWS-100",
                    "GWS-230",
                    "GWS-351",
                    "HIST-150",
                    "HIST-151",
                    "HIST-303",
                    "HIST-304",
                    "HUM-101",
                    "HUM-105",
                    "HUM-106",
                    "JS-100",
                    "JS-255",
                    "JS-300",
                    "JS-333",
                    "LING-200",
                    "PHIL-150",
                    "PHIL-165",
                    "PHIL-170",
                    "PHIL-180",
                    "PHIL-201",
                    "PHIL-202",
                    "PHIL-240",
                    "PHIL-250",
                    "PHIL-260",
                    "PHIL-265",
                ],
            )

            set_block(
                "F Comparative Cultural Studies",
                2,
                [
                    "AAS-230",
                    "AAS-390",
                    "AAS-390F",
                    "AFRS-337",
                    "AIS-301",
                    "ART-151",
                    "ART-201",
                    "BIOL-375",
                    "BLAW-280",
                    "BLAW-368",
                    "BUS-104",
                    "CADV-310",
                    "CAS-270",
                    "CAS-270F",
                    "CCE-200",
                    "CD-133",
                    "CD-361",
                    "CHS-270SOC",
                    "CHS-270F",
                    "CHS-360",
                    "CHS-390",
                    "CJS-340",
                    "CM-336",
                    "CM-336L",
                    "COMP-100",
                    "COMP-102",
                    "COMP-102L",
                    "COMP-110",
                    "COMP-110L",
                    "COMP-111B",
                    "COMP-111BL",
                    "COMP-300",
                    "COMS-150",
                    "COMS-251",
                    "COMS-323",
                    "COMS-360",
                    "CTVA-100",
                    "CTVA-323",
                    "ENGL-253",
                    "ENGL-306",
                    "ENGL-313",
                    "ENGL-315",
                    "ENT-101",
                    "EOH-101",
                    "EOH-353",
                    "FCS-120",
                    "FCS-171",
                    "FCS-207",
                    "FCS-260",
                    "FCS-315",
                    "FCS-323",
                    "FCS-324",
                    "FCS-330",
                    "FCS-340",
                    "FIN-102",
                    "FIN-302",
                    "FLIT-234",
                    "GEOG-206",
                    "GEOG-206L",
                    "GEOL-104",
                    "GWS-305",
                    "GWS-305CS",
                    "HIST-366",
                    "HSCI-131",
                    "HSCI-170",
                    "HSCI-231",
                    "HSCI-336",
                    "HSCI-337",
                    "IS-212",
                    "JOUR-100",
                    "JOUR-390",
                    "JS-390CS",
                    "KIN-115A",
                    "KIN-117",
                    "KIN-118",
                    "SPAN-101",
                    "AAS-210",
                    "AAS-220",
                    "AAS-231",
                ],
            )

            set_block(
                "Ethnic Studies",
                1,
                [
                    "AAS-340",
                    "AAS-345",
                    "AAS-360",
                    "AFRS-300",
                    "AFRS-320",
                    "AFRS-322",
                    "AFRS-324",
                    "AFRS-325",
                    "AFRS-366",
                    "AIS-304",
                    "AIS-318",
                    "AIS-333",
                    "ANTH-308",
                    "ANTH-310",
                    "ANTH-315",
                    "ANTH-345",
                    "ARMN-310",
                    "ARMN-360",
                    "ART-315",
                    "BLAW-391",
                    "CAS-311",
                    "CAS-365",
                    "CHS-333",
                    "CHS-364",
                    "CHS-365",
                    "COMS-356",
                    "COMS-360",
                    "ENGL-311",
                    "ENGL-318",
                    "ENGL-371",
                    "FLIT-370",
                    "FLIT-371",
                    "FLIT-380",
                    "GEOG-318",
                    "GEOG-322",
                    "GEOG-324",
                    "GEOG-326",
                    "GEOG-334",
                    "GWS-300",
                    "GWS-351",
                    "HIST-349A",
                    "HIST-349B",
                    "HIST-369",
                    "JS-306",
                    "JS-330",
                    "JS-335",
                    "JS-378",
                    "JOUR-371",
                    "JOUR-372",
                    "KIN-385",
                ],
                allow_double_count=True,
            )

            set_block(
                "Information Competence",
                1,
                [
                    "COMP-110",
                    "COMP-182",
                    "COMP-310",
                    "COMS-151",
                    "LING-113B",
                    "HUM-101",
                    "POLS-155",
                ],
                allow_double_count=True,
            )

            # =====================================================
            # MAJOR
            # =====================================================

            set_block(
                "Pre-Major",
                6,
                [
                    "COMP-110",
                    "COMP-122",
                    "COMP-122L",
                    "COMP-182",
                    "COMP-182L",
                    "MATH-150A",
                ],
            )

            set_block(
                "Lower Division Core",
                8,
                [
                    "COMP-222",
                    "COMP-256",
                    "COMP-256L",
                    "COMP-310",
                    "MATH-150B",
                    "MATH-340",
                    "MATH-105",
                    "MATH-105L",
                ],
            )

            set_block(
                "Lower Division Elective A",
                1,
                [
                    "GEOL-101",
                    "GEOL-110",
                    "BIOL-106",
                    "BIOL-107",
                ],
            )

            set_block(
                "Lower Division Elective B",
                1,
                [
                    "GEOL-102",
                    "GEOL-112",
                    "BIOL-106L",
                    "BIOL-107L",
                ],
                allow_double_count=True,
            )

            set_block(
                "Probability / Statistics",
                1,
                [
                    "MATH-340",
                ],
                allow_double_count=True,
            )

            set_block(
                "Upper Division Core",
                5,
                [
                    "COMP-322",
                    "COMP-322L",
                    "COMP-324",
                    "COMP-333",
                    "COMP-380",
                    "COMP-482",
                    "COMP-490",
                ],
            )

            set_block(
                "Senior Electives",
                5,
                [
                    "COMP-410",
                    "COMP-424",
                    "COMP-429",
                    "COMP-440",
                    "COMP-467",
                    "COMP-491",
                    "COMP-491L",
                    "COMP-529L",
                    "COMP-541",
                ],
            )

            self.stdout.write(self.style.SUCCESS("DPR rules auto-setup complete."))