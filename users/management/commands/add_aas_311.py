import json
import re
from pathlib import Path

from django.core.management.base import BaseCommand
from users.models import Course, ProgramRequirement, RequirementBlock, Tag


class Command(BaseCommand):
    help = "Import missing courses from add.json and COMP.json, normalize codes, attach blocks/tags, and report anything skipped."

    SUBJECT_NAME_MAP = {
        "AAS": "Asian American Studies",
        "AFRS": "Africana Studies",
        "AIS": "American Indian Studies",
        "ART": "Art",
        "ASTR": "Astronomy",
        "BIOL": "Biology",
        "BLAW": "Business Law",
        "CADV": "Career Development",
        "CAS": "Central American Studies",
        "CD": "Child Development",
        "CHS": "Chicana/o Studies",
        "CM": "Cinema and Media",
        "CLAS": "Classics",
        "COMS": "Communication Studies",
        "COMP": "Computer Science",
        "CTVA": "Cinema and Television Arts",
        "DH": "Digital Humanities",
        "ECON": "Economics",
        "ENGL": "English",
        "FCS": "Family and Consumer Sciences",
        "FIN": "Finance",
        "FLIT": "Foreign Literature",
        "GEH": "General Education Honors",
        "GEOG": "Geography",
        "GEOL": "Geology",
        "GWS": "Gender and Women's Studies",
        "HIST": "History",
        "HSCI": "Health Sciences",
        "IS": "Information Systems",
        "JS": "Jewish Studies",
        "JOUR": "Journalism",
        "KIN": "Kinesiology",
        "LING": "Linguistics",
        "MATH": "Mathematics",
        "MKT": "Marketing",
        "MSE": "Mathematics Science Engineering",
        "MUS": "Music",
        "PHIL": "Philosophy",
        "PHYS": "Physics",
        "POLS": "Political Science",
        "PSY": "Psychology",
        "QS": "Queer Studies",
        "RS": "Religious Studies",
        "RTM": "Recreation and Tourism Management",
        "SCI": "Science",
        "SOC": "Sociology",
        "SUST": "Sustainability",
        "TH": "Theatre",
        "UNIV": "University",
        "URBS": "Urban Studies and Planning",
    }

    BLOCK_ALIAS_MAP = {
        "E.S. Ethnic Studies": "Ethnic Studies",
        "LL Lifelong Learning": "E Lifelong Learning",
        "GE Area 1B Critical Thinking": "A3 Critical Thinking",
        "GE Section F Comparative Cultural Studies": "F Comparative Cultural Studies",
    }

    def add_arguments(self, parser):
        parser.add_argument("--program", default="BS_CS")
        parser.add_argument("--catalog", type=int, default=2023)
        parser.add_argument("--json", nargs="*", default=[
            "/mnt/data/add.json",
            "/mnt/data/COMP.json",
        ])
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        self.program = options["program"]
        self.catalog = options["catalog"]
        self.dry_run = options["dry_run"]

        self.req = ProgramRequirement.objects.filter(
            program=self.program,
            catalog_year=self.catalog,
        ).first()

        if not self.req:
            self.stdout.write(self.style.ERROR(
                f"ProgramRequirement not found for program={self.program}, catalog_year={self.catalog}"
            ))
            return

        self.missing_references = []
        self.skipped_items = []
        self.created_courses = []
        self.updated_courses = []
        self.attached_blocks = []
        self.attached_tags = []
        self.created_tags = []

        all_items = []
        for raw_path in options["json"]:
            path = Path(raw_path)
            if not path.exists():
                self.stdout.write(self.style.WARNING(f"JSON file not found: {path}"))
                continue
            items = self.load_courses(path)
            all_items.extend(items)

        if not all_items:
            self.stdout.write(self.style.WARNING("No courses found to import."))
            return

        for item in all_items:
            self.process_item(item)

        self.print_summary()

    def load_courses(self, path: Path):
        text = path.read_text(encoding="utf-8").strip()
        decoder = json.JSONDecoder()

        items = []
        idx = 0
        n = len(text)

        while idx < n:
            while idx < n and text[idx].isspace():
                idx += 1
            if idx >= n:
                break

            obj, next_idx = decoder.raw_decode(text, idx)
            idx = next_idx

            if isinstance(obj, dict) and "courses" in obj:
                items.extend(obj["courses"])
            elif isinstance(obj, dict) and "code" in obj:
                items.append(obj)
            elif isinstance(obj, list):
                items.extend(obj)

        return items

    def process_item(self, item: dict):
        raw_code = (item.get("code") or "").strip()
        if not raw_code:
            self.skipped_items.append("Skipped item with no code")
            return

        split_variants = self.expand_course_variants(item)
        for variant in split_variants:
            self.create_or_update_full_course(variant)

    def expand_course_variants(self, item: dict):
        """
        Examples:
        COMP 490/L -> create COMP-490 and COMP-490L
        COMP 322/L -> create COMP-322 and COMP-322L
        KIN 380/L  -> create KIN-380 and KIN-380L
        RTM 310/L  -> create RTM-310 and RTM-310L

        But:
        AAS 390/F -> create only AAS-390
        CAS 270/F -> create only CAS-270
        CHS 270SOC/C/F -> create only CHS-270SOC
        """
        raw_code = (item.get("code") or "").strip().upper()
        raw_title = (item.get("title") or "").strip()

        m = re.match(r"^([A-Z]+)\s+(.+)$", raw_code)
        if not m:
            normalized = self.normalize_code(raw_code)
            new_item = dict(item)
            new_item["code"] = normalized
            return [new_item]

        subject = m.group(1)
        rest = m.group(2).strip()

        # Split lecture/lab pattern like 490/L, 322/L, 380/L, 310/L, 465/L etc.
        if re.match(r"^\d+[A-Z]?/L$", rest):
            base_num = rest.split("/")[0].strip()
            lecture_code = f"{subject}-{base_num}"
            lab_code = f"{subject}-{base_num}L"

            credits = self.parse_credit_parts(item.get("credits"))
            lecture_credits = credits[0] if len(credits) >= 1 else 3.0
            lab_credits = credits[1] if len(credits) >= 2 else 1.0

            lecture = dict(item)
            lecture["code"] = lecture_code
            lecture["credits"] = lecture_credits
            lecture["corequisites"] = self.ensure_contains_code(
                item.get("corequisites", []),
                lab_code
            )
            lecture["__pair_role__"] = "lecture"
            lecture["__pair_code__"] = lab_code

            lab = dict(item)
            lab["code"] = lab_code
            lab["title"] = self.make_lab_title(raw_title)
            lab["credits"] = lab_credits
            lab["corequisites"] = self.ensure_contains_code(
                item.get("corequisites", []),
                lecture_code
            )
            lab["__pair_role__"] = "lab"
            lab["__pair_code__"] = lecture_code

            return [lecture, lab]

        # Other slash patterns remain single normalized course
        normalized = self.normalize_code(raw_code)
        new_item = dict(item)
        new_item["code"] = normalized
        return [new_item]

    def make_lab_title(self, title: str) -> str:
        t = (title or "").strip()
        if not t:
            return "Laboratory"
        if " and Lab" in t:
            return t.replace(" and Lab", " Lab")
        if "Lab" not in t:
            return f"{t} Lab"
        return t

    def ensure_contains_code(self, values, code):
        clean = list(values or [])
        norm_targets = {self.normalize_code(v) for v in clean if str(v).strip()}
        if self.normalize_code(code) not in norm_targets:
            clean.append(code)
        return clean

    def normalize_code(self, raw_code: str) -> str:
        raw = (raw_code or "").strip().upper()
        raw = re.sub(r"\s+", " ", raw)

        m = re.match(r"^([A-Z]+)\s+(.+)$", raw)
        if not m:
            return raw.replace(" ", "-")

        subject = m.group(1)
        rest = m.group(2).strip()

        if "/" in rest:
            rest = rest.split("/")[0].strip()

        rest = rest.replace(" ", "")
        return f"{subject}-{rest}"

    def parse_credit_parts(self, value):
        if isinstance(value, list):
            out = []
            for x in value:
                try:
                    out.append(float(x))
                except Exception:
                    pass
            return out

        if isinstance(value, (int, float)):
            return [float(value)]

        raw = str(value).strip()
        if not raw:
            return [0.0]

        # Range like 1-4 or 1-3: keep first number only
        if "-" in raw and "/" not in raw:
            first = raw.split("-")[0].strip()
            try:
                return [float(first)]
            except Exception:
                return [0.0]

        if "/" in raw:
            out = []
            for part in raw.split("/"):
                part = part.strip()
                try:
                    out.append(float(part))
                except Exception:
                    pass
            return out or [0.0]

        try:
            return [float(raw)]
        except Exception:
            return [0.0]

    def infer_subject_name(self, code: str) -> str:
        subject_code = code.split("-")[0]
        return self.SUBJECT_NAME_MAP.get(subject_code, subject_code)

    def infer_level(self, code: str) -> str:
        m = re.search(r"-(\d+)", code)
        if not m:
            return "upper"
        num = int(m.group(1))
        if num < 100:
            return "lower"
        if num < 300:
            return "lower"
        return "upper"

    def build_description(self, item: dict) -> str:
        desc = (item.get("description") or "").strip()
        extra_notes = []

        prereqs = item.get("prerequisites") or []
        preparatory = item.get("preparatory") or []

        if any("completion of the lower division writing requirement" in str(x).lower() for x in prereqs + preparatory):
            extra_notes.append(
                "Lower division writing requirement note: one approved lower division writing pathway must be completed."
            )

        non_course_preps = [
            str(x).strip()
            for x in preparatory
            if str(x).strip()
            and not self.extract_course_like_codes(str(x))
            and "completion of the lower division writing requirement" not in str(x).lower()
        ]
        if non_course_preps:
            extra_notes.append("Preparatory note: " + "; ".join(non_course_preps))

        if extra_notes:
            if desc:
                desc += "\n\n"
            desc += "\n".join(extra_notes)

        return desc

    def create_or_update_full_course(self, item: dict):
        code = self.normalize_code(item["code"])
        title = (item.get("title") or "").strip()
        credits = self.parse_credit_parts(item.get("credits"))[0]
        subject_name = self.infer_subject_name(code)
        level = self.infer_level(code)
        description = self.build_description(item)

        defaults = {
            "subject": subject_name,
            "title": title,
            "credits": credits,
            "level": level,
            "description": description,
        }

        if self.dry_run:
            course = Course(
                code=code,
                program=self.program,
                catalog_year=self.catalog,
                **defaults,
            )
            created = True
        else:
            course, created = Course.objects.get_or_create(
                code=code,
                program=self.program,
                catalog_year=self.catalog,
                defaults=defaults,
            )

            if not created:
                course.subject = subject_name
                course.title = title
                course.credits = credits
                course.level = level
                course.description = description
                course.save()

        if created:
            self.created_courses.append(code)
        else:
            self.updated_courses.append(code)

        self.attach_tags(course, item.get("tags", []))
        self.attach_prereqs_and_coreqs(course, item)
        self.attach_blocks(course, item)

    def attach_tags(self, course, tag_names):
        for raw_name in tag_names or []:
            name = (raw_name or "").strip()
            if not name:
                continue
            if self.dry_run:
                self.attached_tags.append((course.code, name))
                continue
            tag, created = Tag.objects.get_or_create(name=name)
            if created:
                self.created_tags.append(name)
            course.tags.add(tag)
            self.attached_tags.append((course.code, name))

    def extract_course_like_codes(self, text: str):
        raw = (text or "").upper()

        patterns = re.findall(r"[A-Z]{2,6}\s+\d+[A-Z/]*", raw)
        out = []
        for p in patterns:
            norm = self.normalize_code(p)
            if norm not in out:
                out.append(norm)
        return out

    def maybe_find_course(self, raw_code: str):
        code = self.normalize_code(raw_code)
        return Course.objects.filter(
            code=code,
            program=self.program,
            catalog_year=self.catalog,
        ).first()

    def attach_prereqs_and_coreqs(self, course, item: dict):
        prereq_texts = list(item.get("prerequisites", []) or [])
        prep_texts = list(item.get("preparatory", []) or [])
        coreq_texts = list(item.get("corequisites", []) or [])

        prereq_codes = []
        coreq_codes = []

        for text in prereq_texts + prep_texts:
            if "completion of the lower division writing requirement" in str(text).lower():
                continue
            prereq_codes.extend(self.extract_course_like_codes(str(text)))

        for text in coreq_texts:
            coreq_codes.extend(self.extract_course_like_codes(str(text)))

        seen_pr = set()
        for raw_code in prereq_codes:
            norm = self.normalize_code(raw_code)
            if norm in seen_pr:
                continue
            seen_pr.add(norm)

            obj = self.maybe_find_course(norm)
            if obj:
                if not self.dry_run:
                    course.prerequisites.add(obj)
            else:
                self.missing_references.append(
                    f"{course.code}: prerequisite not found -> {norm}"
                )

        seen_co = set()
        for raw_code in coreq_codes:
            norm = self.normalize_code(raw_code)
            if norm in seen_co:
                continue
            seen_co.add(norm)

            obj = self.maybe_find_course(norm)
            if obj:
                if not self.dry_run:
                    course.corequisites.add(obj)
            else:
                self.missing_references.append(
                    f"{course.code}: corequisite not found -> {norm}"
                )

    def resolve_blocks(self, course_code: str, raw_blocks: list[str]):
        resolved = []

        for raw in raw_blocks or []:
            raw = (raw or "").strip()
            if not raw:
                continue

            if raw in self.BLOCK_ALIAS_MAP:
                resolved.append(self.BLOCK_ALIAS_MAP[raw])
                continue

            if raw == "GE I.C. Information Competence":
                if course_code == "ENGL-215":
                    resolved.append("Basic Skills Information Competence")
                else:
                    resolved.append("Subject Explorations Information Competence")
                continue

            if raw == "GE Area 5 Physical and Biological Sciences":
                if course_code == "BIOL-362":
                    resolved.append("B5 Upper Division Scientific Inquiry")
                else:
                    resolved.append("B2 Life Science")
                continue

            if raw == "GE UD Upper Division":
                if course_code.startswith("KIN-380"):
                    resolved.append("C Upper Division Arts and Humanities")
                elif course_code.startswith("RTM-310"):
                    resolved.append("F Upper Division Comparative Cultural Studies")
                    resolved.append("F Upper Division Comparative Cultural Studies 2")
                elif course_code == "BIOL-362":
                    resolved.append("B5 Upper Division Scientific Inquiry")
                continue

            if raw == "GE Area 3A Arts":
                if course_code.startswith("KIN-380"):
                    resolved.append("C Upper Division Arts and Humanities")
                else:
                    resolved.append("C1 Arts")
                continue

            resolved.append(raw)

        # dedupe keep order
        out = []
        seen = set()
        for x in resolved:
            if x not in seen:
                out.append(x)
                seen.add(x)
        return out

    def attach_blocks(self, course, item: dict):
        block_names = self.resolve_blocks(course.code, item.get("blocks", []) or [])
        for block_name in block_names:
            block = RequirementBlock.objects.filter(
                requirement=self.req,
                name=block_name,
            ).first()

            if not block:
                self.missing_references.append(
                    f"{course.code}: requirement block not found -> {block_name}"
                )
                continue

            if not self.dry_run:
                block.courses.add(course)
            self.attached_blocks.append((course.code, block_name))

    def print_summary(self):
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Import summary"))
        self.stdout.write(f"Created courses: {len(self.created_courses)}")
        self.stdout.write(f"Updated courses: {len(self.updated_courses)}")
        self.stdout.write(f"Tag attachments: {len(self.attached_tags)}")
        self.stdout.write(f"Block attachments: {len(self.attached_blocks)}")

        if self.created_tags:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING("Created tags"))
            for tag in sorted(set(self.created_tags)):
                self.stdout.write(f"  - {tag}")

        if self.skipped_items:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING("Skipped items"))
            for msg in self.skipped_items:
                self.stdout.write(f"  - {msg}")

        if self.missing_references:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING("Could not add these references"))
            for msg in self.missing_references:
                self.stdout.write(f"  - {msg}")

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            "Finished. Anything listed under 'Could not add these references' needs manual review."
        ))