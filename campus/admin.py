from django.contrib import admin
from .models import Course, Term, Program, DegreeRequirement, StudentProfile, Enrollment

admin.site.register([Course, Term, Program, DegreeRequirement, StudentProfile, Enrollment])
