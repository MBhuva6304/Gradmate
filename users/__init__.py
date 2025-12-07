# users/__init__.py

"""
Users app package.

We intentionally keep models in models.py.
This file just re-exports them if you want `from users import Course`, etc.
"""

from .models import (
    EmailOTP,
    Term,
    StudentProfile,
    Tag,
    Course,
    ProgramRequirement,
    CompletedClass,
    PrerequisiteGroup,
)

__all__ = [
    "EmailOTP",
    "Term",
    "StudentProfile",
    "Tag",
    "Course",
    "ProgramRequirement",
    "CompletedClass",
    "PrerequisiteGroup",
]
