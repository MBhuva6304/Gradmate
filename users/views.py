# users/views.py
from __future__ import annotations

import random
import re
from functools import wraps
from types import SimpleNamespace

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.hashers import make_password, check_password
from django.db.models import Q
from django.shortcuts import get_object_or_404, render, redirect
from django.urls import reverse
from django.utils import timezone
from .models import Course, Tag, StudentProfile
from .models import StudentProfile, Term, ProgramRequirement, Course

from .forms import (
    SignUpForm,
    OTPForm,
    ProfileSettingsForm,
    ProfileSetupForm,
)
from .models import (
    EmailOTP,
    StudentProfile,
    Term,
    ProgramRequirement,
    Course,
    # Tag is optional; we try to use it but gracefully fall back if absent
)

# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

EMAIL_BACKEND_PATH = "users.backends.EmailBackend"


def _gen6() -> str:
    """Generate a zero-padded 6-digit code as a string."""
    return f"{random.randint(0, 999999):06d}"


def _parse_codes(raw: str) -> list[str]:
    """
    Split by comma or whitespace, uppercase, drop blanks, and de-dupe
    while preserving order (for completed course codes input).
    """
    if not raw:
        return []
    parts = [p.strip().upper() for p in re.split(r"[,\s]+", raw) if p.strip()]
    seen, out = set(), []
    for p in parts:
        if p not in seen:
            out.append(p)
            seen.add(p)
    return out


def _wants_partial(request) -> bool:
    """
    True if the request is asking for a partial (AJAX fetch) rather than the full page.
    We support either ?partial=1 or a fetch/XHR header.
    """
    return (
        request.GET.get("partial") == "1"
        or request.headers.get("X-Requested-With") in {"fetch", "XMLHttpRequest"}
    )


# ----------------------------------------------------------------------
# Decorator: require a StudentProfile (and attach it to request)
# ----------------------------------------------------------------------

def profile_required(viewfunc):
    @wraps(viewfunc)
    @login_required
    def _wrapped(request, *args, **kwargs):
        profile = StudentProfile.objects.filter(user=request.user).first()
        if not profile:
            messages.info(request, "Create your student profile to continue.")
            next_url = request.get_full_path()
            return redirect(f"{reverse('setup_profile')}?next={next_url}")
        request.profile = profile
        return viewfunc(request, *args, **kwargs)

    return _wrapped


# ----------------------------------------------------------------------
# Signup + email verification (OTP)
# ----------------------------------------------------------------------

def signup(request):
    if request.method == "POST":
        form = SignUpForm(request.POST)
        if form.is_valid():
            # Create inactive user until they verify the email
            user = form.save(commit=False)
            user.is_active = False
            user.save()

            # Create/overwrite the initial profile
            profile, _ = StudentProfile.objects.update_or_create(
                user=user,
                defaults={
                    "program": form.cleaned_data["program"],
                    "catalog_year": int(form.cleaned_data["catalog_year"]),
                },
            )

            # Persist completed course codes
            codes = _parse_codes(form.cleaned_data.get("completed_codes", ""))
            profile.completed_codes = ", ".join(codes) if codes else ""
            profile.save(update_fields=["completed_codes"])

            # Send OTP
            code = _gen6()
            EmailOTP.create_for_user(user, make_password(code), purpose="VERIFY")
            send_to = [user.email]
            from django.core.mail import send_mail  # local import to avoid circulars
            send_mail(
                subject="Verify your email",
                message=f"Your verification code is: {code}\nThis code expires in 10 minutes.",
                from_email=None,
                recipient_list=send_to,
                fail_silently=False,
            )

            request.session["verify_email"] = user.email
            messages.success(request, "We emailed you a 6-digit code. Enter it to verify your account.")
            return redirect("verify-signup")
        else:
            messages.error(request, "Please fix the errors below.")
    else:
        form = SignUpForm()

    return render(request, "signup.html", {"form": form})


def verify_signup(request):
    """
    Verify the signup OTP stored in EmailOTP (purpose='VERIFY').
    On success: activate the user and log them in (explicit backend).
    """
    email = request.session.get("verify_email")
    if not email:
        messages.error(request, "Session expired. Please start again.")
        return redirect("signup")

    User = get_user_model()

    # Resend flow: /verify-signup/?resend=1
    if request.method == "GET" and request.GET.get("resend") == "1":
        try:
            user = User.objects.get(email__iexact=email)
        except User.DoesNotExist:
            messages.error(request, "No user found for this email.")
            return redirect("signup")

        code = _gen6()
        EmailOTP.create_for_user(user, make_password(code), purpose="VERIFY")
        from django.core.mail import send_mail
        send_mail(
            subject="Your verification code",
            message=f"Your verification code is: {code}\nThis code expires in 10 minutes.",
            from_email=None,
            recipient_list=[email],
            fail_silently=False,
        )
        messages.success(request, "We sent you a new verification code.")
        return render(request, "verify_email.html", {"form": OTPForm(), "email": email})

    if request.method == "POST":
        form = OTPForm(request.POST)
        if form.is_valid():
            code = form.cleaned_data["code"]

            try:
                user = User.objects.get(email__iexact=email)
            except User.DoesNotExist:
                messages.error(request, "No user found for this email.")
                return redirect("signup")

            otp = (
                EmailOTP.objects
                .filter(user=user, purpose="VERIFY", is_used=False)
                .order_by("-created_at")
                .first()
            )
            if not otp or not otp.is_valid() or not check_password(code, otp.code_hash):
                messages.error(request, "Invalid or expired code.")
                return render(request, "verify_email.html", {"form": form, "email": email})

            # Activate user & consume OTP
            user.is_active = True
            user.save(update_fields=["is_active"])
            otp.mark_used()

            # Log in with explicit backend (since multiple backends may be configured)
            backend = EMAIL_BACKEND_PATH
            if backend not in settings.AUTHENTICATION_BACKENDS:
                backend = settings.AUTHENTICATION_BACKENDS[0]
            login(request, user, backend=backend)

            request.session.pop("verify_email", None)
            return redirect("dashboard")
    else:
        form = OTPForm()

    return render(request, "verify_email.html", {"form": form, "email": email})


# ----------------------------------------------------------------------
# Setup profile
# ----------------------------------------------------------------------

@login_required
def setup_profile(request):
    # If profile already exists, bounce to next (or dashboard)
    if StudentProfile.objects.filter(user=request.user).exists():
        return redirect(request.GET.get("next") or "dashboard")

    if request.method == "POST":
        form = ProfileSetupForm(request.POST)
        if form.is_valid():
            profile = form.save(commit=False)
            profile.user = request.user
            profile.save()
            messages.success(request, "Profile created.")
            return redirect(request.GET.get("next") or "dashboard")
    else:
        form = ProfileSetupForm(initial={
            "catalog_year": timezone.localdate().year,
            "avg_credits_per_term": 15,
            "max_credits_next_term": 15,
        })
    return render(request, "setup_profile.html", {"form": form})


# ----------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------

@profile_required
def settings_page(request):
    profile = request.profile

    submit_kind = request.POST.get("form", "")
    profile_form = ProfileSettingsForm(
        request.user, profile,
        data=(request.POST if submit_kind == "profile" else None)
    )
    pwd_form = PasswordChangeForm(
        request.user,
        data=(request.POST if submit_kind == "password" else None)
    )

    if request.method == "POST":
        if submit_kind == "profile" and profile_form.is_valid():
            profile_form.save(request.user, profile)
            messages.success(request, "Profile updated.")
            return redirect("settings")

        if submit_kind == "password" and pwd_form.is_valid():
            user = pwd_form.save()
            update_session_auth_hash(request, user)  # keep user logged in
            messages.success(request, "Password changed.")
            return redirect("settings")

    return render(request, "settings.html", {
        "profile_form": profile_form,
        "pwd_form": pwd_form,
        "profile": profile,
    })


# ----------------------------------------------------------------------
# Dashboard / Audit / Degree plan
# ----------------------------------------------------------------------

@profile_required
def dashboard(request):
    profile = StudentProfile.objects.select_related("user").get(user=request.user)

    # Is a requirement configured?
    req_missing = not ProgramRequirement.objects.filter(
        program=profile.program, catalog_year=profile.catalog_year
    ).exists()

    # Base progress numbers
    remaining = profile.remaining_required_courses()
    next_list, next_credits, next_term = profile.recommend_next_term()
    grad_term, remaining_credits, completed_credits, target_credits = profile.approximate_graduation_term()

    # For now, no "in progress" slice (we'll keep it simple)
    in_progress_credits = 0

    # Remaining for the donut = target - completed
    remaining_for_chart = max(0, int(target_credits - completed_credits - in_progress_credits))

    # overall percentage (completed only)
    denom = target_credits or 1
    progress_percent = int(((completed_credits + in_progress_credits) / denom) * 100)

    context = {
        "profile": profile,
        "current_term": Term.from_date(),
        "grad_term": grad_term,

        "completed_credits": int(completed_credits),
        "in_progress_credits": int(in_progress_credits),
        "remaining_credits": remaining_for_chart,
        "target_credits": int(target_credits),
        "progress_percent": progress_percent,

        "remaining": remaining,
        "next_list": next_list,
        "next_credits": int(next_credits),
        "next_term": next_term,

        "req_missing": req_missing,
    }
    return render(request, "dashboard.html", context)


@profile_required
def degree_plan(request):
    return render(request, "degree_plan.html", {"profile": request.profile})


@profile_required
def audit(request):
    """
    Simple wrapper view for the degree audit page.
    It just renders templates/audit.html.
    """
    return render(request, "audit.html")


# ----------------------------------------------------------------------
# Courses list (STRICT) with live filters + Course detail with modal partial
# ----------------------------------------------------------------------

@profile_required
def courses_page(request):
    """
    Courses list page with search + filters + AJAX partial rendering.
    Filters:
      - q: free-text search
      - level: one of our custom ranges (000-099, 100-199, ...)
      - credits: exact numeric credits (1..5)
      - tag: fulfillment label, matched against Tag.name
    All queries are restricted to the student's program + catalog year.
    """
    profile = get_object_or_404(StudentProfile, user=request.user)

    # ---- base queryset (program + catalog year only) ----
    base_qs = (
        Course.objects
        .filter(program=profile.program, catalog_year=profile.catalog_year)
        .prefetch_related("tags")          # <-- select_related("program") removed
        .order_by("subject", "code")
    )

    # ---- free-text search ----
    q = request.GET.get("q", "").strip()
    if q:
        base_qs = (
            base_qs.filter(
                Q(code__icontains=q)
                | Q(title__icontains=q)
                | Q(subject__icontains=q)
                | Q(description__icontains=q)
                | Q(tags__name__icontains=q)
            )
            .distinct()
        )

    # work in Python from here (dataset is small: ~100 courses)
    courses = list(base_qs)

    # ---- LEVEL FILTER (by numeric part of course code) ----
    level_param = request.GET.get("level", "").strip()

    LEVEL_RANGES = {
        "000-099": (0, 99),
        "100-199": (100, 199),
        "200-299": (200, 299),
        "300-399": (300, 399),
        "400-499": (400, 499),
        "500+": (500, 9999),
    }

    def parse_number_from_code(code: str):
        digits = "".join(ch for ch in (code or "") if ch.isdigit())
        return int(digits) if digits else None

    if level_param in LEVEL_RANGES:
        lo, hi = LEVEL_RANGES[level_param]
        filtered = []
        for c in courses:
            n = parse_number_from_code(getattr(c, "code", ""))
            if n is not None and lo <= n <= hi:
                filtered.append(c)
        courses = filtered

    # ---- CREDITS FILTER ----
    credits_param = request.GET.get("credits", "").strip()
    if credits_param:
        try:
            wanted_credits = float(credits_param)
            courses = [
                c for c in courses
                if getattr(c, "credits", None) is not None
                and float(c.credits) == wanted_credits
            ]
        except ValueError:
            # ignore bad input; show all
            pass

    # ---- FULFILLMENT (TAG) FILTER ----
    tag_param = request.GET.get("tag", "").strip()
    if tag_param:
        courses = [
            c for c in courses
            if any(t.name == tag_param for t in c.tags.all())
        ]

    # ---- dropdown choices ----
    level_choices = [
        ("", "All levels"),
        ("000-099", "000–099"),
        ("100-199", "100–199"),
        ("200-299", "200–299"),
        ("300-399", "300–399"),
        ("400-499", "400–499"),
        ("500+", "500 and above"),
    ]

    credit_choices = [
        ("", "Any credits"),
        ("1", "1 credit"),
        ("2", "2 credits"),
        ("3", "3 credits"),
        ("4", "4 credits"),
        ("5", "5 credits"),
    ]

    fulfillment_labels = [
        "GE Section A",
        "GE Section B",
        "GE Section C",
        "GE Section D",
        "GE Section E",
        "GE Section F",
        "GE Quantitative Reasoning",
        "Lower Division Core",
        "Lower Division Elective",
        "Upper Division Core",
        "Upper Division GE",
        "Ethnic Studies",
        "Writing Skills",
        "Senior Elective Division",
    ]
    fulfillment_choices = [("", "Any fulfillment")] + [
        (label, label) for label in fulfillment_labels
    ]

    ctx = {
        "courses": courses,
        "profile": profile,
        "q": q,
        "selected_level": level_param,
        "selected_credits": credits_param,
        "selected_tag": tag_param,
        "level_choices": level_choices,
        "credit_choices": credit_choices,
        "fulfillment_choices": fulfillment_choices,
        "results_count": len(courses),
    }

    if _wants_partial(request):
        return render(request, "courses_list.html", ctx)

    return render(request, "courses.html", ctx)


@login_required
def course_detail(request, pk: int):
    """
    Show details for a single course (STRICT to student's program + catalog year).
    If a partial is requested, return the modal body HTML instead of the full page.
    """
    profile = get_object_or_404(StudentProfile, user=request.user)

    course = get_object_or_404(
        Course,
        pk=pk,
        program=profile.program,
        catalog_year=profile.catalog_year,
    )

    # Related data
    tags = course.tags.all().order_by("name")
    prereqs = course.prerequisites.all().order_by("subject", "level", "code")
    groups = course.prereq_groups.all().prefetch_related("options").order_by("name") if hasattr(course, "prereq_groups") else []
    offered = course.offered_in.all().order_by("year", "season") if hasattr(course, "offered_in") else []

    ctx = {
        "c": course,
        "tags": tags,
        "prereqs": prereqs,
        "groups": groups,
        "offered": offered,
        "profile": profile,
    }

    if _wants_partial(request):
        return render(request, "course_detail_modal.html", ctx)

    return render(request, "course_detail.html", ctx)
