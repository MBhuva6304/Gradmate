# users/admin.py

from django.contrib import admin
from django.db import models

from .models import (
    Tag,
    StudentProfile,
    Course,
    CompletedClass,
    Term,
    ProgramRequirement,
    ProgramRequirementGroup,
    EmailOTP,
    PrerequisiteGroup,
)

# ──────────────────────────────────────────────────────────────────────────────
# Inline for grouped/alternative prerequisites (shown last on the page)
# ──────────────────────────────────────────────────────────────────────────────
class PrerequisiteGroupInline(admin.StackedInline):
    model = PrerequisiteGroup
    extra = 0
    filter_horizontal = ("options",)
    fields = ("name", "min_required", "options")

class ProgramRequirementGroupInline(admin.StackedInline):
    model = ProgramRequirementGroup
    extra = 1
    filter_horizontal = ("courses",)
    fields = ("name", "min_required", "courses")



# ──────────────────────────────────────────────────────────────────────────────
# StudentProfile admin
# ──────────────────────────────────────────────────────────────────────────────
@admin.register(StudentProfile)
class StudentProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "program", "catalog_year",
                    "avg_credits_per_term", "max_credits_next_term")
    list_filter = ("program", "catalog_year")
    search_fields = ("user__email", "user__username",
                     "user__first_name", "user__last_name")


# ──────────────────────────────────────────────────────────────────────────────
# Tag admin  (name only; slug hidden/auto)
# ──────────────────────────────────────────────────────────────────────────────
@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ("name",)
    search_fields = ("name",)
    exclude = ("slug",)   # keep slug out of the form


# ──────────────────────────────────────────────────────────────────────────────
# Course admin
# ──────────────────────────────────────────────────────────────────────────────
@admin.register(Course)
class CourseAdmin(admin.ModelAdmin):
    list_display = (
        "code", "subject", "level", "section",
        "title", "program", "catalog_year",
        "credits", "prereq_mode",
    )
    list_filter = ("program", "catalog_year", "prereq_mode", "tags")
    search_fields = ("code", "subject", "title", "description")

    filter_horizontal = ("tags", "prerequisites", "offered_in")

    formfield_overrides = {
        models.TextField: {
            "widget": admin.widgets.AdminTextareaWidget(attrs={"rows": 3})
        },
    }

    fieldsets = (
        ("Course info", {
            "fields": (
                "program",
                "catalog_year",
                "code",
                "subject",
                "title",
                "credits",
                "level",
                "description",
                "prerequisites",
                "corequisites",
                "prereq_mode",
                "section",
                "tags",
            ),
        }),
        ("Scheduling (optional)", {
            "fields": ("offered_in",),
            "classes": ("collapse",),
        }),
    )

    inlines = [PrerequisiteGroupInline]


# ──────────────────────────────────────────────────────────────────────────────
# CompletedClass admin  (no grade column)
# ──────────────────────────────────────────────────────────────────────────────
@admin.register(CompletedClass)
class CompletedClassAdmin(admin.ModelAdmin):
    list_display = ("profile", "course", "term")
    list_filter = ("course__program", "course__catalog_year")
    search_fields = ("profile__user__email", "course__code", "course__title")


# ──────────────────────────────────────────────────────────────────────────────
# Term admin
# ──────────────────────────────────────────────────────────────────────────────
@admin.register(Term)
class TermAdmin(admin.ModelAdmin):
    list_display = ("season", "year")
    list_filter = ("season", "year")
    ordering = ("year", "season")


# ──────────────────────────────────────────────────────────────────────────────
# ProgramRequirement admin
# ──────────────────────────────────────────────────────────────────────────────
@admin.register(ProgramRequirement)
class ProgramRequirementAdmin(admin.ModelAdmin):
    list_display = ("program", "catalog_year", "required_credits")
    list_filter = ("program", "catalog_year")
    inlines = [ProgramRequirementGroupInline]


# ──────────────────────────────────────────────────────────────────────────────
# EmailOTP admin
# ──────────────────────────────────────────────────────────────────────────────
@admin.register(EmailOTP)
class EmailOTPAdmin(admin.ModelAdmin):
    list_display = ("user", "purpose", "created_at", "expires_at",
                    "is_used", "attempts", "max_attempts")
    list_filter = ("purpose", "is_used", "created_at")
    search_fields = ("user__email", "user__username")
    readonly_fields = ("created_at",)
