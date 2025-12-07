from django.db import models
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator
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

    # free-text completions (comma/space separated codes)
    completed_codes = models.TextField(blank=True, default="")

    program = models.CharField(max_length=32, choices=PROGRAM_CHOICES)
    catalog_year = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(2000), MaxValueValidator(2100)],
        help_text="e.g., 2024",
    )

    # planning knobs
    avg_credits_per_term = models.PositiveIntegerField(default=15)
    max_credits_next_term = models.PositiveIntegerField(default=15)

    def __str__(self):
        who = self.user.get_full_name() or self.user.email or self.user.username
        return f"{who} — {self.get_program_display()} ({self.catalog_year})"

    # ----- helpers -----
    def _parsed_completed_codes(self) -> set[str]:
        raw = (self.completed_codes or "").upper()
        parts = [p.strip() for p in re.split(r"[,\s]+", raw) if p.strip()]
        return set(parts)

    def completed_courses_qs(self):
        """Catalog Course rows completed by this student (by code OR CompletedClass)."""
        codes = set(
            CompletedClass.objects.filter(profile=self)
            .values_list("course__code", flat=True)
        ) | self._parsed_completed_codes()
        return Course.objects.filter(
            program=self.program, catalog_year=self.catalog_year, code__in=codes
        )

    def get_requirement(self):
        # Safe helper: returns None if not set up yet
        return ProgramRequirement.objects.filter(
            program=self.program, catalog_year=self.catalog_year
        ).first()

    def remaining_required_courses(self):
        req = self.get_requirement()
        if not req:
            # No requirement configured yet → return empty queryset
            return Course.objects.none()
        done_ids = list(self.completed_courses_qs().values_list("id", flat=True))
        return req.courses.exclude(id__in=done_ids)

    def prerequisites_satisfied(self, course):
        """
        True if base prereqs (Course.prerequisites + prereq_mode) are met
        AND every PrerequisiteGroup linked to this course is satisfied.
        """
        # ---- base prereqs (ALL / ANY) ----
        prereq_ids = set(course.prerequisites.values_list("id", flat=True))
        completed_ids = set(self.completed_courses_qs().values_list("id", flat=True))

        base_ok = True
        if prereq_ids:
            if course.prereq_mode == "ANY":
                base_ok = len(prereq_ids & completed_ids) >= 1
            else:  # "ALL"
                base_ok = prereq_ids.issubset(completed_ids)
        if not base_ok:
            return False

        # ---- grouped alternatives (PrerequisiteGroup) ----
        for grp in course.prereq_groups.all():
            option_ids = set(grp.options.values_list("id", flat=True))
            if not option_ids:
                # empty group = ignore
                continue
            if len(option_ids & completed_ids) < grp.min_required:
                return False

        return True

    def recommend_next_term(self, next_term=None):
        next_term = next_term or Term.from_date().next()
        remaining = list(self.remaining_required_courses())
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

    def approximate_graduation_term(self):
        req = self.get_requirement()
        completed_credits = sum(float(c.credits) for c in self.completed_courses_qs())

        # Fallback target if requirement not configured yet
        base_target = 120
        if req:
            base_target = max(
                req.required_credits,
                sum(float(c.credits) for c in req.courses.all())
            )

        remaining_credits = max(0.0, base_target - completed_credits)
        terms_needed = ceil(remaining_credits / max(1, self.avg_credits_per_term))
        term = Term.from_date()
        for _ in range(terms_needed):
            term = term.next()
        return term, remaining_credits, completed_credits, base_target


# =========================
# Tags
# =========================
class Tag(models.Model):
    name = models.CharField(max_length=48, unique=True)
    slug = models.SlugField(max_length=60, unique=True, blank=True)

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)


# =========================
# Course catalog + reqs
# =========================
class Course(models.Model):
    """Catalog course for a given program + catalog year."""
    program = models.CharField(max_length=32, choices=StudentProfile.PROGRAM_CHOICES)
    catalog_year = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(2000), MaxValueValidator(2100)]
    )

    tags = models.ManyToManyField(
        Tag,
        blank=True,
        related_name="courses",
        help_text="Optional labels like LAB, SUPPORT, GE-QR, etc."
    )

    code = models.CharField(max_length=16)  # e.g., CS101
    title = models.CharField(max_length=128)
    credits = models.DecimalField(max_digits=4, decimal_places=1, default=3.0)

    subject = models.CharField(  # e.g., "CS" or "AAS"
        max_length=32,
        blank=True,
        default="",
        help_text="Department prefix, e.g., CS, AAS",
    )

    level = models.CharField(          # e.g., "100", "200"
        max_length=8,                  # ← this must be present
        null=True,
        blank=True,
        help_text="Course number, e.g., 151",
    )

    section = models.CharField(    # e.g., 01 or A
        max_length=32,
        blank=True,
        default="",
        help_text="Section code, e.g., 01 or A",
    )

    description = models.TextField(
        blank=True,
        default="",
        help_text="Optional short description of this course.",
    )

    prerequisites = models.ManyToManyField("self", symmetrical=False, blank=True)
    offered_in = models.ManyToManyField(Term, related_name="offerings", blank=True)

    PREREQ_MODES = (
        ("ALL", "All listed are required"),
        ("ANY", "Any one is sufficient"),
    )
    prereq_mode = models.CharField(
        max_length=3,
        choices=PREREQ_MODES,
        default="ALL",
        help_text="If 'ANY', completing any one of the listed prerequisites is enough."
    )

    class Meta:
        unique_together = (("program", "catalog_year", "code"),)
        ordering = ["program", "catalog_year", "code"]

    def __str__(self):
        return f"{self.code} — {self.title} ({self.program} {self.catalog_year})"


class ProgramRequirement(models.Model):
    """Set of required courses for a program/catalog year (+ an overall credit target)."""
    program = models.CharField(max_length=32, choices=StudentProfile.PROGRAM_CHOICES)
    catalog_year = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(2000), MaxValueValidator(2100)]
    )
    required_credits = models.PositiveIntegerField(default=120)
    courses = models.ManyToManyField(Course, blank=True)

    class Meta:
        unique_together = (("program", "catalog_year"),)

    def __str__(self):
        return f"{self.program} {self.catalog_year} Requirements"


class CompletedClass(models.Model):
    """A catalog course a student has already finished."""
    profile = models.ForeignKey(
        StudentProfile, on_delete=models.CASCADE, related_name="completed_classes"
    )
    course = models.ForeignKey(
        Course, on_delete=models.CASCADE, related_name="completions"
    )
    grade = models.CharField(max_length=2, blank=True)   # optional (A, B, P…)
    term = models.CharField(max_length=12, blank=True)   # optional (e.g., 'Fall 2024')

    class Meta:
        unique_together = (("profile", "course"),)

    def __str__(self):
        who = (
            self.profile.user.get_full_name()
            or self.profile.user.email
            or self.profile.user.username
        )
        return f"{who} • {self.course.code}"


# =========================
# Grouped alternative prereqs
# =========================
class PrerequisiteGroup(models.Model):
    """
    A group of alternative prerequisites for a specific course.
    The group is satisfied if the student has completed at least `min_required`
    of the `options`.
    """
    for_course = models.ForeignKey(
        Course, on_delete=models.CASCADE, related_name="prereq_groups"
    )
    name = models.CharField(
        max_length=64,
        blank=True,
        help_text="Optional label, e.g., 'Math alternatives'",
    )
    min_required = models.PositiveSmallIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        help_text="How many from this group are required.",
    )
    options = models.ManyToManyField(
        Course,
        blank=True,
        related_name="as_prereq_option",
        help_text="Courses that can satisfy this group.",
    )

    def __str__(self):
        return f"{self.for_course.code} group ({self.name or self.pk}) — need {self.min_required}"
