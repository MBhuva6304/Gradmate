# users/admin.py

from django.contrib import admin
from django.db import models

from .models import (
    Tag,
    StudentProfile,
    Course,
    CompletedClass,
    InProgressClass,
    Term,
    ProgramRequirement,
    ProgramRequirementGroup,
    RequirementBlock,
    PathRule,
    EmailOTP,
    PrerequisiteGroup,
)

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


@admin.register(StudentProfile)
class StudentProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "program", "catalog_year", "avg_credits_per_term", "max_credits_next_term")
    search_fields = ("user__email", "user__username", "user__first_name", "user__last_name")


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ("name",)
    search_fields = ("name",)
    exclude = ("slug",)


@admin.register(Course)
class CourseAdmin(admin.ModelAdmin):
    list_display = (
        "code", "subject", "level", "section",
        "title", "credits", "prereq_mode",
        "program", "catalog_year",
    )
    list_filter = ("prereq_mode", "tags", "program", "catalog_year")
    search_fields = ("code", "subject", "title", "description")

    filter_horizontal = ("tags", "prerequisites", "corequisites", "offered_in")

    formfield_overrides = {
        models.TextField: {
            "widget": admin.widgets.AdminTextareaWidget(attrs={"rows": 3})
        },
    }

    fieldsets = (
        ("Course info", {
            "fields": (
                "code",
                "subject",
                "title",
                "credits",
                "level",
                "description",
                "program",
                "catalog_year",
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


@admin.register(CompletedClass)
class CompletedClassAdmin(admin.ModelAdmin):
    list_display = ("profile", "course", "term")
    list_filter = ("course__program", "course__catalog_year")
    search_fields = ("profile__user__email", "course__code", "course__title")


@admin.register(InProgressClass)
class InProgressClassAdmin(admin.ModelAdmin):
    list_display = ("profile", "course", "term")
    list_filter = ("course__program", "course__catalog_year")
    search_fields = ("profile__user__email", "course__code", "course__title")


@admin.register(Term)
class TermAdmin(admin.ModelAdmin):
    list_display = ("season", "year")
    list_filter = ("season", "year")
    ordering = ("year", "season")


@admin.register(ProgramRequirement)
class ProgramRequirementAdmin(admin.ModelAdmin):
    list_display = ("program", "catalog_year", "required_credits")
    list_filter = ("program", "catalog_year")
    inlines = [ProgramRequirementGroupInline]


@admin.register(ProgramRequirementGroup)
class ProgramRequirementGroupAdmin(admin.ModelAdmin):
    list_display = ("name", "requirement", "min_required")
    list_filter = ("requirement__program", "requirement__catalog_year")
    search_fields = ("name", "requirement__program")
    filter_horizontal = ("courses",)


@admin.register(RequirementBlock)
class RequirementBlockAdmin(admin.ModelAdmin):
    list_display = ("name", "requirement", "min_required", "allow_double_count")
    list_filter = ("requirement__program", "requirement__catalog_year", "allow_double_count")
    search_fields = ("name", "requirement__program")
    filter_horizontal = ("courses",)


@admin.register(PathRule)
class PathRuleAdmin(admin.ModelAdmin):
    list_display = ("name", "requirement")
    list_filter = ("requirement__program", "requirement__catalog_year")
    search_fields = ("name", "requirement__program")


@admin.register(EmailOTP)
class EmailOTPAdmin(admin.ModelAdmin):
    list_display = ("user", "purpose", "created_at", "expires_at", "is_used", "attempts", "max_attempts")
    list_filter = ("purpose", "is_used", "created_at")
    search_fields = ("user__email", "user__username")
    readonly_fields = ("created_at",)