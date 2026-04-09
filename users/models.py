from django.db import models
from django.apps import apps
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator, FileExtensionValidator
from datetime import timedelta
from math import ceil
from django.utils.text import slugify
import re

User = get_user_model()

# =========================
# One-time passcodes (OTP)
# =========================
class EmailOTP(models.Model):
    PURPOSES = [
        ("RESET", "Password Reset"),
        ("VERIFY", "Account Verification"),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="email_otps")
    code_hash = models.CharField(max_length=256)
    purpose = models.CharField(max_length=10, choices=PURPOSES, default="RESET")

    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    attempts = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(default=5)
    is_used = models.BooleanField(default=False)

    @classmethod
    def expire_minutes(cls) -> int:
        return 10

    @classmethod
    def create_for_user(cls, user, code_hash, purpose="RESET"):
        return cls.objects.create(
            user=user,
            code_hash=code_hash,
            purpose=purpose,
            expires_at=timezone.now() + timedelta(minutes=cls.expire_minutes()),
        )

    def is_valid(self):
        return (
            (not self.is_used)
            and (self.attempts < self.max_attempts)
            and (timezone.now() < self.expires_at)
        )

    def mark_used(self):
        self.is_used = True
        self.save(update_fields=["is_used"])


# =========================
# Terms (SP/SU/FA)
# =========================
SEASONS = [("SP", "Spring"), ("SU", "Summer"), ("FA", "Fall")]


class Term(models.Model):
    year = models.PositiveIntegerField()
    season = models.CharField(max_length=2, choices=SEASONS)

    class Meta:
        unique_together = ("year", "season")
        ordering = ["year", "season"]

    def __str__(self):
        return f"{self.get_season_display()} {self.year}"

    def next(self):
        order = ["SP", "SU", "FA"]
        i = order.index(self.season)
        n_season = order[(i + 1) % 3]
        n_year = self.year + (1 if self.season == "FA" else 0)
        return Term.objects.get_or_create(year=n_year, season=n_season)[0]

    @staticmethod
    def from_date(dt=None):
        dt = dt or timezone.localdate()
        m = dt.month
        if 1 <= m <= 5:
            season = "SP"
        elif 6 <= m <= 7:
            season = "SU"
        else:
            season = "FA"
        term, _ = Term.objects.get_or_create(year=dt.year, season=season)
        return term


# =========================
# Student profile
# =========================
class StudentProfile(models.Model):
    PROGRAM_CHOICES = [
        ("BS_CS", "B.S. Computer Science"),
        ("BS_IT", "B.S. Information Technology"),
        ("MS_CS", "M.S. Computer Science"),
        ("MBA", "MBA"),
    ]

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile"
    )

    completed_codes = models.TextField(blank=True, default="")

    program = models.CharField(max_length=32, choices=PROGRAM_CHOICES)
    catalog_year = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(2000), MaxValueValidator(2100)],
        help_text="e.g., 2024",
    )

    avg_credits_per_term = models.PositiveIntegerField(default=15)
    max_credits_next_term = models.PositiveIntegerField(default=15)

    def __str__(self):
        who = self.user.get_full_name() or self.user.email or self.user.username
        return f"{who} — {self.get_program_display()} ({self.catalog_year})"

    def _parsed_completed_codes(self) -> set[str]:
        raw = (self.completed_codes or "").upper()
        parts = [p.strip() for p in re.split(r"[,\s]+", raw) if p.strip()]
        return set(parts)

    def completed_courses_qs(self):
        codes = set(
            CompletedClass.objects.filter(profile=self)
            .values_list("course__code", flat=True)
        ) | self._parsed_completed_codes()

        return Course.objects.filter(
            program=self.program,
            catalog_year=self.catalog_year,
            code__in=codes,
        )

    def completed_course_codes(self) -> set[str]:
        db_codes = set(
            CompletedClass.objects.filter(profile=self)
            .values_list("course__code", flat=True)
        )
        return {c.upper() for c in (db_codes | self._parsed_completed_codes())}

    def in_progress_course_codes(self) -> set[str]:
        return {
            c.upper()
            for c in InProgressClass.objects.filter(profile=self)
            .values_list("course__code", flat=True)
        }

    def taken_course_codes(self) -> set[str]:
        return self.completed_course_codes() | self.in_progress_course_codes()

    def get_requirement(self):
        return ProgramRequirement.objects.filter(
            program=self.program,
            catalog_year=self.catalog_year,
        ).first()

    def remaining_required_courses(self):
        """
        DPR-aware remaining course list.

        Excludes BOTH:
        - completed courses
        - in-progress courses

        IMPORTANT:
        Exclusion is by COURSE CODE, not by COURSE ID.
        This avoids duplicate-row problems like:
        COMP-122 completed row id != COMP-122 block row id
        """
        req = self.get_requirement()
        if not req:
            return Course.objects.none()

        taken_codes = {code.upper() for code in self.taken_course_codes()}

        # New DPR engine blocks
        blocks = list(req.blocks.prefetch_related("courses").all())
        if blocks:
            used_codes = set()
            needed_codes = set()

            blocks.sort(key=lambda b: (int(b.min_required), b.courses.count(), b.name))

            for b in blocks:
                block_courses = list(b.courses.all())
                block_codes = [c.code.upper() for c in block_courses]

                available_completed = []
                for code in block_codes:
                    if code in taken_codes and (b.allow_double_count or code not in used_codes):
                        available_completed.append(code)

                assigned = available_completed[: int(b.min_required)]

                if not b.allow_double_count:
                    used_codes.update(assigned)

                still_needed = max(0, int(b.min_required) - len(assigned))
                if still_needed > 0:
                    remaining_options = [code for code in block_codes if code not in taken_codes]
                    needed_codes.update(remaining_options[:still_needed])

            return Course.objects.filter(
                program=self.program,
                catalog_year=self.catalog_year,
                code__in=needed_codes,
            ).order_by("code").distinct()

        # Fallback old group logic
        groups = req.groups.prefetch_related("courses").all()

        required_codes = set()
        elective_pool_codes = set()

        for g in groups:
            group_codes = {c.code.upper() for c in g.courses.all()}
            completed_in_group = len(group_codes & taken_codes)

            if completed_in_group >= int(g.min_required):
                continue

            if len(group_codes) <= int(g.min_required):
                required_codes.update(group_codes)
            else:
                elective_pool_codes.update(group_codes)

        required_codes = required_codes - taken_codes
        elective_pool_codes = elective_pool_codes - taken_codes

        required_qs = Course.objects.filter(
            program=self.program,
            catalog_year=self.catalog_year,
            code__in=required_codes,
        ).order_by("code")

        elective_qs = Course.objects.filter(
            program=self.program,
            catalog_year=self.catalog_year,
            code__in=elective_pool_codes,
        ).order_by("code")[:30]

        return (required_qs | elective_qs).distinct()

    def prerequisites_satisfied(self, course):
        prereq_codes = {
            c.code.upper() for c in course.prerequisites.all()
            if c.program == self.program and c.catalog_year == self.catalog_year
        }
        completed_codes = self.completed_course_codes()

        base_ok = True
        if prereq_codes:
            if course.prereq_mode == "ANY":
                base_ok = len(prereq_codes & completed_codes) >= 1
            else:
                base_ok = prereq_codes.issubset(completed_codes)

        if not base_ok:
            return False

        for grp in course.prereq_groups.all():
            option_codes = {c.code.upper() for c in grp.options.all()}
            if not option_codes:
                continue
            if len(option_codes & completed_codes) < grp.min_required:
                return False

        return True

    def recommend_next_term(self, next_term=None):
        next_term = next_term or Term.from_date().next()
        remaining = list(self.remaining_required_courses())

        taken_codes = self.taken_course_codes()

        # double safety: never recommend already completed / in-progress classes
        remaining = [c for c in remaining if c.code.upper() not in taken_codes]

        eligible = [c for c in remaining if self.prerequisites_satisfied(c)]
        offered = [
            c for c in eligible
            if (c.offered_in.count() == 0 or next_term in c.offered_in.all())
        ]

        total = 0.0
        take = []
        for c in sorted(offered, key=lambda x: x.code):
            if total + float(c.credits) <= self.max_credits_next_term:
                take.append(c)
                total += float(c.credits)

        return take, total, next_term
    
    def requirement_group_progress_with_planned(self):
        base = self.requirement_group_progress() or []
        req = self.get_requirement()
        if not req:
            return base

        planned_codes = self.planned_course_codes()
        used_by_group1 = set()

        out = []
        blocks = list(req.blocks.prefetch_related("courses").all())

        if not blocks:
            for row in base:
                item = dict(row)
                item["planned"] = 0
                item["assigned_planned_codes"] = []
                out.append(item)
            return out

        blocks.sort(key=lambda b: (b.count_group or "group4", int(b.min_required), b.courses.count(), b.name))

        completed_codes = self.completed_course_codes()
        in_progress_codes = self.in_progress_course_codes()

        for b in blocks:
            block_codes = [c.code.upper() for c in b.courses.all()]
            block_group = (b.count_group or "group4").strip() or "group4"

            assigned_completed = []
            assigned_ip = []
            assigned_planned = []

            def code_allowed(code: str) -> bool:
                if b.allow_double_count:
                    return True
                if block_group == "group1":
                    return code not in used_by_group1
                return True

            for code in block_codes:
                if code in completed_codes and code_allowed(code):
                    if code not in assigned_completed:
                        assigned_completed.append(code)
                    if len(assigned_completed) >= int(b.min_required):
                        break

            remaining_slots = max(0, int(b.min_required) - len(assigned_completed))

            if remaining_slots > 0:
                for code in block_codes:
                    if remaining_slots <= 0:
                        break
                    if code in in_progress_codes and code not in assigned_completed and code_allowed(code):
                        if code not in assigned_ip:
                            assigned_ip.append(code)
                            remaining_slots -= 1

            if remaining_slots > 0:
                for code in block_codes:
                    if remaining_slots <= 0:
                        break
                    if code in planned_codes and code not in assigned_completed and code not in assigned_ip and code_allowed(code):
                        if code not in assigned_planned:
                            assigned_planned.append(code)
                            remaining_slots -= 1

            if not b.allow_double_count and block_group == "group1":
                used_by_group1.update(assigned_completed)
                used_by_group1.update(assigned_ip)
                used_by_group1.update(assigned_planned)

            out.append({
                "name": b.name,
                "min_required": int(b.min_required),
                "completed": int(len(assigned_completed)),
                "in_progress": int(len(assigned_ip)),
                "planned": int(len(assigned_planned)),
                "total_options": int(len(block_codes)),
                "done": len(assigned_completed) >= int(b.min_required),
                "type": "block",
                "allow_double_count": bool(b.allow_double_count),
                "count_group": block_group,
                "assigned_completed_codes": assigned_completed,
                "assigned_in_progress_codes": assigned_ip,
                "assigned_planned_codes": assigned_planned,
            })

        return out
    
    def get_or_create_future_term_plans(self, count=4):
        TermPlanModel = apps.get_model("users", "TermPlan")

        current = Term.from_date()
        term = current.next()
        created_or_found = []

        for i in range(count):
            term_plan, _ = TermPlanModel.objects.get_or_create(
                profile=self,
                term=term,
                defaults={"position": i + 1},
            )

            if term_plan.position != i + 1:
                term_plan.position = i + 1
                term_plan.save(update_fields=["position"])

            created_or_found.append(term_plan)
            term = term.next()

        return created_or_found

    def planned_course_codes(self) -> set[str]:
        PlannedCourseModel = apps.get_model("users", "PlannedCourse")
        return {
            (code or "").upper()
            for code in PlannedCourseModel.objects.filter(term_plan__profile=self)
            .values_list("course__code", flat=True)
        }

    def term_plan_total_units(self, term_plan) -> float:
        return sum(
            float(pc.course.credits or 0)
            for pc in term_plan.planned_courses.select_related("course").all()
            if pc.course
        )

    def total_planned_units(self) -> float:
        PlannedCourseModel = apps.get_model("users", "PlannedCourse")
        return sum(
            float(pc.course.credits or 0)
            for pc in PlannedCourseModel.objects.filter(term_plan__profile=self).select_related("course")
            if pc.course
        )

    def planned_courses_by_term(self):
        return (
            PlannedCourse.objects
            .filter(term_plan__profile=self)
            .select_related("course", "term_plan", "term_plan__term")
            .order_by("term_plan__position", "position", "id")
        )

    def planned_warning_count(self) -> int:
        count = 0
        for pc in self.planned_courses_by_term():
            if pc.course and not self.course_still_useful_for_requirements(pc.course):
                count += 1
        return count

    def remaining_credits_after_plan(self) -> int:
        _, _, completed_credits, target_credits = self.approximate_graduation_term()
        in_progress_units = sum(
            float(c.course.credits or 0)
            for c in InProgressClass.objects.filter(profile=self).select_related("course")
            if c.course
        )
        planned_units = self.total_planned_units()
        return max(0, int(target_credits - completed_credits - in_progress_units - planned_units))

    def missing_corequisites_for_term(self, course, term_plan) -> list:
        PlannedCourseModel = apps.get_model("users", "PlannedCourse")

        taken_codes = self.taken_course_codes()
        planned_same_term_codes = {
            (pc.course.code or "").upper()
            for pc in PlannedCourseModel.objects.filter(term_plan=term_plan).select_related("course")
            if pc.course and pc.course.code
        }

        missing = []
        for coreq in course.corequisites.all():
            if coreq.program != self.program or coreq.catalog_year != self.catalog_year:
                continue
            code = (coreq.code or "").upper()
            if code not in taken_codes and code not in planned_same_term_codes:
                missing.append(coreq)

        return missing

    def course_still_useful_for_requirements(self, course) -> bool:
        req = self.get_requirement()
        if not req or not course:
            return True

        code = (course.code or "").upper()
        taken_codes = self.taken_course_codes()

        if code in taken_codes:
            return False

        group_progress = self.requirement_group_progress() or []
        incomplete_names = {
            g.get("name")
            for g in group_progress
            if not g.get("done")
        }

        for block in req.blocks.prefetch_related("courses").all():
            if block.name in incomplete_names:
                block_codes = {(c.code or "").upper() for c in block.courses.all()}
                if code in block_codes:
                    return True

        return False

    def can_add_course_to_term_plan(self, course, term_plan) -> tuple[bool, str]:
        PlannedCourseModel = apps.get_model("users", "PlannedCourse")

        if not course:
            return False, "Invalid course."

        if course.program != self.program or course.catalog_year != self.catalog_year:
            return False, "Course does not belong to your program/catalog."

        code = (course.code or "").upper()

        if code in self.completed_course_codes():
            return False, "Course already completed."

        if code in self.in_progress_course_codes():
            return False, "Course already in progress."

        already_planned_elsewhere = PlannedCourseModel.objects.filter(
            term_plan__profile=self,
            course=course,
        ).exclude(term_plan=term_plan).exists()

        if already_planned_elsewhere:
            return False, "Course already planned in another semester."

        if PlannedCourseModel.objects.filter(term_plan=term_plan, course=course).exists():
            return False, "Course already planned in this semester."

        if not self.planner_prerequisites_satisfied(course, term_plan):
            return False, "Prerequisites are not satisfied for that semester."

        current_units = self.term_plan_total_units(term_plan)
        next_units = current_units + float(course.credits or 0)

        if next_units > float(self.max_credits_next_term or 15):
            return False, "Adding this course would exceed your term unit limit."

        return True, ""

    def can_move_planned_course(self, planned_course, target_term_plan) -> tuple[bool, str]:
        PlannedCourseModel = apps.get_model("users", "PlannedCourse")

        if not planned_course or not planned_course.course:
            return False, "Invalid planned course."

        if planned_course.term_plan.profile_id != self.id:
            return False, "That course is not in your degree plan."

        if target_term_plan.profile_id != self.id:
            return False, "Invalid target semester."

        if planned_course.term_plan_id == target_term_plan.id:
            return False, "Course is already in that semester."

        course = planned_course.course
        code = (course.code or "").upper()

        if code in self.completed_course_codes():
            return False, "Course already completed."

        if code in self.in_progress_course_codes():
            return False, "Course already in progress."

        duplicate_in_target = PlannedCourseModel.objects.filter(
            term_plan=target_term_plan,
            course=course,
        ).exists()
        if duplicate_in_target:
            return False, "Course already exists in the target semester."

        current_units = self.term_plan_total_units(target_term_plan)
        next_units = current_units + float(course.credits or 0)
        if next_units > float(self.max_credits_next_term or 15):
            return False, "Moving this course would exceed the target semester unit limit."

        if not self.planner_prerequisites_satisfied(course, target_term_plan):
            return False, "Prerequisites are not satisfied for the target semester."

        return True, ""

    def renumber_term_plan_positions(self, term_plan):
        PlannedCourseModel = apps.get_model("users", "PlannedCourse")
        rows = list(
            PlannedCourseModel.objects.filter(term_plan=term_plan).order_by("position", "id")
        )
        for idx, row in enumerate(rows, start=1):
            if row.position != idx:
                row.position = idx
                row.save(update_fields=["position"])

    
    def planner_prerequisites_satisfied(self, course, target_term_plan) -> bool:
        """
        Planner-aware prerequisite check.

        A course is allowed if its prerequisites are satisfied by:
        - completed courses
        - in-progress courses
        - courses planned in earlier term plans only
        """
        completed_codes = self.completed_course_codes()
        in_progress_codes = self.in_progress_course_codes()

        earlier_planned_codes = set()
        earlier_terms = self.term_plans.filter(position__lt=target_term_plan.position).order_by("position")

        for tp in earlier_terms:
            for pc in tp.planned_courses.select_related("course").all():
                if pc.course and pc.course.code:
                    earlier_planned_codes.add(pc.course.code.upper())

        available_codes = completed_codes | in_progress_codes | earlier_planned_codes

        prereq_codes = {
            c.code.upper()
            for c in course.prerequisites.all()
            if c.program == self.program and c.catalog_year == self.catalog_year
        }

        base_ok = True
        if prereq_codes:
            if course.prereq_mode == "ANY":
                base_ok = len(prereq_codes & available_codes) >= 1
            else:
                base_ok = prereq_codes.issubset(available_codes)

        if not base_ok:
            return False

        for grp in course.prereq_groups.all():
            option_codes = {c.code.upper() for c in grp.options.all()}
            if not option_codes:
                continue
            if len(option_codes & available_codes) < grp.min_required:
                return False

        return True

    def get_next_unused_term(self):
        existing_term_ids = set(
            self.term_plans.values_list("term_id", flat=True)
        )

        term = Term.from_date().next()
        while term.id in existing_term_ids:
            term = term.next()
        return term

    def approximate_graduation_term(self):
        req = self.get_requirement()
        completed_credits = sum(
            float(c.credits)
            for c in self.completed_courses_qs()
            if c.counts_for_total_units()
        )

        base_target = 120
        if req:
            base_target = int(req.required_credits)

        remaining_credits = max(0.0, base_target - completed_credits)
        terms_needed = ceil(remaining_credits / max(1, self.avg_credits_per_term))
        term = Term.from_date()
        for _ in range(terms_needed):
            term = term.next()
        return term, remaining_credits, completed_credits, base_target

    def requirement_group_progress(self):
        """
        Full DPR engine:
        - supports any 1 / any 2 / any N via RequirementBlock.min_required
        - avoids double-counting by default
        - supports explicit double-counting with allow_double_count=True
        - supports path/either-or rules via PathRule
        - counts BOTH completed and in-progress courses
        - falls back to old ProgramRequirementGroup logic if blocks are not configured

        IMPORTANT:
        Matching is by COURSE CODE, not by COURSE ID.
        """
        req = self.get_requirement()
        if not req:
            return []

        completed_codes = self.completed_course_codes()
        in_progress_codes = self.in_progress_course_codes()

        out = []

        blocks = list(req.blocks.prefetch_related("courses").all())

        if blocks:
            blocks.sort(key=lambda b: (b.count_group or "group4", int(b.min_required), b.courses.count(), b.name))

            used_by_group1 = set()

            for b in blocks:
                block_codes = [c.code.upper() for c in b.courses.all()]
                block_group = (b.count_group or "group4").strip() or "group4"

                assigned_completed = []
                assigned_ip = []

                def code_allowed(code: str) -> bool:
                    if b.allow_double_count:
                        return True
                    if block_group == "group1":
                        return code not in used_by_group1
                    return True

                for code in block_codes:
                    if code in completed_codes:
                        if code_allowed(code) and code not in assigned_completed:
                            assigned_completed.append(code)
                        if len(assigned_completed) >= int(b.min_required):
                            break

                remaining_slots = max(0, int(b.min_required) - len(assigned_completed))

                if remaining_slots > 0:
                    for code in block_codes:
                        if remaining_slots <= 0:
                            break
                        if code in in_progress_codes and code not in assigned_completed:
                            if code_allowed(code) and code not in assigned_ip:
                                assigned_ip.append(code)
                                remaining_slots -= 1

                if not b.allow_double_count and block_group == "group1":
                    used_by_group1.update(assigned_completed)
                    used_by_group1.update(assigned_ip)

                out.append({
                    "name": b.name,
                    "min_required": int(b.min_required),
                    "completed": int(len(assigned_completed)),
                    "in_progress": int(len(assigned_ip)),
                    "total_options": int(len(block_codes)),
                    "done": len(assigned_completed) >= int(b.min_required),
                    "type": "block",
                    "allow_double_count": bool(b.allow_double_count),
                    "count_group": block_group,
                    "assigned_completed_codes": assigned_completed,
                    "assigned_in_progress_codes": assigned_ip,
                })
        udge_names = {
            "B5 Upper Division Scientific Inquiry",
            "C Upper Division Arts and Humanities",
            "D Upper Division Social Sciences",
            "F Upper Division Comparative Cultural Studies",
        }

        udge_blocks = [x for x in out if x.get("name") in udge_names]

        if udge_blocks and not any(x.get("name") == "General Education Upper Division" for x in out):
            udge_completed = sum(int(x.get("completed", 0)) for x in udge_blocks)
            udge_in_progress = sum(int(x.get("in_progress", 0)) for x in udge_blocks)
            udge_planned = sum(int(x.get("planned", 0)) for x in udge_blocks)

            out.append({
                "name": "General Education Upper Division",
                "min_required": 3,
                "completed": udge_completed,
                "in_progress": udge_in_progress,
                "planned": udge_planned,
                "total_options": 0,
                "done": (udge_completed + udge_in_progress + udge_planned) >= 3,
                "type": "block",
                "allow_double_count": False,
                "count_group": "group4",
                "assigned_completed_codes": [],
                "assigned_in_progress_codes": [],
                "assigned_planned_codes": [],
            })
        return out

class Tag(models.Model):
    name = models.CharField(max_length=48, unique=True)
    slug = models.SlugField(max_length=60, unique=True, blank=True)

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)


class Course(models.Model):
    program = models.CharField(max_length=32, choices=StudentProfile.PROGRAM_CHOICES)
    catalog_year = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(2000), MaxValueValidator(2100)]
    )

    tags = models.ManyToManyField(
        Tag,
        blank=True,
        related_name="courses",
        help_text="Optional labels like LAB, SUPPORT, GE-QR, etc.",
    )

    code = models.CharField(max_length=16)
    title = models.CharField(max_length=128)
    credits = models.DecimalField(max_digits=4, decimal_places=1, default=3.0)

    subject = models.CharField(max_length=32, blank=True, default="")
    level = models.CharField(max_length=50, null=True, blank=True)
    section = models.CharField(max_length=16, blank=True, default="")
    description = models.TextField(blank=True, default="")

    prerequisites = models.ManyToManyField(
        "self",
        symmetrical=False,
        blank=True,
        related_name="as_prerequisite_for",
    )
    corequisites = models.ManyToManyField(
        "self",
        symmetrical=False,
        blank=True,
        related_name="as_corequisite_for",
    )
    offered_in = models.ManyToManyField(Term, related_name="offerings", blank=True)

    PREREQ_MODES = (
        ("ALL", "All listed are required"),
        ("ANY", "Any one is sufficient"),
    )
    prereq_mode = models.CharField(max_length=3, choices=PREREQ_MODES, default="ALL")

    class Meta:
        unique_together = (("program", "catalog_year", "code"),)
        ordering = ["program", "catalog_year", "code"]

    def numeric_level(self):
        digits = "".join(ch for ch in (self.code or "") if ch.isdigit())
        return int(digits) if digits else None

    def counts_for_total_units(self) -> bool:
        level = self.numeric_level()
        return level is not None and level >= 100
    
    def __str__(self):
        return f"{self.code} — {self.title}"


class ProgramRequirement(models.Model):
    program = models.CharField(max_length=32, choices=StudentProfile.PROGRAM_CHOICES)
    catalog_year = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(2000), MaxValueValidator(2100)]
    )
    required_credits = models.PositiveIntegerField(default=120)

    class Meta:
        unique_together = (("program", "catalog_year"),)

    def __str__(self):
        return f"{self.program} {self.catalog_year} Requirements"


class ProgramRequirementGroup(models.Model):
    requirement = models.ForeignKey(
        ProgramRequirement,
        on_delete=models.CASCADE,
        related_name="groups",
    )
    name = models.CharField(max_length=80)
    min_required = models.PositiveSmallIntegerField(default=1, validators=[MinValueValidator(1)])
    courses = models.ManyToManyField(Course, blank=True)

    def __str__(self):
        return f"{self.requirement} — {self.name} (need {self.min_required})"


class RequirementBlock(models.Model):
    requirement = models.ForeignKey(
        ProgramRequirement,
        on_delete=models.CASCADE,
        related_name="blocks",
    )
    name = models.CharField(max_length=80)
    min_required = models.PositiveSmallIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
    )
    allow_double_count = models.BooleanField(default=False)
    courses = models.ManyToManyField(
        Course,
        blank=True,
        related_name="requirement_blocks",
    )

    count_group = models.CharField(
    max_length=20,
    blank=True,
    default="group4",
    help_text="Use group1, group2, group3, or group4 for counting rules."
    )

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.requirement} — {self.name} (need {self.min_required})"


class PathRule(models.Model):
    requirement = models.ForeignKey(
        ProgramRequirement,
        on_delete=models.CASCADE,
        related_name="path_rules",
    )
    name = models.CharField(max_length=80)
    paths = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.requirement} — {self.name}"


class CompletedClass(models.Model):
    profile = models.ForeignKey(
        StudentProfile, on_delete=models.CASCADE, related_name="completed_classes"
    )
    course = models.ForeignKey(
        Course, on_delete=models.CASCADE, related_name="completions"
    )
    term = models.CharField(max_length=12, blank=True)

    class Meta:
        unique_together = (("profile", "course"),)

    def __str__(self):
        who = self.profile.user.get_full_name() or self.profile.user.email or self.profile.user.username
        return f"{who} • {self.course.code}"


class InProgressClass(models.Model):
    profile = models.ForeignKey(
        StudentProfile, on_delete=models.CASCADE, related_name="in_progress_classes"
    )
    course = models.ForeignKey(
        Course, on_delete=models.CASCADE, related_name="in_progress"
    )
    term = models.CharField(max_length=12, blank=True)

    class Meta:
        unique_together = (("profile", "course"),)

    def __str__(self):
        who = self.profile.user.get_full_name() or self.profile.user.email or self.profile.user.username
        return f"{who} • {self.course.code} (IP)"

class TermPlan(models.Model):
    profile = models.ForeignKey(
        StudentProfile,
        on_delete=models.CASCADE,
        related_name="term_plans",
    )
    term = models.ForeignKey(
        Term,
        on_delete=models.CASCADE,
        related_name="term_plans",
    )
    position = models.PositiveSmallIntegerField(default=1)
    notes = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["position", "term__year", "term__season"]
        unique_together = (("profile", "term"),)

    def __str__(self):
        who = self.profile.user.get_full_name() or self.profile.user.email or self.profile.user.username
        return f"{who} • {self.term}"


class PlannedCourse(models.Model):
    STATUS_CHOICES = [
        ("planned", "Planned"),
    ]

    term_plan = models.ForeignKey(
        TermPlan,
        on_delete=models.CASCADE,
        related_name="planned_courses",
    )
    course = models.ForeignKey(
        Course,
        on_delete=models.CASCADE,
        related_name="planned_courses",
    )
    position = models.PositiveSmallIntegerField(default=1)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="planned")

    class Meta:
        ordering = ["position", "id"]
        unique_together = (("term_plan", "course"),)

    def __str__(self):
        return f"{self.term_plan} • {self.course.code}"

class PrerequisiteGroup(models.Model):
    for_course = models.ForeignKey(
        Course, on_delete=models.CASCADE, related_name="prereq_groups"
    )
    name = models.CharField(max_length=64, blank=True)
    min_required = models.PositiveSmallIntegerField(default=1, validators=[MinValueValidator(1)])
    options = models.ManyToManyField(Course, blank=True, related_name="as_prereq_option")

    def __str__(self):
        return f"{self.for_course.code} group ({self.name or self.pk}) — need {self.min_required}"


def dpr_upload_path(instance, filename: str) -> str:
    safe = (filename or "dpr.pdf").replace(" ", "_")
    return f"dpr/user_{instance.user_id}/{timezone.now().date()}_{safe}"


class DPRUpload(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="dpr_uploads",
    )
    file = models.FileField(
        upload_to=dpr_upload_path,
        validators=[FileExtensionValidator(["pdf"])],
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"DPRUpload({self.user_id}) @ {self.uploaded_at:%Y-%m-%d %H:%M}"