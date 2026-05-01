
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

    def _get_cache(self) -> dict:
        if '_runtime_cache' not in self.__dict__:
            self.__dict__['_runtime_cache'] = {}
        return self.__dict__['_runtime_cache']

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
        cache = self._get_cache()
        if 'completed_codes' not in cache:
            db_codes = set(
                CompletedClass.objects.filter(profile=self)
                .values_list("course__code", flat=True)
            )
            cache['completed_codes'] = {c.upper() for c in (db_codes | self._parsed_completed_codes())}
        return cache['completed_codes']

    def in_progress_course_codes(self) -> set[str]:
        cache = self._get_cache()
        if 'in_progress_codes' not in cache:
            cache['in_progress_codes'] = {
                c.upper()
                for c in InProgressClass.objects.filter(profile=self)
                .values_list("course__code", flat=True)
            }
        return cache['in_progress_codes']

    def taken_course_codes(self) -> set[str]:
        return self.completed_course_codes() | self.in_progress_course_codes()

    def get_requirement(self):
        return ProgramRequirement.objects.filter(
            program=self.program,
            catalog_year=self.catalog_year,
        ).first()

    def remaining_required_courses(self):
        """
        Return courses still useful for unfinished requirements.

        Rules:
        - completed and in-progress courses are excluded
        - count_group exclusivity is respected when allow_double_count=False
        - Senior Electives are tracked by units, not row count
        """
        req = self.get_requirement()
        if not req:
            return Course.objects.none()

        taken_codes = {code.upper() for code in self.taken_course_codes()}
        blocks = list(req.blocks.prefetch_related("courses").all())
        if not blocks:
            return Course.objects.none()

        blocks.sort(key=lambda b: ((b.count_group or "group4"), int(b.min_required), b.courses.count(), b.name))
        used_by_count_group = {}
        needed_codes = set()

        for b in blocks:
            block_group = (b.count_group or "group4").strip() or "group4"
            block_courses = list(b.courses.all())
            code_to_course = {
                (c.code or "").upper(): c
                for c in block_courses
                if c.code
            }
            block_codes = self._prioritized_block_codes(b, blocks)

            def code_allowed(code: str) -> bool:
                if b.allow_double_count:
                    return True
                used_codes = used_by_count_group.setdefault(block_group, set())
                return code not in used_codes

            chosen_codes = []

            if b.name == "Senior Electives":
                earned_units = 0.0

                for code in block_codes:
                    course = code_to_course.get(code)
                    if not course:
                        continue
                    if code in taken_codes and code_allowed(code):
                        chosen_codes.append(code)
                        earned_units += float(course.credits or 0)
                        if earned_units >= float(b.min_required):
                            break

                remaining_units = max(0.0, float(b.min_required) - earned_units)

                if remaining_units > 0:
                    future_units = 0.0
                    for code in block_codes:
                        course = code_to_course.get(code)
                        if not course:
                            continue
                        if code in taken_codes:
                            continue
                        if not code_allowed(code):
                            continue
                        if code in chosen_codes:
                            continue

                        needed_codes.add(code)
                        chosen_codes.append(code)
                        future_units += float(course.credits or 0)
                        if future_units >= remaining_units:
                            break

            else:
                assigned = []
                for code in block_codes:
                    if code in taken_codes and code_allowed(code):
                        if code not in assigned:
                            assigned.append(code)
                        if len(assigned) >= int(b.min_required):
                            break

                still_needed = max(0, int(b.min_required) - len(assigned))

                future = []
                if still_needed > 0:
                    for code in block_codes:
                        if code in taken_codes:
                            continue
                        if not code_allowed(code):
                            continue
                        if code in future:
                            continue
                        future.append(code)
                        needed_codes.add(code)
                        if len(future) >= still_needed:
                            break

                chosen_codes = assigned + future

            if not b.allow_double_count:
                used_codes = used_by_count_group.setdefault(block_group, set())
                used_codes.update(chosen_codes)

        return Course.objects.filter(
            program=self.program,
            catalog_year=self.catalog_year,
            code__in=needed_codes,
        ).prefetch_related(
            "prerequisites",
            "corequisites",
            "prereq_groups__options",
            "offered_in",
        ).order_by("code").distinct()

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

    def _prereqs_satisfied_with(self, course, simulated_taken: set) -> bool:
        """Like prerequisites_satisfied but checks against an arbitrary 'taken' set.
        Used by build_full_plan to treat courses planned in earlier terms as completed."""
        prereq_codes = {
            c.code.upper() for c in course.prerequisites.all()
            if c.program == self.program and c.catalog_year == self.catalog_year
        }

        if prereq_codes:
            if course.prereq_mode == "ANY":
                if not (prereq_codes & simulated_taken):
                    return False
            else:
                if not prereq_codes.issubset(simulated_taken):
                    return False

        for grp in course.prereq_groups.all():
            option_codes = {c.code.upper() for c in grp.options.all()}
            if not option_codes:
                continue
            if len(option_codes & simulated_taken) < grp.min_required:
                return False

        return True

    def recommend_next_term(self, next_term=None):
        next_term = next_term or Term.from_date().next()

        taken_codes = self.taken_course_codes()
        planned_codes = self.planned_course_codes()
        progress_by_name = self._requirement_progress_map()
        block_code_map = self._requirement_block_code_map()
        completed_units = self._completed_units_for_planning()

        candidates = self.recommendation_candidates(
            progress_by_name=progress_by_name,
            block_code_map=block_code_map,
        )

        eligible = [c for c in candidates if self.prerequisites_satisfied(c)]

        offered = []
        for c in eligible:
            offered_terms = list(c.offered_in.all())
            if not offered_terms or next_term in offered_terms:
                offered.append(c)

        scored = []
        for c in offered:
            scored.append(
                self._score_course_for_recommendation(
                    c,
                    taken_codes=taken_codes,
                    planned_codes=planned_codes,
                    progress_by_name=progress_by_name,
                    block_code_map=block_code_map,
                    completed_units=completed_units,
                )
            )

        scored.sort(key=lambda item: (-item["score"], item["course"].code))

        total_units = 0.0
        selected = []
        recommendation_details = []

        for item in scored:
            c = item["course"]
            units = float(c.credits or 0)

            if total_units + units <= float(self.max_credits_next_term or 15):
                selected.append(c)
                recommendation_details.append(item)
                total_units += units

        # Force-pair corequisites: if a selected course has a coreq not yet taken or selected,
        # add it to the same term regardless of unit budget.
        selected_codes = {(c.code or "").upper() for c in selected}
        for c in list(selected):
            for coreq in c.corequisites.all():
                if coreq.program != self.program or coreq.catalog_year != self.catalog_year:
                    continue
                coreq_code = (coreq.code or "").upper()
                if coreq_code in taken_codes or coreq_code in selected_codes:
                    continue
                selected.append(coreq)
                selected_codes.add(coreq_code)
                total_units += float(coreq.credits or 0)
                recommendation_details.append({
                    "course": coreq,
                    "score": 0,
                    "reasons": {},
                    "labels": ["Required corequisite"],
                })

        return selected, total_units, next_term, recommendation_details
    
    def recommend_for_term_plan(self, term_plan, remaining_courses=None, limit=5):
        progress_by_name = self._requirement_progress_map()
        block_code_map = self._requirement_block_code_map()
        taken_codes = self.taken_course_codes()
        planned_codes = self.planned_course_codes()
        completed_units = self._completed_units_for_planning()

        remaining_courses = remaining_courses or list(self.remaining_required_courses())

        target_term = term_plan.term

        scored = []
        for course in remaining_courses:
            ok, _reason = self.can_add_course_to_term_plan(course, term_plan)
            if not ok:
                continue

            # Skip courses not offered this specific term
            offered_terms = list(course.offered_in.all())
            if offered_terms and target_term not in offered_terms:
                continue

            item = self._score_course_for_recommendation(
                course,
                taken_codes=taken_codes,
                planned_codes=planned_codes,
                progress_by_name=progress_by_name,
                block_code_map=block_code_map,
                completed_units=completed_units,
            )

            scored.append(item)

        scored.sort(key=lambda item: (-item["score"], item["course"].code))
        return scored[:limit]
    
    def _completed_units_for_planning(self):
        completed = self.completed_classes.select_related("course").all()
        in_progress = self.in_progress_classes.select_related("course").all()

        completed_units = sum(float(row.course.credits or 0) for row in completed if row.course)
        in_progress_units = sum(float(row.course.credits or 0) for row in in_progress if row.course)

        return completed_units + in_progress_units
    
    def _course_number_value(self, course):
        if not course:
            return 0

        raw = str(getattr(course, "code", "") or "").strip().upper()
        digits = "".join(ch for ch in raw if ch.isdigit())
        return int(digits) if digits else 0
    
    def _is_upper_division_course(self, course):
        return self._course_number_value(course) >= 300

    def _is_udge_course(self, course, block_code_map=None):
        """Return True if course belongs to any Upper Division GE requirement block."""
        if block_code_map is None:
            block_code_map = self._requirement_block_code_map()
        code = (course.code or "").upper()
        return any(
            "upper division" in block_name.lower() and code in block_codes
            for block_name, block_codes in block_code_map.items()
        )

    def _term_timing_score(self, course, completed_units=None, block_code_map=None):
        completed_units = completed_units if completed_units is not None else self._completed_units_for_planning()
        code = (course.code or "").upper()

        score = 0

        # Hard timing rule: UDGE courses require 60+ units completed
        if completed_units < 60 and self._is_udge_course(course, block_code_map=block_code_map):
            return -50

        if self.program == "BS_CS":
            if code in {"COMP-490", "COMP-490L", "COMP-491L"}:
                if completed_units < 75:
                    score -= 40
                elif completed_units < 90:
                    score -= 18
                else:
                    score += 4

            elif code in {"COMP-380", "COMP-380L", "COMP-322", "COMP-322L", "COMP-333", "COMP-324", "COMP-310"}:
                if completed_units < 45:
                    score -= 8
                else:
                    score += 4

            elif self._is_upper_division_course(course):
                if completed_units < 60:
                    score -= 6
                else:
                    score += 2

        return score
    
    def _recommendation_labels(self, reasons):
        labels = []

        if reasons.get("block_completer"):
            labels.append("Completes a section")

        if reasons.get("unlock", 0) > 0:
            labels.append("Unlocks future courses")

        if reasons.get("multi_block", 0) > 0:
            labels.append("Fills multiple requirements")

        if reasons.get("scarcity", 0) >= 8:
            labels.append("Fills narrow requirement")
        elif reasons.get("scarcity", 0) > 0:
            labels.append("Helps requirement progress")

        if reasons.get("critical_path", 0) >= 10:
            labels.append("Critical path")
        elif reasons.get("critical_path", 0) > 0:
            labels.append("Major path")

        if reasons.get("timing", 0) > 0:
            labels.append("Good for this stage")
        elif reasons.get("timing", 0) < 0:
            labels.append("Better later")

        return labels

    def _score_course_for_recommendation(
        self,
        course,
        taken_codes,
        planned_codes,
        progress_by_name,
        block_code_map=None,
        completed_units=None,
    ):
        usable_codes = taken_codes | planned_codes
        code = (course.code or "").upper()

        unlock_score = self._unlock_score(course, usable_codes)
        scarcity_score = self._requirement_scarcity_score(
            course,
            progress_by_name=progress_by_name,
            taken_codes=taken_codes,
            planned_codes=planned_codes,
            block_code_map=block_code_map,
        )

        critical_path_score = 0
        if self.program == "BS_CS":
            critical_path_score = self._cs_critical_path_score(course)

        timing_score = self._term_timing_score(course, completed_units=completed_units, block_code_map=block_code_map)

        # Bonus for courses that count toward multiple unfulfilled blocks
        multi_block_count = sum(
            1 for block_name, block_codes in (block_code_map or {}).items()
            if code in block_codes and not progress_by_name.get(block_name, {}).get("done")
        )
        multi_block_score = max(0, (multi_block_count - 1) * 4)

        # Detect if taking this course would complete any requirement block
        block_completer = False
        for block_name, block_codes in (block_code_map or {}).items():
            if code not in block_codes:
                continue
            row = progress_by_name.get(block_name)
            if not row or row.get("done"):
                continue
            completed = float(row.get("completed") or 0)
            in_progress = float(row.get("in_progress") or 0)
            min_req = float(row.get("min_required") or 0)
            if min_req - completed - in_progress <= 1:
                block_completer = True
                break

        # Flag unsatisfied corequisites (informational — no score penalty)
        coreq_warning = any(
            coreq.program == self.program
            and coreq.catalog_year == self.catalog_year
            and (coreq.code or "").upper() not in taken_codes
            and (coreq.code or "").upper() not in planned_codes
            for coreq in course.corequisites.all()
        )

        total_score = unlock_score + scarcity_score + critical_path_score + timing_score + multi_block_score

        if self.program == "BS_CS" and self.catalog_year == 2023:
            if code.startswith("COMP-4") or code.startswith("COMP-5"):
                total_score += 3

        reasons = {
            "unlock": unlock_score,
            "scarcity": scarcity_score,
            "critical_path": critical_path_score,
            "timing": timing_score,
            "multi_block": multi_block_score,
            "block_completer": block_completer,
            "coreq_warning": coreq_warning,
        }

        return {
            "course": course,
            "score": total_score,
            "reasons": reasons,
            "labels": self._recommendation_labels(reasons),
        }

    def _required_course_pool(self):
        req = self.get_requirement()
        if not req:
            return Course.objects.none()

        return (
            Course.objects.filter(
                program=self.program,
                catalog_year=self.catalog_year,
            )
            .prefetch_related(
                "prerequisites",
                "corequisites",
                "offered_in",
                "requirement_blocks",
                "as_prerequisite_for",
                "as_prerequisite_for__prerequisites",
                "as_prerequisite_for__as_prerequisite_for",
                "as_prerequisite_for__as_prerequisite_for__prerequisites",
                "prereq_groups__options",
            )
            .distinct()
        )
    
    def effective_target_credits(self, include_planned=False):
        """
        Graduation needs BOTH:
        1. at least the catalog minimum units
        2. all required sections satisfied

        So effective target is the larger of:
        - catalog minimum credits
        - current earned/planned credits + remaining requirement units
        """
        req = self.get_requirement()
        base_target = int(req.required_credits) if req else 120

        completed_credits = sum(
            float(c.credits or 0)
            for c in self.completed_courses_qs()
            if c.counts_for_total_units()
        )

        in_progress_credits = sum(
            float(c.course.credits or 0)
            for c in InProgressClass.objects.filter(profile=self).select_related("course")
            if c.course
        )

        planned_credits = 0.0
        if include_planned:
            planned_credits = float(self.total_planned_units())

        current_total = completed_credits + in_progress_credits + planned_credits
        remaining_requirement_units = self._remaining_requirement_units(include_planned=include_planned)

        logical_target = current_total + remaining_requirement_units
        return max(float(base_target), float(logical_target))
    
    def _normalized_requirement_rows(self, include_planned=False):
        rows = (
            self.requirement_group_progress_with_planned()
            if include_planned else
            self.requirement_group_progress()
        )

        names = { (row.get("name") or "").strip() for row in rows }

        ud_helper_names = {
            "C Upper Division Arts and Humanities",
            "D Upper Division Social Sciences",
            "F Upper Division Comparative Cultural Studies",
            "F Upper Division Comparative Cultural Studies 2",
        }

        if "General Education Upper Division" in names:
            rows = [
                row for row in rows
                if (row.get("name") or "").strip() not in ud_helper_names
            ]

        return rows
    
    def all_sections_satisfied(self, include_planned=False):
        rows = self._normalized_requirement_rows(include_planned=include_planned)
        # Trust the done flag computed by the audit engine for each row.
        # For rows without a done flag, fall back to numeric comparison.
        for row in rows:
            if row.get("done"):
                continue
            required = float(row.get("min_required") or 0)
            if required <= 0:
                continue
            completed = float(row.get("completed") or 0)
            in_progress = float(row.get("in_progress") or 0)
            planned = float(row.get("planned") or 0) if include_planned else 0.0
            if completed + in_progress + planned < required:
                return False
        return True
    
    def _remaining_requirement_units(self, include_planned=False):
        """
        Credit estimate rule:

        - Normal missing section = 3 units per missing course-count
        - Senior Electives = real remaining units
        - If synthetic UDGE row exists, do not count the helper UD rows again
        """
        rows = self._normalized_requirement_rows(include_planned=include_planned)

        total = 0.0
        for row in rows:
            name = (row.get("name") or "").strip()

            required = float(row.get("min_required") or 0)
            completed = float(row.get("completed") or 0)
            in_progress = float(row.get("in_progress") or 0)
            planned = float(row.get("planned") or 0)

            progress_value = completed + in_progress
            if include_planned:
                progress_value += planned

            remaining = max(0.0, required - progress_value)
            if remaining <= 0:
                continue

            if name == "Senior Electives":
                total += remaining
            else:
                total += remaining * 3.0

        return total

    def _requirement_block_code_map(self):
        req = self.get_requirement()
        if not req:
            return {}

        return {
            block.name: {
                (c.code or "").upper()
                for c in block.courses.all()
                if c.code
            }
            for block in req.blocks.prefetch_related("courses").all()
        }
    
    def course_still_useful_for_requirements(
        self,
        course,
        progress_by_name=None,
        block_code_map=None,
    ):  
        code = (course.code or "").upper()
        req = self.get_requirement()

        if not req or not code:
            return False

        progress_by_name = progress_by_name or self._requirement_progress_map()
        block_code_map = block_code_map or self._requirement_block_code_map()

        for block_name, block_codes in block_code_map.items():
            if code not in block_codes:
                continue

            row = progress_by_name.get(block_name)
            if row and not row.get("done", False):
                return True

        return False

    def recommendation_candidates(self, progress_by_name=None, block_code_map=None):
        taken = self.taken_course_codes()
        planned = self.planned_course_codes()
        progress_by_name = progress_by_name or self._requirement_progress_map()
        block_code_map = block_code_map or self._requirement_block_code_map()

        candidates = []
        for c in self._required_course_pool():
            code = (c.code or "").upper()
            if code in taken or code in planned:
                continue
            if self.course_still_useful_for_requirements(
                c,
                progress_by_name=progress_by_name,
                block_code_map=block_code_map,
            ):
                candidates.append(c)

        return candidates
    
    def _unlock_score(self, course, taken_codes=None):
        taken_codes = taken_codes or (self.taken_course_codes() | self.planned_course_codes())
        this_code = (course.code or "").upper()
        score = 0
        directly_unlocked = set()

        # Depth 1: courses this directly enables
        for unlocked in course.as_prerequisite_for.all():
            if unlocked.program != self.program or unlocked.catalog_year != self.catalog_year:
                continue
            unlocked_code = (unlocked.code or "").upper()
            if unlocked_code in taken_codes:
                continue
            prereqs = {
                (p.code or "").upper()
                for p in unlocked.prerequisites.all()
                if p.program == self.program and p.catalog_year == self.catalog_year
            }
            if this_code not in prereqs:
                continue
            remaining = prereqs - taken_codes
            if len(remaining) == 1:
                score += 10
                directly_unlocked.add(unlocked)
            elif len(remaining) == 2:
                score += 6
            else:
                score += 2

        # Depth 2: what do directly-unlocked courses further enable?
        simulated = taken_codes | {this_code}
        for d1 in directly_unlocked:
            d1_code = (d1.code or "").upper()
            sim_d2 = simulated | {d1_code}
            for unlocked2 in d1.as_prerequisite_for.all():
                if unlocked2.program != self.program or unlocked2.catalog_year != self.catalog_year:
                    continue
                if (unlocked2.code or "").upper() in taken_codes:
                    continue
                prereqs2 = {
                    (p.code or "").upper()
                    for p in unlocked2.prerequisites.all()
                    if p.program == self.program and p.catalog_year == self.catalog_year
                }
                remaining2 = prereqs2 - sim_d2
                if len(remaining2) == 0:
                    score += 5
                elif len(remaining2) == 1:
                    score += 3

        return score
    
    def _requirement_scarcity_score(
        self,
        course,
        progress_by_name=None,
        taken_codes=None,
        planned_codes=None,
        block_code_map=None,
    ):
        req = self.get_requirement()
        if not req:
            return 0

        progress_by_name = progress_by_name or self._requirement_progress_map()
        taken_codes = taken_codes or self.taken_course_codes()
        planned_codes = planned_codes or self.planned_course_codes()
        block_code_map = block_code_map or self._requirement_block_code_map()

        code = (course.code or "").upper()
        score = 0

        for block_name, block_codes in block_code_map.items():
            if code not in block_codes:
                continue

            row = progress_by_name.get(block_name)
            if not row or row.get("done"):
                continue

            options_left = len([
                bc for bc in block_codes
                if bc not in taken_codes and bc not in planned_codes
            ])

            if options_left <= 1:
                score += 12
            elif options_left <= 2:
                score += 8
            elif options_left <= 4:
                score += 5
            else:
                score += 2

            # Extra bonus when taking this course would complete the block
            completed = float(row.get("completed") or 0)
            in_progress = float(row.get("in_progress") or 0)
            min_req = float(row.get("min_required") or 0)
            if min_req - completed - in_progress <= 1:
                score += 8

        return score
    
    def _cs_critical_path_score(self, course):
        code = (course.code or "").upper()

        weights = {
            "COMP-182": 16,
            "COMP-182L": 8,
            "COMP-256": 14,
            "COMP-256L": 8,
            "COMP-282": 15,
            "COMP-310": 13,
            "COMP-322": 14,
            "COMP-322L": 8,
            "COMP-333": 12,
            "COMP-380": 16,
            "COMP-380L": 8,
            "COMP-324": 10,
            "MATH-150A": 12,
            "MATH-150B": 10,
            "MATH-262": 10,
            "MATH-340": 9,
            "PHIL-230": 8,
            "COMP-490": -50,
            "COMP-490L": -50,
            "COMP-491L": -60,
        }

        return weights.get(code, 0)
    
    def alternative_courses_for_planned_sections(self, planned_courses, limit=12):
        """
        When all sections are already satisfied by the current plan,
        show alternatives from the same requirement blocks as the planned courses,
        excluding already taken/in-progress/planned courses.
        """
        req = self.get_requirement()
        if not req:
            return []

        taken_codes = self.taken_course_codes()
        planned_codes = self.planned_course_codes()
        used_codes = taken_codes | planned_codes

        target_block_names = set()
        for course in planned_courses:
            if not course:
                continue
            for block in course.requirement_blocks.all():
                target_block_names.add(block.name)

        alt_courses = []
        seen_ids = set()

        for block in req.blocks.prefetch_related("courses").all():
            if block.name not in target_block_names:
                continue

            for course in block.courses.all():
                code = (course.code or "").upper()
                if not code or code in used_codes:
                    continue
                if course.id in seen_ids:
                    continue
                alt_courses.append(course)
                seen_ids.add(course.id)

        alt_courses.sort(key=lambda c: (c.code or ""))
        return alt_courses[:limit]

    def _requirement_progress_map(self):
        rows = self.requirement_group_progress_with_planned()
        return {row["name"]: row for row in rows}
    
    def _prioritized_block_codes(self, block, all_blocks):
        current_group = (block.count_group or "group4").strip() or "group4"

        group_blocks = [
            b for b in all_blocks
            if ((b.count_group or "group4").strip() or "group4") == current_group
        ]

        block_code_sets = {
            b.id: {(c.code or "").upper() for c in b.courses.all() if c.code}
            for b in group_blocks
        }

        codes = list(block_code_sets.get(block.id, set()))

        overlap_counts = {
            code: sum(1 for s in block_code_sets.values() if code in s)
            for code in codes
        }

        codes.sort(key=lambda code: (overlap_counts.get(code, 9999), code))
        return codes
    def requirement_group_progress_with_planned(self):
        base = self.requirement_group_progress() or []
        req = self.get_requirement()
        if not req:
            return base

        planned_codes = self.planned_course_codes()
        used_by_count_group = {}

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

        completed_courses = list(
            CompletedClass.objects.filter(profile=self).select_related("course")
        )
        in_progress_courses = list(
            InProgressClass.objects.filter(profile=self).select_related("course")
        )
        planned_courses = list(
            PlannedCourse.objects.filter(term_plan__profile=self).select_related("course", "term_plan")
        )

        for b in blocks:
            block_codes = self._prioritized_block_codes(b, blocks)
            block_group = (b.count_group or "group4").strip() or "group4"

            assigned_completed = []
            assigned_ip = []
            assigned_planned = []

            def code_allowed(code: str) -> bool:
                if b.allow_double_count:
                    return True

                used_codes = used_by_count_group.setdefault(block_group, set())
                return code not in used_codes

            code_to_course = {
                (c.code or "").upper(): c
                for c in b.courses.all()
                if c.code
            }

            if b.name == "Senior Electives":
                earned_units = 0.0

                for code in block_codes:
                    course = code_to_course.get(code)
                    if not course:
                        continue
                    if code in completed_codes and code_allowed(code):
                        if code not in assigned_completed:
                            assigned_completed.append(code)
                            earned_units += float(course.credits or 0)
                        if earned_units >= float(b.min_required):
                            break

                remaining_units = max(0.0, float(b.min_required) - earned_units)

                if remaining_units > 0:
                    for code in block_codes:
                        course = code_to_course.get(code)
                        if not course:
                            continue
                        if code in in_progress_codes and code not in assigned_completed and code_allowed(code):
                            if code not in assigned_ip:
                                assigned_ip.append(code)
                                remaining_units -= float(course.credits or 0)
                            if remaining_units <= 0:
                                break

                if remaining_units > 0:
                    for code in block_codes:
                        course = code_to_course.get(code)
                        if not course:
                            continue
                        if code in planned_codes and code not in assigned_completed and code not in assigned_ip and code_allowed(code):
                            if code not in assigned_planned:
                                assigned_planned.append(code)
                                remaining_units -= float(course.credits or 0)
                            if remaining_units <= 0:
                                break
            else:
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

            if not b.allow_double_count:
                used_codes = used_by_count_group.setdefault(block_group, set())
                used_codes.update(assigned_completed)
                used_codes.update(assigned_ip)
                used_codes.update(assigned_planned)

            completed_rows = [cc for cc in completed_courses if cc.course and (cc.course.code or "").upper() in assigned_completed]
            ip_rows = [ic for ic in in_progress_courses if ic.course and (ic.course.code or "").upper() in assigned_ip]
            planned_rows = [pc for pc in planned_courses if pc.course and (pc.course.code or "").upper() in assigned_planned]

            completed_value = self._block_progress_value(b.name, completed_rows)
            in_progress_value = self._block_progress_value(b.name, ip_rows)
            planned_value = self._block_progress_value(b.name, planned_rows)
            required_value = float(b.min_required)

            row = {
                "name": b.name,
                "min_required": required_value,
                "completed": completed_value,
                "in_progress": in_progress_value,
                "planned": planned_value,
                "total_options": int(len(block_codes)),
                "done": (completed_value + in_progress_value + planned_value) >= required_value,
                "type": "block",
                "allow_double_count": bool(b.allow_double_count),
                "count_group": block_group,
                "assigned_completed_codes": assigned_completed,
                "assigned_in_progress_codes": assigned_ip,
                "assigned_planned_codes": assigned_planned,
            }

            if b.name == "Senior Electives":
                display = self._senior_elective_display_values(
                    units_completed=completed_value,
                    units_in_progress=in_progress_value,
                    units_planned=planned_value,
                    completed_rows=len(completed_rows),
                    in_progress_rows=len(ip_rows),
                    planned_rows=len(planned_rows),
                )
                row["display_required"] = display["display_required"]
                row["display_completed"] = display["display_completed"]
                row["display_in_progress"] = display["display_in_progress"]
                row["display_planned"] = display["display_planned"]
                row["display_remaining"] = display["display_remaining"]
                row["display_mode"] = "course_equivalent"

            out.append(row)

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

    def build_full_plan(self, term_plans=None):
        """Fill term_plans sequentially using smart scoring.
        Prereqs from earlier terms are treated as satisfied for later terms.
        Existing manually-planned courses are preserved; only missing gaps are filled.
        Returns (total_courses_placed, terms_actually_used)."""
        PlannedCourseModel = apps.get_model("users", "PlannedCourse")

        if term_plans is None:
            term_plans = self.get_or_create_future_term_plans(count=4)

        # Authoritative check first — same logic the audit page uses.
        # If all sections are already satisfied, do nothing.
        if self.all_sections_satisfied(include_planned=True):
            return 0, 0

        # Run all expensive queries once up-front
        actual_taken = self.taken_course_codes()
        completed_units = self._completed_units_for_planning()
        all_pool = list(self._required_course_pool())
        block_code_map = self._requirement_block_code_map()

        # Build block_remaining as a fast local tracker (done flag drives it)
        progress_by_name = self._requirement_progress_map()

        def _build_block_remaining():
            br = {}
            for bname in block_code_map:
                row = progress_by_name.get(bname)
                if not row or row.get("done"):
                    br[bname] = 0.0
                else:
                    min_req = float(row.get("min_required") or 0)
                    done_so_far = (
                        float(row.get("completed") or 0)
                        + float(row.get("in_progress") or 0)
                        + float(row.get("planned") or 0)
                    )
                    br[bname] = max(0.0, min_req - done_so_far)
            return br

        block_remaining = _build_block_remaining()

        # simulated_taken starts with already taken + already planned courses
        # so existing planned courses are respected and never duplicated
        existing_planned = self.planned_course_codes()
        simulated_taken = set(actual_taken) | existing_planned

        # Per-term unit caps: count units already in each term plan
        term_existing_units = {
            tp.id: float(
                PlannedCourseModel.objects.filter(term_plan=tp)
                .aggregate(s=models.Sum("course__credits"))
                .get("s") or 0
            )
            for tp in term_plans
        }

        # Per-term next position
        term_next_position = {
            tp.id: (
                PlannedCourseModel.objects.filter(term_plan=tp)
                .aggregate(mx=models.Max("position"))
                .get("mx") or 0
            ) + 1
            for tp in term_plans
        }

        def _block_still_needs(course):
            code = (course.code or "").upper()
            return any(
                code in block_code_map.get(bname, set())
                and block_remaining.get(bname, 0) > 0
                for bname in block_remaining
            )

        def _consume_block(course):
            code = (course.code or "").upper()
            credits = float(course.credits or 0)
            for bname, bcodes in block_code_map.items():
                if code in bcodes and block_remaining.get(bname, 0) > 0:
                    if bname == "Senior Electives":
                        block_remaining[bname] = max(0.0, block_remaining[bname] - credits)
                    else:
                        block_remaining[bname] = max(0.0, block_remaining[bname] - 1)

        # Pre-fetch offered_in for all pool courses to avoid N+1
        offered_cache = {}
        for c in all_pool:
            offered_cache[c.id] = set(c.offered_in.all())

        unit_limit = float(self.avg_credits_per_term or 15)
        total_placed = 0
        terms_used = 0

        for term_plan in term_plans:
            if self.all_sections_satisfied(include_planned=True):
                break

            # Refresh local tracker from DB after each term's placements
            progress_by_name = self._requirement_progress_map()
            block_remaining = _build_block_remaining()

            if all(v <= 0 for v in block_remaining.values()):
                break

            term = term_plan.term
            # Use existing units as starting point but allow filling up to unit_limit
            term_units = term_existing_units[term_plan.id]
            position = term_next_position[term_plan.id]

            # Filter candidates using simulated_taken (includes prior placements)
            candidates = [
                c for c in all_pool
                if (c.code or "").upper() not in simulated_taken
                and _block_still_needs(c)
                and (not offered_cache[c.id] or term in offered_cache[c.id])
                and self._prereqs_satisfied_with(c, simulated_taken)
                and not (
                    completed_units < 60
                    and self._is_udge_course(c, block_code_map=block_code_map)
                )
            ]

            if not candidates:
                continue

            scored = [
                self._score_course_for_recommendation(
                    c,
                    taken_codes=actual_taken,
                    planned_codes=simulated_taken,
                    progress_by_name=progress_by_name,
                    block_code_map=block_code_map,
                    completed_units=completed_units,
                )
                for c in candidates
            ]
            scored.sort(key=lambda x: (-x["score"], x["course"].code))

            new_this_term = []
            for item in scored:
                if all(v <= 0 for v in block_remaining.values()):
                    break
                c = item["course"]
                code = (c.code or "").upper()
                if code in simulated_taken:
                    continue
                if not _block_still_needs(c):
                    continue
                units = float(c.credits or 0)
                if term_units + units > unit_limit:
                    continue

                new_this_term.append(
                    PlannedCourseModel(
                        term_plan=term_plan,
                        course=c,
                        position=position,
                        status="planned",
                    )
                )
                simulated_taken.add(code)
                _consume_block(c)
                term_units += units
                total_placed += 1
                position += 1

                # Corequisites
                for coreq in c.corequisites.all():
                    if coreq.program != self.program or coreq.catalog_year != self.catalog_year:
                        continue
                    coreq_code = (coreq.code or "").upper()
                    if coreq_code in simulated_taken:
                        continue
                    new_this_term.append(
                        PlannedCourseModel(
                            term_plan=term_plan,
                            course=coreq,
                            position=position,
                            status="planned",
                        )
                    )
                    simulated_taken.add(coreq_code)
                    _consume_block(coreq)
                    term_units += float(coreq.credits or 0)
                    total_placed += 1
                    position += 1

            if new_this_term:
                PlannedCourseModel.objects.bulk_create(new_this_term, ignore_conflicts=True)
                terms_used += 1
                completed_units += (term_units - term_existing_units[term_plan.id])

        return total_placed, terms_used

    def planned_course_codes(self) -> set[str]:
        cache = self._get_cache()
        if 'planned_codes' not in cache:
            PlannedCourseModel = apps.get_model("users", "PlannedCourse")
            cache['planned_codes'] = {
                (code or "").upper()
                for code in PlannedCourseModel.objects.filter(term_plan__profile=self)
                .values_list("course__code", flat=True)
            }
        return cache['planned_codes']

    def term_plan_total_units(self, term_plan) -> float:
        cache = self._get_cache()
        key = f'term_units_{term_plan.id}'
        if key not in cache:
            cache[key] = sum(
                float(pc.course.credits or 0)
                for pc in term_plan.planned_courses.select_related("course").all()
                if pc.course
            )
        return cache[key]

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

    
    def _earlier_planned_codes_for(self, term_plan) -> set:
        cache = self._get_cache()
        key = f'earlier_codes_{term_plan.id}'
        if key not in cache:
            codes: set[str] = set()
            earlier_terms = (
                self.term_plans
                .filter(position__lt=term_plan.position)
                .order_by("position")
                .prefetch_related("planned_courses__course")
            )
            for tp in earlier_terms:
                for pc in tp.planned_courses.all():
                    if pc.course and pc.course.code:
                        codes.add(pc.course.code.upper())
            cache[key] = codes
        return cache[key]

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
        earlier_planned_codes = self._earlier_planned_codes_for(target_term_plan)
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
    
    def _block_progress_value(self, block_name, assigned_courses):
        """
        Most blocks count by number of assigned courses.
        Senior Electives count by units.
        """
        if block_name == "Senior Electives":
            return sum(float(row.course.credits or 0) for row in assigned_courses if getattr(row, "course", None))
        return float(len(assigned_courses))
    
    def _senior_elective_display_values(
        self,
        units_completed,
        units_in_progress=0.0,
        units_planned=0.0,
        completed_rows=0,
        in_progress_rows=0,
        planned_rows=0,
    ):
        """
        Keep real logic as 15 required units, but send a dynamic row-count display.

        Examples:
        - 5 three-unit rows -> 5 / 5
        - one 2+1 split can push display to 6 / 6
        - 2+2+2+1 means 4 rows used, 8 units left -> 3 more best-case rows -> 4 / 7
        """
        required_units = 15.0

        earned_units = float(units_completed) + float(units_in_progress) + float(units_planned)
        used_rows = int(completed_rows) + int(in_progress_rows) + int(planned_rows)

        remaining_units = max(0.0, required_units - earned_units)
        future_rows_needed = ceil(remaining_units / 3.0) if remaining_units > 0 else 0

        display_required = used_rows + future_rows_needed

        return {
            "display_required": float(display_required),
            "display_completed": float(completed_rows),
            "display_in_progress": float(in_progress_rows),
            "display_planned": float(planned_rows),
            "display_remaining": float(future_rows_needed),
            "required_units": required_units,
            "earned_units": earned_units,
            "remaining_units": remaining_units,
        }

    def approximate_graduation_term(self):
        req = self.get_requirement()
        base_target = int(req.required_credits) if req else 120

        completed_credits = sum(
            float(c.credits)
            for c in self.completed_courses_qs()
            if c.counts_for_total_units()
        )

        in_progress_credits = sum(
            float(c.course.credits or 0)
            for c in InProgressClass.objects.filter(profile=self).select_related("course")
            if c.course
        )

        effective_target = self.effective_target_credits(include_planned=False)
        current_total = completed_credits + in_progress_credits
        remaining_credits = max(0.0, effective_target - current_total)

        terms_needed = ceil(remaining_credits / max(1, self.avg_credits_per_term))
        term = Term.from_date()
        for _ in range(terms_needed):
            term = term.next()

        return term, remaining_credits, completed_credits, max(base_target, int(effective_target))
    

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

        completed_rows_all = list(
            CompletedClass.objects.filter(profile=self).select_related("course")
        )
        in_progress_rows_all = list(
            InProgressClass.objects.filter(profile=self).select_related("course")
        )

        out = []

        blocks = list(req.blocks.prefetch_related("courses").all())

        if blocks:
            blocks.sort(key=lambda b: (b.count_group or "group4", int(b.min_required), b.courses.count(), b.name))

            used_by_count_group = {}

            for b in blocks:
                block_codes = self._prioritized_block_codes(b, blocks)
                block_group = (b.count_group or "group4").strip() or "group4"

                assigned_completed = []
                assigned_ip = []

                def code_allowed(code: str) -> bool:
                    if b.allow_double_count:
                        return True

                    used_codes = used_by_count_group.setdefault(block_group, set())
                    return code not in used_codes

                code_to_course = {
                    (c.code or "").upper(): c
                    for c in b.courses.all()
                    if c.code
                }

                if b.name == "Senior Electives":
                    earned_units = 0.0

                    for code in block_codes:
                        course = code_to_course.get(code)
                        if not course:
                            continue
                        if code in completed_codes:
                            if code_allowed(code) and code not in assigned_completed:
                                assigned_completed.append(code)
                                earned_units += float(course.credits or 0)
                            if earned_units >= float(b.min_required):
                                break

                    remaining_units = max(0.0, float(b.min_required) - earned_units)

                    if remaining_units > 0:
                        for code in block_codes:
                            course = code_to_course.get(code)
                            if not course:
                                continue
                            if code in in_progress_codes and code not in assigned_completed:
                                if code_allowed(code) and code not in assigned_ip:
                                    assigned_ip.append(code)
                                    remaining_units -= float(course.credits or 0)
                                if remaining_units <= 0:
                                    break
                else:
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

                if not b.allow_double_count:
                    used_codes = used_by_count_group.setdefault(block_group, set())
                    used_codes.update(assigned_completed)
                    used_codes.update(assigned_ip)

                completed_rows = [
                    cc for cc in completed_rows_all
                    if cc.course and (cc.course.code or "").upper() in assigned_completed
                ]
                ip_rows = [
                    ic for ic in in_progress_rows_all
                    if ic.course and (ic.course.code or "").upper() in assigned_ip
                ]

                completed_value = self._block_progress_value(b.name, completed_rows)
                in_progress_value = self._block_progress_value(b.name, ip_rows)
                required_value = float(b.min_required)

                row = {
                    "name": b.name,
                    "min_required": required_value,
                    "completed": completed_value,
                    "in_progress": in_progress_value,
                    "total_options": int(len(block_codes)),
                    "done": (completed_value + in_progress_value) >= required_value,
                    "type": "block",
                    "allow_double_count": bool(b.allow_double_count),
                    "count_group": block_group,
                    "assigned_completed_codes": assigned_completed,
                    "assigned_in_progress_codes": assigned_ip,
                }

                if b.name == "Senior Electives":
                    display = self._senior_elective_display_values(
                        units_completed=completed_value,
                        units_in_progress=in_progress_value,
                        units_planned=0.0,
                        completed_rows=len(completed_rows),
                        in_progress_rows=len(ip_rows),
                        planned_rows=0,
                    )
                    row["display_required"] = display["display_required"]
                    row["display_completed"] = display["display_completed"]
                    row["display_in_progress"] = display["display_in_progress"]
                    row["display_planned"] = 0.0
                    row["display_remaining"] = display["display_remaining"]
                    row["display_mode"] = "course_equivalent"

                out.append(row)
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