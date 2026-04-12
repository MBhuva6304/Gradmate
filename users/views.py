# users/views.py
from __future__ import annotations

import io
import os
import random
import re
from .requirement_engine import build_course_counting_map
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.hashers import make_password, check_password
from django.db import transaction
from django.db.models import Sum, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.core.paginator import Paginator

from .models import (
    Course,
    StudentProfile,
    Term,
    TermPlan,
    ProgramRequirement,
    CompletedClass,
    InProgressClass,
    PlannedCourse,
    DPRUpload,
)
from .forms import (
    SignUpForm,
    OTPForm,
    ProfileSettingsForm,
    ProfileSetupForm,
    DPRUploadForm,
)

EMAIL_BACKEND_PATH = "users.backends.EmailBackend"

HIDDEN_AUDIT_RULE_NAMES = {
    "Need One Life Science Pair",
    "Need One Physical Science Pair",
}

def _gen6() -> str:
    return f"{random.randint(0, 999999):06d}"


def _parse_codes(raw: str) -> list[str]:
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
    return (
        request.GET.get("partial") == "1"
        or request.headers.get("X-Requested-With") in {"fetch", "XMLHttpRequest"}
    )


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


# ---------------------------
# PDF parsing helpers
# ---------------------------

# 23SP or 2023SP
_TERM_RE = re.compile(r"^(?:\d{2}|\d{4})(SP|SU|FA)$", re.I)

# COMP110, COMP-110, COMP 110, COMP-110L, etc.
_COURSE_TOKEN_RE = re.compile(r"^(?P<subj>[A-Z]{2,6})[-\s]?(?P<num>\d{1,4}[A-Z]?)$", re.I)


def _extract_pdf_text(uploaded_file) -> str:
    """
    Extract all text from a PDF using PyPDF2.
    """
    try:
        from PyPDF2 import PdfReader
    except Exception as e:
        raise RuntimeError("PyPDF2 is not installed. Please install: pip install pypdf2") from e

    data = uploaded_file.read()
    uploaded_file.seek(0)

    reader = PdfReader(io.BytesIO(data))
    chunks = []
    for page in reader.pages:
        t = page.extract_text() or ""
        chunks.append(t)
    return "\n".join(chunks)


def _norm_code(s: str) -> str:
    # "COMP-110L" -> "COMP110L", "COMP 110" -> "COMP110"
    return re.sub(r"[^A-Z0-9]+", "", (s or "").strip().upper())


def _parse_dpr_lines(text: str) -> tuple[list[dict], list[dict]]:
    completed: list[dict] = []
    inprog: list[dict] = []

    if not text:
        return completed, inprog

    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)

    grade_tokens = r"A\+|A-|A|B\+|B-|B|C\+|C-|C|D\+|D-|D|F|P|CR|IP"

    pattern = re.compile(
        rf"(?P<term>(?:\d{{2}}|\d{{4}})(?:SP|SU|FA))\s+"
        rf"(?P<subject>[A-Z]{{1,4}}(?:\s+[A-Z]{{1,3}})?)\s+"
        rf"(?P<number>\d{{1,4}}[A-Z]?)\s+"
        rf"(?P<units>\d+(?:\.\d+)?)\s*"
        rf"(?P<grade>{grade_tokens})\b",
        re.I,
    )

    for m in pattern.finditer(text):
        row = {
            "term": m.group("term").upper(),
            "subject": re.sub(r"\s+", "", m.group("subject").upper()),
            "number": m.group("number").upper(),
            "units": float(m.group("units")),
        }
        status = m.group("grade").upper()

        if status == "IP":
            inprog.append(row)
        else:
            row["grade"] = status
            completed.append(row)

    return completed, inprog


# ---------------------------
# Auth / signup etc
# ---------------------------

def signup(request):
    if request.method == "POST":
        form = SignUpForm(request.POST)
        if form.is_valid():
            user = form.save(commit=False)
            user.is_active = False  
            user.save()

            profile, _ = StudentProfile.objects.update_or_create(
                user=user,
                defaults={
                    "program": form.cleaned_data["program"],
                    "catalog_year": int(form.cleaned_data["catalog_year"]),
                },
            )

            codes = _parse_codes(form.cleaned_data.get("completed_codes", ""))
            profile.completed_codes = ", ".join(codes) if codes else ""
            profile.save(update_fields=["completed_codes"])

            code = _gen6()
            from .models import EmailOTP
            EmailOTP.create_for_user(user, make_password(code), purpose="VERIFY")

            from django.core.mail import send_mail
            send_mail(
                subject="Verify your email",
                message=f"Your verification code is: {code}\nThis code expires in 10 minutes.",
                from_email=None,
                recipient_list=[user.email],
                fail_silently=False,
            )

            request.session["verify_email"] = user.email
            messages.success(request, "We emailed you a 6-digit code. Enter it to verify your account.")
            return redirect("verify_signup")
        else:
            messages.error(request, "Please fix the errors below.")
    else:
        form = SignUpForm()

    return render(request, "signup.html", {"form": form})


def verify_signup(request):
    email = request.session.get("verify_email")
    if not email:
        messages.error(request, "Session expired. Please start again.")
        return redirect("signup")

    User = get_user_model()

    if request.method == "GET" and request.GET.get("resend") == "1":
        try:
            user = User.objects.get(email__iexact=email)
        except User.DoesNotExist:
            messages.error(request, "No user found for this email.")
            return redirect("signup")

        code = _gen6()
        from .models import EmailOTP
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

            from .models import EmailOTP
            otp = (
                EmailOTP.objects
                .filter(user=user, purpose="VERIFY", is_used=False)
                .order_by("-created_at")
                .first()
            )
            if not otp or not otp.is_valid() or not check_password(code, otp.code_hash):
                messages.error(request, "Invalid or expired code.")
                return render(request, "verify_email.html", {"form": form, "email": email})

            user.is_active = True
            user.save(update_fields=["is_active"])
            otp.mark_used()

            backend = EMAIL_BACKEND_PATH
            if backend not in settings.AUTHENTICATION_BACKENDS:
                backend = settings.AUTHENTICATION_BACKENDS[0]
            login(request, user, backend=backend)

            request.session.pop("verify_email", None)
            return redirect("dashboard")
    else:
        form = OTPForm()

    return render(request, "verify_email.html", {"form": form, "email": email})


@login_required
def setup_profile(request):
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

    completed_courses = (
        CompletedClass.objects
        .filter(profile=profile)
        .select_related("course")
        .order_by("course__subject", "course__code")
    )

    if request.method == "POST":
        if submit_kind == "profile" and profile_form.is_valid():
            profile_form.save(request.user, profile)
            messages.success(request, "Profile updated.")
            return redirect("settings")

        if submit_kind == "password" and pwd_form.is_valid():
            user = pwd_form.save()
            update_session_auth_hash(request, user)
            messages.success(request, "Password changed.")
            return redirect("settings")

    dpr_history = (
        DPRUpload.objects
        .filter(user=request.user)
        .order_by("-uploaded_at")
    )

    for dpr in dpr_history:
        dpr.display_name = os.path.basename(dpr.file.name or "")

    return render(request, "settings.html", {
        "profile_form": profile_form,
        "pwd_form": pwd_form,
        "profile": profile,
        "completed_courses": completed_courses,
        "dpr_history": dpr_history,
    })

@profile_required
def clear_audit_data(request):
    if request.method != "POST":
        return redirect("settings")

    profile = request.profile

    CompletedClass.objects.filter(profile=profile).delete()
    InProgressClass.objects.filter(profile=profile).delete()
    profile.completed_codes = ""
    profile.save(update_fields=["completed_codes"])

    messages.success(request, "Current audit data was cleared. Your DPR history files were kept.")
    return redirect("settings")

@profile_required
def dashboard(request):
    profile = StudentProfile.objects.select_related("user").get(user=request.user)

    req_missing = not ProgramRequirement.objects.filter(
        program=profile.program,
        catalog_year=profile.catalog_year,
    ).exists()

    next_term = Term.from_date().next()

    next_term_plan = (
        profile.term_plans
        .filter(term=next_term)
        .prefetch_related("planned_courses__course")
        .first()
    )

    if next_term_plan and next_term_plan.planned_courses.exists():
        next_list = [
            pc.course
            for pc in next_term_plan.planned_courses.select_related("course").all()
            if pc.course
        ]
        next_credits = sum(float(c.credits or 0) for c in next_list)
        recommendation_details = []
    else:
        next_list, next_credits, next_term, recommendation_details = profile.recommend_next_term()

    planned_codes = profile.planned_course_codes()

    remaining = [
        c for c in profile.remaining_required_courses()
        if (c.code or "").upper() not in planned_codes
    ]

    grad_term, remaining_credits, completed_credits, target_credits = profile.approximate_graduation_term()
    effective_target_credits = int(profile.effective_target_credits(include_planned=False))
    group_progress_raw = profile.requirement_group_progress_with_planned()
    planned_credits = int(profile.total_planned_units())
    remaining_after_plan = int(profile.remaining_credits_after_plan())

    in_progress_credits = sum(
        float(c.course.credits or 0)
        for c in InProgressClass.objects.filter(profile=profile).select_related("course")
        if c.course
    )

    total_now = int(float(completed_credits) + float(in_progress_credits))
    total_after_plan = int(float(completed_credits) + float(in_progress_credits) + float(planned_credits))

    if remaining_after_plan <= 0:
        effective_target_with_plan = total_after_plan
    else:
        effective_target_with_plan = total_after_plan + remaining_after_plan

    ring_total = max(1, effective_target_with_plan)

    current_done_pct = min(
        100,
        int((float(completed_credits) / ring_total) * 100)
    )
    current_ip_pct = min(
        100,
        int(((float(completed_credits) + float(in_progress_credits)) / ring_total) * 100)
    )
    current_plan_pct = min(
        100,
        int(((float(completed_credits) + float(in_progress_credits) + float(planned_credits)) / ring_total) * 100)
    )

    ge_mapping = {
        "A1 Oral Communication": "A1",
        "A2 Written Communication": "A2",
        "A3 Critical Thinking": "A3",
        "B1 Physical Science": "B1",
        "B2 Life Science": "B2",
        "B3 Laboratory Activity": "B3",
        "B4 Math / Quantitative Reasoning": "B4",
        "B5 Upper Division Scientific Inquiry": "B5",
        "C1 Arts": "C1",
        "C2 Humanities": "C2",
        "C3 American History": "C3",
        "D1 Social Sciences": "D1",
        "D3 Constitution of the U.S.": "D3",
        "D4 California State and Local Government": "D4",
        "E Lifelong Learning": "E",
        "F Comparative Cultural Studies": "F",
        "Ethnic Studies": "ES",
        "Basic Skills Information Competence": "BSIC",
        "Subject Explorations Information Competence": "SEIC",
        "General Education Upper Division": "UDGE",
    }

    major_mapping = {
        "Lower Division Algorithms & Programming": "LDAP",
        "Lower Division Major": "LDM",
        "Lower Division Life Science": "LDLS",
        "Lower Division Physical Science": "LDPS",
        "Computer Science Upper Division Core": "UDC",
        "Senior Electives": "SE",
    }

    ge_progress = []
    major_progress = []

    for g in group_progress_raw:
        item = dict(g)

        if item.get("display_mode") == "course_equivalent":
            item["completed"] = item.get("display_completed", item.get("completed", 0))
            item["in_progress"] = item.get("display_in_progress", item.get("in_progress", 0))
            item["planned"] = item.get("display_planned", item.get("planned", 0))
            item["min_required"] = item.get("display_required", item.get("min_required", 0))

        for field in ["completed", "in_progress", "planned", "min_required"]:
            val = float(item.get(field, 0) or 0)
            item[field] = int(val) if val.is_integer() else round(val, 2)

        if g["name"] in ge_mapping:
            item["short"] = ge_mapping[g["name"]]
            ge_progress.append(item)
        elif g["name"] in major_mapping:
            item["short"] = major_mapping[g["name"]]
            major_progress.append(item)

    ge_order = {
        "A1": 1,
        "A2": 2,
        "A3": 3,
        "B1": 4,
        "B2": 5,
        "B3": 6,
        "B4": 7,
        "B5": 8,
        "C1": 9,
        "C2": 10,
        "C3": 11,
        "D1": 12,
        "D3": 13,
        "D4": 14,
        "E": 15,
        "F": 16,
        "UDGE": 17,
        "BSIC": 18,
        "SEIC": 19,
        "ES": 20,
    }

    major_order = {
        "LDAP": 1,
        "LDM": 2,
        "LDLS": 3,
        "LDPS": 4,
        "UDC": 5,
        "SE": 6,
    }

    ge_progress.sort(key=lambda g: ge_order.get(g["short"], 999))
    major_progress.sort(key=lambda g: major_order.get(g["short"], 999))

    incomplete_block_count = sum(1 for g in (ge_progress + major_progress) if not g.get("done"))

    remaining_for_chart = max(
        0,
        int(
            effective_target_with_plan
            - completed_credits
            - float(in_progress_credits)
            - float(planned_credits)
        )
    )

    denom = effective_target_with_plan or 1
    progress_percent = min(
        100,
        int(
            (
                completed_credits
                + float(in_progress_credits)
                + float(planned_credits)
            ) / denom * 100
        )
    )

    show_upper_alert = any(
        not g.get("done") for g in major_progress
        if g.get("short") in {"UDC", "SE"}
    )

    dashboard_first_term_plan = (
        profile.term_plans
        .select_related("term")
        .prefetch_related("planned_courses__course")
        .order_by("position")
        .first()
    )

    dashboard_plan_courses = []
    dashboard_plan_units = 0

    if dashboard_first_term_plan:
        dashboard_plan_courses = [
            pc for pc in dashboard_first_term_plan.planned_courses.select_related("course").all()
            if pc.course
        ]
        dashboard_plan_units = int(sum(float(pc.course.credits or 0) for pc in dashboard_plan_courses))

    req = profile.get_requirement()

    all_for_counting = list(remaining) + list(next_list)
    all_for_counting += [pc.course for pc in dashboard_plan_courses if pc.course]

    if req and all_for_counting:
        count_map = build_course_counting_map(all_for_counting, req)
    else:
        count_map = {}

    for c in next_list:
        info = count_map.get(c.id, {})
        c.applied_blocks = info.get("applied_names", [])
        c.eligible_blocks = info.get("eligible_names", [])

    for c in remaining:
        info = count_map.get(c.id, {})
        c.applied_blocks = info.get("applied_names", [])
        c.eligible_blocks = info.get("eligible_names", [])

    for pc in dashboard_plan_courses:
        if not pc.course:
            continue
        info = count_map.get(pc.course.id, {})
        pc.course.applied_blocks = info.get("applied_names", [])
        pc.course.eligible_blocks = info.get("eligible_names", [])

    dashboard_remaining = []
    if remaining:
        dashboard_remaining = list(remaining[:8])

    dashboard_alerts = []

    if show_upper_alert:
        dashboard_alerts.append({
            "icon": "⚠️",
            "title": "Upper-division planning check",
            "message": "Make sure your remaining plan includes enough upper-division units.",
            "level": "warning",
        })

    if remaining_after_plan > 0:
        dashboard_alerts.append({
            "icon": "🗓",
            "title": "More planning needed",
            "message": f"You still have {remaining_after_plan} unit(s) left after your saved plan.",
            "level": "info",
        })
    else:
        dashboard_alerts.append({
            "icon": "✅",
            "title": "Plan looks complete",
            "message": "Your saved degree plan currently covers all remaining required units.",
            "level": "success",
        })

    if dashboard_plan_courses:
        dashboard_alerts.append({
            "icon": "📚",
            "title": f"{dashboard_first_term_plan.term} plan loaded",
            "message": f"You have {len(dashboard_plan_courses)} planned course(s) totaling {dashboard_plan_units} unit(s).",
            "level": "info",
        })
    else:
        dashboard_alerts.append({
            "icon": "📝",
            "title": "No saved courses in next term",
            "message": f"You do not have any saved planned courses in {next_term}.",
            "level": "info",
        })

    context = {
        "profile": profile,
        "current_term": Term.from_date(),
        "grad_term": grad_term,
        "completed_credits": int(completed_credits),
        "in_progress_credits": int(float(in_progress_credits)),
        "remaining_credits": remaining_for_chart,
        "target_credits": int(target_credits),
        "effective_target_credits": effective_target_credits,
        "effective_target_with_plan": effective_target_with_plan,
        "progress_percent": progress_percent,
        "ge_progress": ge_progress,
        "major_progress": major_progress,
        "ge_done_count": sum(1 for g in ge_progress if g.get("done")),
        "ge_total_count": len(ge_progress),
        "major_done_count": sum(1 for g in major_progress if g.get("done")),
        "major_total_count": len(major_progress),
        "remaining": remaining,
        "next_list": next_list,
        "next_credits": int(next_credits),
        "next_term": next_term,
        "req_missing": req_missing,
        "incomplete_block_count": incomplete_block_count,
        "planned_credits": planned_credits,
        "remaining_after_plan": remaining_after_plan,
        "current_done_pct": current_done_pct,
        "current_ip_pct": current_ip_pct,
        "current_plan_pct": current_plan_pct,
        "show_upper_alert": show_upper_alert,
        "dashboard_alerts": dashboard_alerts,
        "dashboard_remaining": dashboard_remaining,
        "dashboard_plan_courses": dashboard_plan_courses,
        "dashboard_plan_units": dashboard_plan_units,
        "dashboard_plan_term": dashboard_first_term_plan.term if dashboard_first_term_plan else next_term,
        "dashboard_plan_is_saved": bool(dashboard_plan_courses),
    }

    return render(request, "dashboard.html", context)


@profile_required
@require_POST
def remove_term_plan(request, term_plan_id):
    profile = request.profile

    term_plan = get_object_or_404(
        TermPlan,
        id=term_plan_id,
        profile=profile,
    )

    term_label = str(term_plan.term)
    term_plan.delete()

    remaining_terms = profile.term_plans.order_by("position", "term__year", "term__season")
    for idx, tp in enumerate(remaining_terms, start=1):
        if tp.position != idx:
            tp.position = idx
            tp.save(update_fields=["position"])

    messages.success(request, f"{term_label} was removed from your degree plan.")
    return redirect("degree_plan")

@profile_required
def degree_plan(request):
    profile = request.profile

    req_missing = not ProgramRequirement.objects.filter(
        program=profile.program,
        catalog_year=profile.catalog_year,
    ).exists()

    remaining = list(profile.remaining_required_courses())
    recommended_courses, recommended_units, next_term, recommendation_details = profile.recommend_next_term()
    next_list = list(recommended_courses)

    grad_term, _, completed_credits, target_credits = profile.approximate_graduation_term()
    effective_target_credits = int(profile.effective_target_credits(include_planned=False))
    group_progress_raw = profile.requirement_group_progress_with_planned()

    ge_mapping = {
        "A1 Oral Communication": "A1",
        "A2 Written Communication": "A2",
        "A3 Critical Thinking": "A3",
        "B1 Physical Science": "B1",
        "B2 Life Science": "B2",
        "B3 Laboratory Activity": "B3",
        "B4 Math / Quantitative Reasoning": "B4",
        "B5 Upper Division Scientific Inquiry": "B5",
        "C1 Arts": "C1",
        "C2 Humanities": "C2",
        "C3 American History": "C3",
        "D1 Social Sciences": "D1",
        "D3 Constitution of the U.S.": "D3",
        "D4 California State and Local Government": "D4",
        "E Lifelong Learning": "E",
        "F Comparative Cultural Studies": "F",
        "Ethnic Studies": "ES",
        "Basic Skills Information Competence": "BSIC",
        "Subject Explorations Information Competence": "SEIC",
        "General Education Upper Division": "UDGE",
    }

    major_mapping = {
        "Lower Division Algorithms & Programming": "LDAP",
        "Lower Division Major": "LDM",
        "Lower Division Life Science": "LDLS",
        "Lower Division Physical Science": "LDPS",
        "Computer Science Upper Division Core": "UDC",
        "Senior Electives": "SE",
    }

    ge_order = {
        "A1": 1, "A2": 2, "A3": 3,
        "B1": 4, "B2": 5, "B3": 6, "B4": 7, "B5": 8,
        "C1": 9, "C2": 10, "C3": 11,
        "D1": 12, "D3": 13, "D4": 14,
        "E": 15, "F": 16,
        "UDGE": 17, "BSIC": 18, "SEIC": 19, "ES": 20,
    }

    major_order = {
        "LDAP": 1,
        "LDM": 2,
        "LDLS": 3,
        "LDPS": 4,
        "UDC": 5,
        "SE": 6,
    }

    ge_progress = []
    major_progress = []

    for g in group_progress_raw:
        item = dict(g)
        if g["name"] in ge_mapping:
            item["short"] = ge_mapping[g["name"]]
            ge_progress.append(item)
        elif g["name"] in major_mapping:
            item["short"] = major_mapping[g["name"]]
            major_progress.append(item)

    ge_progress.sort(key=lambda g: ge_order.get(g["short"], 999))
    major_progress.sort(key=lambda g: major_order.get(g["short"], 999))

    in_progress_qs = InProgressClass.objects.filter(profile=profile).select_related("course")
    in_progress_credits = sum(
        float(row.course.credits or 0)
        for row in in_progress_qs
        if row.course
    )

    total_planned_units = int(profile.total_planned_units())

    total_now = int(float(completed_credits) + float(in_progress_credits))
    total_after_plan = int(float(completed_credits) + float(in_progress_credits) + float(total_planned_units))

    remaining_credits = max(0, effective_target_credits - total_now)
    remaining_after_plan = int(profile.remaining_credits_after_plan())
    planned_warning_count = profile.planned_warning_count()

    if remaining_after_plan <= 0:
        effective_target_with_plan = total_after_plan
    else:
        effective_target_with_plan = total_after_plan + remaining_after_plan

    term_plans = profile.get_or_create_future_term_plans(count=4)
    saved_terms = []

    req = profile.get_requirement()

    planned_courses_flat = []
    for term_plan in term_plans:
        planned_courses = list(
            term_plan.planned_courses.select_related("course").all()
        )
        planned_courses_flat.extend([pc.course for pc in planned_courses if pc.course])

        units = int(profile.term_plan_total_units(term_plan))
        
        saved_terms.append({
            "id": term_plan.id,
            "term_plan": term_plan,
            "term": term_plan.term,
            "position": term_plan.position,
            "notes": term_plan.notes,
            "courses": planned_courses,
            "units": units,
            "is_over_limit": units > int(profile.max_credits_next_term or 15),
        })
    
    all_sections_done_with_plan = profile.all_sections_satisfied(include_planned=True)

    if all_sections_done_with_plan:
        non_empty_terms = [sem for sem in saved_terms if sem["courses"]]
        empty_terms = [sem for sem in saved_terms if not sem["courses"]]
        saved_terms = non_empty_terms + (empty_terms[:1] if empty_terms else [])

    semester_choices = [
        {
            "id": sem["id"],
            "label": str(sem["term"]),
        }
        for sem in saved_terms
    ]

    all_for_counting = []
    all_for_counting.extend(remaining)
    all_for_counting.extend(next_list)
    all_for_counting.extend(planned_courses_flat)

    if req and all_for_counting:
        count_map = build_course_counting_map(all_for_counting, req)
    else:
        count_map = {}

    for sem in saved_terms:
        for pc in sem["courses"]:
            if not pc.course:
                continue

            info = count_map.get(pc.course.id, {})
            pc.applied_blocks = info.get("applied_names", [])
            pc.eligible_blocks = info.get("eligible_names", [])

            pc.is_potentially_unnecessary = not bool(pc.applied_blocks)
    for c in next_list:
        info = count_map.get(c.id, {})
        c.applied_blocks = info.get("applied_names", [])
        c.eligible_blocks = info.get("eligible_names", [])

    for c in remaining:
        info = count_map.get(c.id, {})
        c.applied_blocks = info.get("applied_names", [])
        c.eligible_blocks = info.get("eligible_names", [])

    next_codes = {(c.code or "").upper() for c in next_list if c.code}
    planned_codes = profile.planned_course_codes()

    backup_list = []
    locked_list = []
    alternative_options = []
    if all_sections_done_with_plan:
        alternative_options = profile.alternative_courses_for_planned_sections(
            planned_courses_flat,
            limit=12,
        )


    for c in remaining:
        code = (c.code or "").upper()

        if code in next_codes:
            continue

        if code in planned_codes:
            continue

        can_fit_somewhere = False
        last_reason = "Not ready for planning yet."

        for sem in saved_terms:
            ok, reason = profile.can_add_course_to_term_plan(c, sem["term_plan"])
            if ok:
                can_fit_somewhere = True
                last_reason = ""
                break

            if reason:
                last_reason = reason

        c.lock_reason = last_reason

        if can_fit_somewhere:
            backup_list.append(c)
        else:
            locked_list.append(c)
    semester_recommendations = []
    for sem in saved_terms:
        term_plan = sem["term_plan"]

        if all_sections_done_with_plan:
            scored_courses = []
        else:
            scored_courses = profile.recommend_for_term_plan(
                term_plan,
                remaining_courses=remaining,
                limit=5,
            )

        semester_recommendations.append({
            "term_plan_id": sem["id"],
            "term_label": str(sem["term"]),
            "courses": [item["course"] for item in scored_courses],
            "details": scored_courses,
        })

    sections_left = []
    for g in major_progress + ge_progress:
        if g.get("display_mode") == "course_equivalent":
            completed = float(g.get("display_completed", g.get("completed", 0)))
            in_progress = float(g.get("display_in_progress", g.get("in_progress", 0)))
            planned = float(g.get("display_planned", g.get("planned", 0)))
            required = float(g.get("display_required", g.get("min_required", 0)))
            remaining_count = max(
                0.0,
                float(g.get("display_remaining", required - completed - in_progress - planned))
            )
        else:
            completed = float(g.get("completed", 0))
            in_progress = float(g.get("in_progress", 0))
            planned = float(g.get("planned", 0))
            required = float(g.get("min_required", 0))
            remaining_count = max(0.0, required - completed - in_progress - planned)

        if completed >= required and in_progress == 0 and planned == 0:
            continue

        if planned > 0:
            state = "Planned"
            badge_class = "planned"
        elif in_progress > 0 and remaining_count == 0:
            state = "Finishing"
            badge_class = "ip"
        elif in_progress > 0:
            state = "In Progress"
            badge_class = "ip"
        elif completed > 0:
            state = "Remaining"
            badge_class = "neutral"
        else:
            state = "Not Started"
            badge_class = "warn"

        sections_left.append({
            "name": g.get("name"),
            "short": g.get("short"),
            "required": int(required) if float(required).is_integer() else round(required, 2),
            "completed": int(completed) if float(completed).is_integer() else round(completed, 2),
            "in_progress": int(in_progress) if float(in_progress).is_integer() else round(in_progress, 2),
            "planned": int(planned) if float(planned).is_integer() else round(planned, 2),
            "remaining": int(remaining_count) if float(remaining_count).is_integer() else round(remaining_count, 2),
            "state": state,
            "badge_class": badge_class,
            "total_options": g.get("total_options", 0),
            "display_mode": g.get("display_mode", ""),
        })

    sections_left.sort(key=lambda x: (x["remaining"] == 0, x["name"] or ""))

    show_upper_alert = any(
        not g.get("done") for g in major_progress
        if g.get("short") in {"UDC", "SE"}
    )

    context = {
        "profile": profile,
        "grad_term": grad_term,
        "completed_credits": int(completed_credits),
        "in_progress_credits": int(float(in_progress_credits)),
        "remaining_credits": remaining_credits,
        "target_credits": int(target_credits),
        "effective_target_credits": effective_target_credits,
        "effective_target_with_plan": effective_target_with_plan,
        "next_list": next_list,
        "next_term": next_term,
        "recommendation_details": recommendation_details,
        "next_credits": int(sum(float(c.credits or 0) for c in next_list)),
        "req_missing": req_missing,
        "saved_terms": saved_terms,
        "semester_choices": semester_choices,
        "backup_list": backup_list[:12],
        "locked_list": locked_list[:12],
        "max_credits_next_term": int(profile.max_credits_next_term or 15),
        "show_upper_alert": show_upper_alert,
        "sections_left": sections_left,
        "total_planned_units": total_planned_units,
        "remaining_after_plan": remaining_after_plan,
        "recommended_courses": recommended_courses,
        "recommended_units": recommended_units,
        "semester_recommendations": semester_recommendations,
        "planned_warning_count": planned_warning_count,
        "alternative_options": alternative_options,
        "all_sections_done_with_plan": all_sections_done_with_plan,
    }

    return render(request, "degree_plan.html", context)
@profile_required
def auto_suggest_degree_plan(request):
    if request.method != "POST":
        return redirect("degree_plan")

    profile = request.profile

    term_plans = profile.get_or_create_future_term_plans(count=4)
    if not term_plans:
        messages.error(request, "Could not create future semesters.")
        return redirect("degree_plan")

    first_term = term_plans[0]

    PlannedCourse.objects.filter(term_plan=first_term).delete()

    next_list, _, _, _ = profile.recommend_next_term()

    created = 0
    for idx, course in enumerate(next_list, start=1):
        PlannedCourse.objects.get_or_create(
            term_plan=first_term,
            course=course,
            defaults={
                "position": idx,
                "status": "planned",
            },
        )
        created += 1

    messages.success(request, f"Auto-suggested {created} course(s) into {first_term.term}.")
    return redirect("degree_plan")

@profile_required
@require_POST
def add_suggested_course_to_term(request, term_plan_id, course_id):
    profile = request.profile

    term_plan = get_object_or_404(
        TermPlan,
        id=term_plan_id,
        profile=profile,
    )

    course = get_object_or_404(
        Course,
        id=course_id,
        program=profile.program,
        catalog_year=profile.catalog_year,
    )

    ok, reason = profile.can_add_course_to_term_plan(course, term_plan)
    if not ok:
        messages.error(request, reason)
        return redirect("degree_plan")

    missing_coreqs = profile.missing_corequisites_for_term(course, term_plan)

    for coreq in missing_coreqs:
        ok2, reason2 = profile.can_add_course_to_term_plan(coreq, term_plan)
        if not ok2:
            messages.error(request, f"{course.code} needs corequisite {coreq.code}, but it cannot be added: {reason2}")
            return redirect("degree_plan")

    next_position = (
        term_plan.planned_courses.aggregate(mx=Sum("position")).get("mx") or 0
    ) + 1

    added_codes = []

    PlannedCourse.objects.create(
        term_plan=term_plan,
        course=course,
        position=next_position,
        status="planned",
    )
    added_codes.append(course.code)

    for coreq in missing_coreqs:
        next_position += 1
        PlannedCourse.objects.create(
            term_plan=term_plan,
            course=coreq,
            position=next_position,
            status="planned",
        )
        added_codes.append(coreq.code)

    messages.success(request, f"Added to {term_plan.term}: {', '.join(added_codes)}.")
    return redirect("degree_plan")

@profile_required
@require_POST
def add_planned_course(request, term_plan_id, course_id):
    profile = request.profile

    term_plan = get_object_or_404(
        TermPlan,
        id=term_plan_id,
        profile=profile,
    )

    course = get_object_or_404(
        Course,
        id=course_id,
        program=profile.program,
        catalog_year=profile.catalog_year,
    )

    ok, reason = profile.can_add_course_to_term_plan(course, term_plan)
    if not ok:
        messages.error(request, reason)
        return redirect("degree_plan")

    next_position = (
        term_plan.planned_courses.aggregate(mx=Sum("position")).get("mx") or 0
    ) + 1

    PlannedCourse.objects.create(
        term_plan=term_plan,
        course=course,
        position=next_position,
        status="planned",
    )

    messages.success(request, f"{course.code} added to {term_plan.term}.")
    return redirect("degree_plan")

@profile_required
@require_POST
def add_planned_course_by_form(request, course_id):
    profile = request.profile
    next_url = request.POST.get("next") or request.META.get("HTTP_REFERER") or reverse("degree_plan")

    term_plan_id = request.POST.get("term_plan_id")
    if not term_plan_id:
        messages.error(request, "Please choose a semester.")
        return redirect(next_url)

    term_plan = get_object_or_404(
        TermPlan,
        id=term_plan_id,
        profile=profile,
    )

    course = get_object_or_404(
        Course,
        id=course_id,
        program=profile.program,
        catalog_year=profile.catalog_year,
    )

    ok, reason = profile.can_add_course_to_term_plan(course, term_plan)
    if not ok:
        messages.error(request, reason)
        return redirect(next_url)

    missing_coreqs = profile.missing_corequisites_for_term(course, term_plan)

    for coreq in missing_coreqs:
        ok2, reason2 = profile.can_add_course_to_term_plan(coreq, term_plan)
        if not ok2:
            messages.error(
                request,
                f"{course.code} needs corequisite {coreq.code}, but it cannot be added: {reason2}"
            )
            return redirect(next_url)

    next_position = (
        term_plan.planned_courses.aggregate(mx=Sum("position")).get("mx") or 0
    ) + 1

    added_codes = []

    PlannedCourse.objects.create(
        term_plan=term_plan,
        course=course,
        position=next_position,
        status="planned",
    )
    added_codes.append(course.code)

    for coreq in missing_coreqs:
        next_position += 1
        PlannedCourse.objects.create(
            term_plan=term_plan,
            course=coreq,
            position=next_position,
            status="planned",
        )
        added_codes.append(coreq.code)

    if not profile.course_still_useful_for_requirements(course):
        messages.warning(
            request,
            f"{course.code} was added, but it may not satisfy any remaining requirement."
        )
    else:
        messages.success(request, f"Added to {term_plan.term}: {', '.join(added_codes)}.")

    return redirect(next_url)
@profile_required
@require_POST
def remove_planned_course(request, planned_course_id):
    profile = request.profile

    planned_course = get_object_or_404(
        PlannedCourse.objects.select_related("term_plan", "course"),
        id=planned_course_id,
        term_plan__profile=profile,
    )

    course_code = planned_course.course.code
    term_label = str(planned_course.term_plan.term)
    planned_course.delete()

    messages.success(request, f"{course_code} removed from {term_label}.")

    next_url = request.POST.get("next") or request.META.get("HTTP_REFERER") or reverse("degree_plan")
    return redirect(next_url)

@profile_required
@require_POST
def move_planned_course(request, planned_course_id):
    profile = request.profile

    target_term_plan_id = request.POST.get("target_term_plan_id")
    if not target_term_plan_id:
        messages.error(request, "Please choose a target semester.")
        return redirect("degree_plan")

    planned_course = get_object_or_404(
        PlannedCourse.objects.select_related("term_plan", "course"),
        id=planned_course_id,
        term_plan__profile=profile,
    )

    target_term_plan = get_object_or_404(
        TermPlan,
        id=target_term_plan_id,
        profile=profile,
    )

    ok, reason = profile.can_move_planned_course(planned_course, target_term_plan)
    if not ok:
        messages.error(request, reason)
        return redirect("degree_plan")

    old_term = str(planned_course.term_plan.term)

    next_position = (
        target_term_plan.planned_courses.aggregate(mx=Sum("position")).get("mx") or 0
    ) + 1

    source_term_plan = planned_course.term_plan
    planned_course.term_plan = target_term_plan
    planned_course.position = next_position
    planned_course.save(update_fields=["term_plan", "position"])

    profile.renumber_term_plan_positions(source_term_plan)
    profile.renumber_term_plan_positions(target_term_plan)

    messages.success(
        request,
        f"{planned_course.course.code} moved from {old_term} to {target_term_plan.term}."
    )
    return redirect("degree_plan")

@profile_required
@require_POST
def save_term_notes(request, term_plan_id):
    profile = request.profile

    term_plan = get_object_or_404(
        TermPlan,
        id=term_plan_id,
        profile=profile,
    )

    notes = (request.POST.get("notes") or "").strip()
    max_len = 500
    if len(notes) > max_len:
        messages.error(request, f"Notes must be {max_len} characters or fewer.")
        return redirect("degree_plan")

    term_plan.notes = notes
    term_plan.save(update_fields=["notes"])

    messages.success(request, f"Notes saved for {term_plan.term}.")
    return redirect("degree_plan")
@profile_required
@require_POST
def add_future_term(request):
    profile = request.profile

    next_term = profile.get_next_unused_term()
    next_position = (profile.term_plans.aggregate(mx=Sum("position")).get("mx") or 0) + 1

    TermPlan.objects.create(
        profile=profile,
        term=next_term,
        position=next_position,
    )

    messages.success(request, f"{next_term} added to your degree plan.")
    return redirect("degree_plan")

@profile_required
@require_POST
def clear_degree_plan(request):
    profile = request.profile

    PlannedCourse.objects.filter(
        term_plan__profile=profile
    ).delete()

    messages.success(request, "Your saved degree plan was cleared.")
    return redirect("degree_plan")

def _safe_float(v, default=0.0):
    try:
        return float(v or 0)
    except Exception:
        return float(default)


def _safe_int(v, default=0):
    try:
        return int(v or 0)
    except Exception:
        return int(default)


def _status_meta(done: bool, completed: float, in_progress: float, required: float, planned: float = 0):
    if done or (required > 0 and completed >= required):
        return {
            "label": "Completed",
            "tag_class": "ok",
            "icon": "✔",
        }
    if in_progress > 0:
        return {
            "label": "In Progress",
            "tag_class": "ip",
            "icon": "●",
        }
    if planned > 0:
        return {
            "label": "Planned",
            "tag_class": "planned",
            "icon": "○",
        }
    return {
        "label": "Missing",
        "tag_class": "bad",
        "icon": "✖",
    }
def _short_group_name(name: str) -> str:
    mapping = {
        "A1 Oral Communication": "A1",
        "A2 Written Communication": "A2",
        "A3 Critical Thinking": "A3",
        "B1 Physical Science": "B1",
        "B2 Life Science": "B2",
        "B3 Laboratory Activity": "B3",
        "B4 Math / Quantitative Reasoning": "B4",
        "B5 Upper Division Scientific Inquiry": "B5",
        "C1 Arts": "C1",
        "C2 Humanities": "C2",
        "C3 American History": "C3",
        "D1 Social Sciences": "D1",
        "D3 Constitution of the U.S.": "D3",
        "D4 California State and Local Government": "D4",
        "E Lifelong Learning": "E",
        "F Comparative Cultural Studies": "F",
        "Ethnic Studies": "ES",
        "Basic Skills Information Competence": "BSIC",
        "Subject Explorations Information Competence": "SEIC",

        "Lower Division Algorithms & Programming": "LDAP",
        "Lower Division Major": "LDM",
        "Lower Division Life Science": "LDLS",
        "Lower Division Physical Science": "LDPS",
        "Computer Science Upper Division Core": "UDC",
        "Senior Electives": "SE",

        "General Education Upper Division": "UDGE",
        "C Upper Division Arts and Humanities": "UD-C",
        "D Upper Division Social Sciences": "UD-D",
        "F Upper Division Comparative Cultural Studies": "UD-F",
        "F Upper Division Comparative Cultural Studies 2": "UD-F2",
    }
    return mapping.get(name, name)


def _bucket_title(name: str) -> str:
    n = (name or "").strip()

    if n in {
        "A1 Oral Communication",
        "A2 Written Communication",
        "A3 Critical Thinking",
        "B1 Physical Science",
        "B2 Life Science",
        "B3 Laboratory Activity",
        "B4 Math / Quantitative Reasoning",
        "B5 Upper Division Scientific Inquiry",
        "C1 Arts",
        "C2 Humanities",
        "C3 American History",
        "D1 Social Sciences",
        "D3 Constitution of the U.S.",
        "D4 California State and Local Government",
        "E Lifelong Learning",
        "F Comparative Cultural Studies",
        "Ethnic Studies",
        "Basic Skills Information Competence",
        "Subject Explorations Information Competence",
        "General Education Upper Division",

        # helper UD blocks
        "C Upper Division Arts and Humanities",
        "D Upper Division Social Sciences",
        "F Upper Division Comparative Cultural Studies",
        "F Upper Division Comparative Cultural Studies 2",
    }:
        return "General Education"

    if n in {
        "Lower Division Algorithms & Programming",
        "Lower Division Major",
        "Lower Division Life Science",
        "Lower Division Physical Science",
        "Computer Science Upper Division Core",
        "Senior Electives",
    }:
        return "Computer Science Grade and Residency Requirements"

    if n == "Additional Courses Which Count Toward Total Units Required for a Degree":
        return "Additional Courses Which Count Toward Total Units Required for a Degree"

    if n == "These courses do not provide unit credit toward a bachelor degree":
        return "These courses do not provide unit credit toward a bachelor degree"

    return "Other Requirements"

def _collect_block_course_rows(profile, block_names: list[str]):
    completed_courses = list(
        CompletedClass.objects
        .filter(profile=profile)
        .select_related("course")
        .order_by("course__subject", "course__code")
    )

    in_progress_courses = list(
        InProgressClass.objects
        .filter(profile=profile)
        .select_related("course")
        .order_by("course__subject", "course__code")
    )

    planned_courses = list(
        PlannedCourse.objects
        .filter(term_plan__profile=profile)
        .select_related("course", "term_plan")
        .order_by("term_plan__position", "position")
    )

    req = profile.get_requirement()
    if not req:
        return []

    label_map = {
        "B5 Upper Division Scientific Inquiry": "Section B5 Upper Division Scientific Inquiry",
        "C Upper Division Arts and Humanities": "Section C Upper Division Arts and Humanities",
        "D Upper Division Social Sciences": "Section D Upper Division Social Sciences",
        "F Upper Division Comparative Cultural Studies": "Section F Upper Division Comparative Cultural Studies",
        "F Upper Division Comparative Cultural Studies 2": "Section F Upper Division Comparative Cultural Studies",
    }

    rows = []
    seen_codes = set()

    for block_name in block_names:
        block = req.blocks.filter(name=block_name).first()
        if not block:
            continue

        section_label = label_map.get(block_name, block_name)

        block_course_codes = {
            (c.code or "").upper()
            for c in block.courses.all()
            if c.code
        }

        for cc in completed_courses:
            if cc.course and (cc.course.code or "").upper() in block_course_codes:
                code = (cc.course.code or "").upper()
                if code not in seen_codes:
                    rows.append({
                        "code": cc.course.code,
                        "title": f"{section_label}: {cc.course.title}",
                        "units": _safe_float(cc.course.credits),
                        "grade": "Done",
                        "status": "ok",
                    })
                    seen_codes.add(code)

        for ic in in_progress_courses:
            if ic.course and (ic.course.code or "").upper() in block_course_codes:
                code = (ic.course.code or "").upper()
                if code not in seen_codes:
                    rows.append({
                        "code": ic.course.code,
                        "title": f"{section_label}: {ic.course.title}",
                        "units": _safe_float(ic.course.credits),
                        "grade": "IP",
                        "status": "ip",
                    })
                    seen_codes.add(code)

        for pc in planned_courses:
            if pc.course and (pc.course.code or "").upper() in block_course_codes:
                code = (pc.course.code or "").upper()
                if code not in seen_codes:
                    rows.append({
                        "code": pc.course.code,
                        "title": f"{section_label}: {pc.course.title}",
                        "units": _safe_float(pc.course.credits),
                        "grade": "Planned",
                        "status": "planned",
                    })
                    seen_codes.add(code)

    return rows
def _build_udge_helper_options(
    profile,
    assigned_completed_codes: set[str],
    assigned_ip_codes: set[str],
    assigned_planned_codes: set[str],
):
    option_defs = [
        {
            "name": "Option 1",
            "label": "B5 + C + D",
            "parts": [
                ("B5 Upper Division Scientific Inquiry", "B5"),
                ("C Upper Division Arts and Humanities", "C"),
                ("D Upper Division Social Sciences", "D"),
            ],
        },
        {
            "name": "Option 2",
            "label": "B5 + C + F",
            "parts": [
                ("B5 Upper Division Scientific Inquiry", "B5"),
                ("C Upper Division Arts and Humanities", "C"),
                ("F Upper Division Comparative Cultural Studies", "F"),
            ],
        },
        {
            "name": "Option 3",
            "label": "B5 + D + F",
            "parts": [
                ("B5 Upper Division Scientific Inquiry", "B5"),
                ("D Upper Division Social Sciences", "D"),
                ("F Upper Division Comparative Cultural Studies", "F"),
            ],
        },
        {
            "name": "Option 4",
            "label": "B5 + F + F",
            "parts": [
                ("B5 Upper Division Scientific Inquiry", "B5"),
                ("F Upper Division Comparative Cultural Studies", "F"),
                ("F Upper Division Comparative Cultural Studies 2", "F"),
            ],
        },
    ]

    req = profile.get_requirement()
    if not req:
        return []

    completed_codes = {(c or "").upper() for c in assigned_completed_codes if c}
    ip_codes = {(c or "").upper() for c in assigned_ip_codes if c}
    planned_codes = {(c or "").upper() for c in assigned_planned_codes if c}

    options = []

    for opt in option_defs:
        part_rows = []
        complete_parts = 0
        ip_parts = 0
        planned_parts = 0

        for block_name, short_label in opt["parts"]:
            block = req.blocks.filter(name=block_name).first()
            block_codes = set()

            if block:
                block_codes = {
                    (c.code or "").upper()
                    for c in block.courses.all()
                    if c.code
                }

            matched_completed = sorted(block_codes & completed_codes)
            matched_ip = sorted(block_codes & ip_codes)
            matched_planned = sorted(block_codes & planned_codes)

            is_done = bool(matched_completed)
            is_ip = (not is_done) and bool(matched_ip)
            is_planned = (not is_done) and (not is_ip) and bool(matched_planned)

            if is_done:
                complete_parts += 1
            elif is_ip:
                ip_parts += 1
            elif is_planned:
                planned_parts += 1

            part_rows.append({
                "block_name": block_name,
                "label": short_label,
                "done": is_done,
                "in_progress": is_ip,
                "planned": is_planned,
                "link_name": block_name,
            })

        options.append({
            "name": opt["name"],
            "label": opt["label"],
            "parts": part_rows,
            "done_count": complete_parts,
            "ip_count": ip_parts,
            "planned_count": planned_parts,
            "is_complete": complete_parts == 3,
        })

    return options
def _build_requirement_audit_data(profile, user):
    completed_courses = list(
        CompletedClass.objects
        .filter(profile=profile)
        .select_related("course")
        .order_by("course__subject", "course__code")
    )

    in_progress_courses = list(
        InProgressClass.objects
        .filter(profile=profile)
        .select_related("course")
        .order_by("course__subject", "course__code")
    )

    planned_courses = list(
        PlannedCourse.objects
        .filter(term_plan__profile=profile)
        .select_related("course", "term_plan", "term_plan__term")
        .order_by("term_plan__position", "position")
    )

    completed_codes_all = {
        (cc.course.code or "").upper()
        for cc in completed_courses
        if cc.course and cc.course.code
    }

    in_progress_codes_all = {
        (ic.course.code or "").upper()
        for ic in in_progress_courses
        if ic.course and ic.course.code
    }

    below_100_completed = [
        cc for cc in completed_courses
        if cc.course and cc.course.numeric_level() is not None and cc.course.numeric_level() < 100
    ]

    below_100_in_progress = [
        ic for ic in in_progress_courses
        if ic.course and ic.course.numeric_level() is not None and ic.course.numeric_level() < 100
    ]

    completed_credits = sum(
        _safe_float(getattr(c.course, "credits", 0))
        for c in completed_courses
        if c.course and c.course.counts_for_total_units()
    )

    in_progress_credits = sum(
        _safe_float(getattr(c.course, "credits", 0))
        for c in in_progress_courses
        if c.course
    )

    target_credits = 120
    if hasattr(profile, "approximate_graduation_term"):
        try:
            _, _, calc_completed, calc_target = profile.approximate_graduation_term()
            if calc_completed is not None:
                completed_credits = _safe_float(calc_completed, completed_credits)
            if calc_target is not None:
                target_credits = _safe_int(calc_target, 120)
        except Exception:
            pass

    remaining_credits = max(0, target_credits - int(completed_credits) - int(in_progress_credits))

    raw_groups = []
    if hasattr(profile, "requirement_group_progress_with_planned"):
        try:
            raw_groups = profile.requirement_group_progress_with_planned() or []
        except Exception:
            raw_groups = []
    elif hasattr(profile, "requirement_group_progress"):
        try:
            raw_groups = profile.requirement_group_progress() or []
        except Exception:
            raw_groups = []

    grouped = {}
    for g in raw_groups:
        bucket = _bucket_title(g.get("name", "Other"))
        grouped.setdefault(bucket, []).append(g)

    bucket_order = [
        "General Education",
        "Computer Science Grade and Residency Requirements",
        "Additional Courses Which Count Toward Total Units Required for a Degree",
        "These courses do not provide unit credit toward a bachelor degree",
        "Other Requirements",
    ]

    audit_groups = []
    used_requirement_codes = set()
    used_display_codes = set()

    def _pick_best_fallback_course(source_rows, block_course_codes):
        """
        Display-only fallback chooser.

        Prefer a course that belongs to fewer requirement blocks, so
        single-section courses like TH-110 are used before shared courses
        like HUM-101 when both could satisfy the current section.

        This does NOT change rules or counting. It only improves which
        course is shown inside a section when assigned_*_codes are missing.
        """
        candidates = []
        req_local = profile.get_requirement()

        for row in source_rows:
            if not getattr(row, "course", None) or not row.course.code:
                continue

            code = (row.course.code or "").upper()
            if code not in block_course_codes:
                continue
            if code in used_display_codes:
                continue

            overlap_count = 9999
            if req_local:
                overlap_count = req_local.blocks.filter(courses__code__iexact=code).distinct().count()

            candidates.append((overlap_count, row))

        if not candidates:
            return None

        candidates.sort(key=lambda x: x[0])
        return candidates[0][1]

    for bucket_name in bucket_order:
        items = grouped.get(bucket_name, [])
        if not items:
            continue

        subsections = []
        group_required = 0.0
        group_completed = 0.0
        group_in_progress = 0.0

        udge_helper_items = []
        has_udge_summary = False

        for item in items:
            raw_name = (item.get("name") or "").strip()


            if raw_name in HIDDEN_AUDIT_RULE_NAMES:
                continue
            if raw_name in {
                "Need One Life Science Pair",
                "Need One Physical Science Pair",
            }:
                continue
            if raw_name == "General Education Upper Division":
                has_udge_summary = True

                required = _safe_float(
                    item.get("min_required")
                    or item.get("required")
                    or item.get("credits_required")
                    or item.get("units_required")
                    or item.get("total_required")
                    or 0
                )

                completed = _safe_float(
                    item.get("completed")
                    or item.get("credits_completed")
                    or item.get("units_completed")
                    or 0
                )

                planned = _safe_float(
                    item.get("planned")
                    or item.get("credits_planned")
                    or item.get("units_planned")
                    or 0
                )

                done = bool(item.get("done", False))

                course_rows = _collect_block_course_rows(profile, [
                    "B5 Upper Division Scientific Inquiry",
                    "C Upper Division Arts and Humanities",
                    "D Upper Division Social Sciences",
                    "F Upper Division Comparative Cultural Studies",
                    "F Upper Division Comparative Cultural Studies 2",
                ])

                ip_count = sum(1 for r in course_rows if r["status"] == "ip")
                planned_count = sum(1 for r in course_rows if r["status"] == "planned")

                if not course_rows and not done:
                    missing_count = max(0.0, required - completed - ip_count - planned_count)
                    if missing_count > 0:
                        course_rows.append({
                            "code": "—",
                            "title": "Course needed",
                            "units": 0,
                            "grade": "—",
                            "status": "bad",
                        })

                meta = _status_meta(done, completed, ip_count, required, planned_count)

                udge_completed_codes = {
                    (r["code"] or "").upper()
                    for r in course_rows
                    if r["status"] == "ok" and r.get("code") and r["code"] != "—"
                }
                udge_ip_codes = {
                    (r["code"] or "").upper()
                    for r in course_rows
                    if r["status"] == "ip" and r.get("code") and r["code"] != "—"
                }
                udge_planned_codes = {
                    (r["code"] or "").upper()
                    for r in course_rows
                    if r["status"] == "planned" and r.get("code") and r["code"] != "—"
                }

                helper_options = _build_udge_helper_options(
                    profile,
                    udge_completed_codes,
                    udge_ip_codes,
                    udge_planned_codes,
                )

                subsection = {
                    "name": "General Education Upper Division",
                    "short": _short_group_name("General Education Upper Division"),
                    "required": required,
                    "completed": completed,
                    "in_progress": ip_count,
                    "planned": planned_count,
                    "remaining": max(0.0, required - completed - ip_count - planned_count),
                    "done": done,
                    "total_options": len(course_rows),
                    "assigned_completed_codes": list(udge_completed_codes),
                    "assigned_in_progress_codes": list(udge_ip_codes),
                    "assigned_planned_codes": list(udge_planned_codes),
                    "helper_options": helper_options,
                    "courses": course_rows,
                    "status_label": meta["label"],
                    "tag_class": meta["tag_class"],
                    "icon": meta["icon"],
                }

                subsections.append(subsection)
                group_required += required
                group_completed += completed
                group_in_progress += ip_count
                continue

            if raw_name in {
                "C Upper Division Arts and Humanities",
                "D Upper Division Social Sciences",
                "F Upper Division Comparative Cultural Studies",
                "F Upper Division Comparative Cultural Studies 2",
            }:
                udge_helper_items.append(item)
                continue

            display_name_map = {
                "A1 Oral Communication": "A1 Oral Communication",
                "A2 Written Communication": "A2 Written Communication",
                "A3 Critical Thinking": "A3 Critical Thinking",
                "B1 Physical Science": "B1 Physical Science",
                "B2 Life Science": "B2 Life Science",
                "B3 Laboratory Activity": "B3 Laboratory Activity",
                "B4 Math / Quantitative Reasoning": "B4 Math / Quantitative Reasoning",
                "B5 Upper Division Scientific Inquiry": "B5 Upper Division Scientific Inquiry",
                "C1 Arts": "C1 Arts",
                "C2 Humanities": "C2 Humanities",
                "C3 American History": "C3 American History",
                "D1 Social Sciences": "D1 Social Sciences",
                "D3 Constitution of the U.S.": "D3 Constitution of the U.S.",
                "D4 California State and Local Government": "D4 California State and Local Government",
                "E Lifelong Learning": "E Lifelong Learning",
                "F Comparative Cultural Studies": "F Comparative Cultural Studies",
                "Ethnic Studies": "Ethnic Studies",
                "Basic Skills Information Competence": "Basic Skills Information Competence",
                "Subject Explorations Information Competence": "Subject Explorations Information Competence",
                "Lower Division Algorithms & Programming": "Lower Division Algorithms & Programming",
                "Lower Division Major": "Lower Division Major",
                "Lower Division Life Science": "Lower Division Life Science",
                "Lower Division Physical Science": "Lower Division Physical Science",
                "Computer Science Upper Division Core": "Computer Science Upper Division Core",
                "Senior Electives": "Senior Electives",
                "General Education Upper Division": "General Education Upper Division",
                "C Upper Division Arts and Humanities": "UD C Arts and Humanities",
                "D Upper Division Social Sciences": "UD D Social Sciences",
                "F Upper Division Comparative Cultural Studies": "UD F Comparative Cultural Studies",
                "F Upper Division Comparative Cultural Studies 2": "UD F Comparative Cultural Studies 2",
            }

            display_name = display_name_map.get(raw_name, raw_name)
            short_name = _short_group_name(display_name or raw_name)

            if item.get("display_mode") == "course_equivalent":
                required = _safe_float(item.get("display_required", 0))
                completed = _safe_float(item.get("display_completed", 0))
                in_progress_from_group = _safe_float(item.get("display_in_progress", 0))
                planned_from_group = _safe_float(item.get("display_planned", 0))
            else:
                required = _safe_float(
                    item.get("min_required")
                    or item.get("required")
                    or item.get("credits_required")
                    or item.get("units_required")
                    or item.get("total_required")
                    or 0
                )

                completed = _safe_float(
                    item.get("completed")
                    or item.get("credits_completed")
                    or item.get("units_completed")
                    or 0
                )

                in_progress_from_group = _safe_float(
                    item.get("in_progress")
                    or item.get("credits_in_progress")
                    or item.get("units_in_progress")
                    or 0
                )

                planned_from_group = _safe_float(
                    item.get("planned")
                    or item.get("credits_planned")
                    or item.get("units_planned")
                    or 0
                )

            done = bool(item.get("done", False))

            assigned_completed_codes = {
                (code or "").upper()
                for code in item.get("assigned_completed_codes", [])
            }
            assigned_ip_codes = {
                (code or "").upper()
                for code in item.get("assigned_in_progress_codes", [])
            }
            assigned_planned_codes = {
                (code or "").upper()
                for code in item.get("assigned_planned_codes", [])
            }

            req = profile.get_requirement()
            block = None
            if req:
                block = req.blocks.filter(name=raw_name).first()
                if not block:
                    block = req.blocks.filter(name=display_name).first()
                if not block:
                    block = req.blocks.filter(name=short_name).first()

            block_course_codes = set()
            if block:
                block_course_codes = {
                    (c.code or "").upper()
                    for c in block.courses.all()
                    if c.code
                }

            matched_completed = []
            matched_ip = []
            matched_planned = []

            for cc in completed_courses:
                code = (cc.course.code or "").upper() if cc.course else ""
                if cc.course and code in assigned_completed_codes:
                    matched_completed.append(cc)

            for ic in in_progress_courses:
                code = (ic.course.code or "").upper() if ic.course else ""
                if ic.course and code in assigned_ip_codes:
                    matched_ip.append(ic)

            for pc in planned_courses:
                code = (pc.course.code or "").upper() if pc.course else ""
                if pc.course and code in assigned_planned_codes:
                    matched_planned.append(pc)

            if not matched_completed and completed > 0 and block_course_codes:
                best_cc = _pick_best_fallback_course(completed_courses, block_course_codes)
                if best_cc:
                    matched_completed.append(best_cc)

            if not matched_ip and in_progress_from_group > 0 and block_course_codes:
                best_ic = _pick_best_fallback_course(in_progress_courses, block_course_codes)
                if best_ic:
                    matched_ip.append(best_ic)

            if not matched_planned and planned_from_group > 0 and block_course_codes:
                best_pc = _pick_best_fallback_course(planned_courses, block_course_codes)
                if best_pc:
                    matched_planned.append(best_pc)

            seen_codes = set()
            course_rows = []

            for cc in matched_completed:
                code = (cc.course.code or "").upper()
                if code not in seen_codes:
                    course_rows.append({
                        "code": cc.course.code,
                        "title": cc.course.title,
                        "units": _safe_float(cc.course.credits),
                        "grade": "Done",
                        "status": "ok",
                    })
                    seen_codes.add(code)
                    used_display_codes.add(code)

            for ic in matched_ip:
                code = (ic.course.code or "").upper()
                if code not in seen_codes:
                    course_rows.append({
                        "code": ic.course.code,
                        "title": ic.course.title,
                        "units": _safe_float(ic.course.credits),
                        "grade": "IP",
                        "status": "ip",
                    })
                    seen_codes.add(code)
                    used_display_codes.add(code)

            for pc in matched_planned:
                code = (pc.course.code or "").upper()
                if code not in seen_codes:
                    course_rows.append({
                        "code": pc.course.code,
                        "title": pc.course.title,
                        "units": _safe_float(pc.course.credits),
                        "grade": "Planned",
                        "status": "planned",
                    })
                    seen_codes.add(code)
                    used_display_codes.add(code)

            ip_count = max(int(in_progress_from_group), len(matched_ip))
            planned_count = max(int(planned_from_group), len(matched_planned))

            used_requirement_codes.update(
                (cc.course.code or "").upper()
                for cc in matched_completed
                if cc.course and cc.course.code
            )
            used_requirement_codes.update(
                (ic.course.code or "").upper()
                for ic in matched_ip
                if ic.course and ic.course.code
            )
            used_requirement_codes.update(
                (pc.course.code or "").upper()
                for pc in matched_planned
                if pc.course and pc.course.code
            )

            if not course_rows:
                if done or completed > 0 or ip_count > 0 or planned_count > 0:
                    course_rows.append({
                        "code": "—",
                        "title": "Requirement satisfied by audit assignment",
                        "units": None,
                        "grade": "—",
                        "status": (
                            "ok" if completed > 0
                            else "ip" if ip_count > 0
                            else "planned"
                        ),
                    })
                else:
                    missing_count = max(0.0, required - completed - ip_count - planned_count)
                    if missing_count > 0:
                        course_rows.append({
                            "code": "—",
                            "title": "Course needed",
                            "units": None,
                            "missing_count": int(missing_count),
                            "grade": "—",
                            "status": "bad",
                        })

            meta = _status_meta(done, completed, ip_count, required, planned_count)

            subsection = {
                "name": display_name or "Requirement",
                "short": _short_group_name(display_name or raw_name or "Requirement"),
                "required": required,
                "completed": completed,
                "in_progress": ip_count,
                "planned": planned_count,
                "remaining": max(0.0, required - completed - ip_count - planned_count),
                "done": done,
                "total_options": len(block_course_codes),
                "assigned_completed_codes": list(assigned_completed_codes),
                "assigned_in_progress_codes": list(assigned_ip_codes),
                "assigned_planned_codes": list(assigned_planned_codes),
                "courses": course_rows,
                "status_label": meta["label"],
                "tag_class": meta["tag_class"],
                "icon": meta["icon"],
            }

            subsections.append(subsection)
            group_required += required
            group_completed += completed
            group_in_progress += ip_count

        if bucket_name == "General Education" and not has_udge_summary and udge_helper_items:
            helper_block_names = [
                "B5 Upper Division Scientific Inquiry",
                "C Upper Division Arts and Humanities",
                "D Upper Division Social Sciences",
                "F Upper Division Comparative Cultural Studies",
                "F Upper Division Comparative Cultural Studies 2",
            ]

            course_rows = _collect_block_course_rows(profile, helper_block_names)

            udge_completed_codes = {
                (r["code"] or "").upper()
                for r in course_rows
                if r["status"] == "ok" and r.get("code") and r["code"] != "—"
            }
            udge_ip_codes = {
                (r["code"] or "").upper()
                for r in course_rows
                if r["status"] == "ip" and r.get("code") and r["code"] != "—"
            }
            udge_planned_codes = {
                (r["code"] or "").upper()
                for r in course_rows
                if r["status"] == "planned" and r.get("code") and r["code"] != "—"
            }

            helper_options = _build_udge_helper_options(
                profile,
                udge_completed_codes,
                udge_ip_codes,
                udge_planned_codes,
            )

            if helper_options:
                best_option = max(
                    helper_options,
                    key=lambda opt: (
                        opt["done_count"] + opt["ip_count"] + opt["planned_count"],
                        opt["done_count"],
                        opt["ip_count"],
                        opt["planned_count"],
                    ),
                )
                completed = float(best_option["done_count"])
                ip_count = float(best_option["ip_count"])
                planned_count = float(best_option["planned_count"])
            else:
                completed = 0.0
                ip_count = 0.0
                planned_count = 0.0

            required = 3.0
            done = completed >= required

            if not course_rows and not done:
                missing_count = max(0.0, required - completed - ip_count - planned_count)
                if missing_count > 0:
                    course_rows.append({
                        "code": "—",
                        "title": "Course needed",
                        "units": 0,
                        "grade": "—",
                        "status": "bad",
                    })

            meta = _status_meta(done, completed, ip_count, required, planned_count)

            subsections.append({
                "name": "General Education Upper Division",
                "short": _short_group_name("General Education Upper Division"),
                "required": required,
                "completed": completed,
                "in_progress": ip_count,
                "planned": planned_count,
                "remaining": max(0.0, required - completed - ip_count - planned_count),
                "done": done,
                "total_options": len(course_rows),
                "assigned_completed_codes": list(udge_completed_codes),
                "assigned_in_progress_codes": list(udge_ip_codes),
                "assigned_planned_codes": list(udge_planned_codes),
                "helper_options": helper_options,
                "courses": course_rows,
                "status_label": meta["label"],
                "tag_class": meta["tag_class"],
                "icon": meta["icon"],
            })

        def _subsection_sort_key(section):
            name = (section.get("name") or "").strip()

            custom_order = {
            "A1 Oral Communication": 1,
            "A2 Written Communication": 2,
            "A3 Critical Thinking": 3,
            "B1 Physical Science": 4,
            "B2 Life Science": 5,
            "B3 Laboratory Activity": 6,
            "B4 Math / Quantitative Reasoning": 7,
            "B5 Upper Division Scientific Inquiry": 8,
            "C1 Arts": 9,
            "C2 Humanities": 10,
            "C3 American History": 11,
            "D1 Social Sciences": 12,
            "D3 Constitution of the U.S.": 13,
            "D4 California State and Local Government": 14,
            "E Lifelong Learning": 15,
            "F Comparative Cultural Studies": 16,
            "General Education Upper Division": 17,
            "Basic Skills Information Competence": 18,
            "Subject Explorations Information Competence": 19,
            "Ethnic Studies": 20,
            "Lower Division Algorithms & Programming": 101,
            "Lower Division Major": 102,
            "Lower Division Life Science": 103,
            "Lower Division Physical Science": 104,
            "Computer Science Upper Division Core": 105,
            "Senior Electives": 106,
            }

            return (custom_order.get(name, 999), name.lower())
        
        subsections.sort(key=_subsection_sort_key)

        group_required = sum(_safe_float(s["required"]) for s in subsections)
        group_completed = sum(_safe_float(s["completed"]) for s in subsections)
        group_in_progress = sum(_safe_float(s.get("in_progress", 0)) for s in subsections)
        group_planned = sum(_safe_float(s.get("planned", 0)) for s in subsections)
        group_remaining = sum(_safe_float(s["remaining"]) for s in subsections)

        audit_groups.append({
            "title": bucket_name,
            "required": group_required,
            "completed": group_completed,
            "planned": group_planned,
            "in_progress": group_in_progress,
            "remaining": group_remaining,
            "subsections": subsections,
        })

    additional_completed = [
        cc for cc in completed_courses
        if cc.course
        and cc.course.code
        and (cc.course.code or "").upper() not in used_requirement_codes
        and cc.course.numeric_level() is not None
        and cc.course.numeric_level() >= 100
    ]

    additional_in_progress = [
        ic for ic in in_progress_courses
        if ic.course
        and ic.course.code
        and (ic.course.code or "").upper() not in used_requirement_codes
        and ic.course.numeric_level() is not None
        and ic.course.numeric_level() >= 100
    ]

    additional_planned = [
        pc for pc in planned_courses
        if pc.course
        and pc.course.code
        and (pc.course.code or "").upper() not in used_requirement_codes
        and pc.course.numeric_level() is not None
        and pc.course.numeric_level() >= 100
    ]

    return {
        "audit_groups": audit_groups,
        "audit_summary": {
            "completed": int(completed_credits),
            "in_progress": int(in_progress_credits),
            "planned": int(
                sum(
                    _safe_float(getattr(pc.course, "credits", 0))
                    for pc in planned_courses
                    if pc.course
                )
            ),
            "remaining": int(remaining_credits),
            "total": int(target_credits),
        },
        "quick_stats": {
            "major": getattr(profile, "get_program_display", lambda: profile.program)(),
            "catalog_year": getattr(profile, "catalog_year", ""),
            "standing":"Senior" if completed_credits >= 90 else "Junior" if completed_credits >= 60 else "Sophomore" if completed_credits >= 30 else "Freshman",
        },
        "latest_dpr": DPRUpload.objects.filter(user=user).order_by("-uploaded_at").first(),
        "below_100_completed": below_100_completed,
        "below_100_in_progress": below_100_in_progress,
        "additional_completed": additional_completed,
        "additional_in_progress": additional_in_progress,
        "additional_planned": additional_planned,
    }

@profile_required
def audit(request):
    profile = request.profile

    shared = _build_requirement_audit_data(profile, request.user)

    context = {
        "profile": profile,
        **shared,
    }
    return render(request, "audit.html", context)


@profile_required
def upload_dpr(request):
    is_ajax = request.headers.get("X-Requested-With") in {"fetch", "XMLHttpRequest"}

    if request.method != "POST":
        form = DPRUploadForm()
        return render(request, "upload_dpr.html", {"form": form, "recent_uploads": []})

    uploaded_file = request.FILES.get("dpr_file") or request.FILES.get("file")
    if not uploaded_file:
        if is_ajax:
            return JsonResponse({"ok": False, "error": "No file uploaded."}, status=400)
        messages.error(request, "No file uploaded.")
        return redirect("dashboard")

    if not (uploaded_file.name or "").lower().endswith(".pdf"):
        if is_ajax:
            return JsonResponse({"ok": False, "error": "Please upload a PDF."}, status=400)
        messages.error(request, "Please upload a PDF.")
        return redirect("dashboard")

    profile = request.profile

    try:
        pdf_text = _extract_pdf_text(uploaded_file)
    except Exception as e:
        if is_ajax:
            return JsonResponse({"ok": False, "error": f"Could not read PDF: {e}"}, status=400)
        messages.error(request, f"Could not read PDF: {e}")
        return redirect("dashboard")

    completed_rows, ip_rows = _parse_dpr_lines(pdf_text)

    if not completed_rows and not ip_rows:
        if is_ajax:
            return JsonResponse(
                {"ok": False, "error": "This is not a valid DPR PDF."},
                status=400,
            )
        messages.error(request, "This is not a valid DPR PDF.")
        return redirect("dashboard")

    def build_course_map():
        course_map: dict[str, Course] = {}
        for c in Course.objects.filter(program=profile.program, catalog_year=profile.catalog_year):
            key = _norm_code(c.code)
            if key:
                course_map[key] = c
        return course_map

    course_map = build_course_map()

    def find_course(subject: str, number: str) -> Course | None:
        k1 = _norm_code(f"{subject}{number}")
        k2 = _norm_code(f"{subject}-{number}")
        k3 = _norm_code(f"{subject} {number}")
        return course_map.get(k1) or course_map.get(k2) or course_map.get(k3)

    def guess_title(subject: str, number: str) -> str:
        return f"Imported from DPR: {subject.upper()}-{number.upper()}"

    def guess_credits(number: str, units: float | None = None) -> float:
        if units is not None:
            return float(units)
        return 1.0 if str(number).upper().endswith("L") else 3.0

    def get_or_create_course(subject: str, number: str, units: float | None = None):
        course = find_course(subject, number)
        created = False

        if course:
            if (course.credits is None or float(course.credits) == 0) and units is not None:
                course.credits = float(units)
                course.save(update_fields=["credits"])
            return course, created

        code = f"{subject.upper()}-{number.upper()}"

        course = Course.objects.create(
            code=code,
            title=guess_title(subject, number),
            credits=guess_credits(number, units),
            program=profile.program,
            catalog_year=profile.catalog_year,
        )
        created = True

        # refresh lookup
        course_map[_norm_code(course.code)] = course
        course_map[_norm_code(f"{subject}{number}")] = course
        course_map[_norm_code(f"{subject}-{number}")] = course
        course_map[_norm_code(f"{subject} {number}")] = course

        return course, created

    with transaction.atomic():

        # save new DPR file
        DPRUpload.objects.create(user=request.user, file=uploaded_file)

        # clear old parsed progress
        CompletedClass.objects.filter(profile=profile).delete()
        InProgressClass.objects.filter(profile=profile).delete()
        profile.completed_codes = ""
        profile.save(update_fields=["completed_codes"])

        completed_added = 0
        in_progress_added = 0
        auto_created_courses: list[str] = []

        completed_seen: set[str] = set()
        ip_seen: set[str] = set()

        # write completed (unique)
        for r in completed_rows:
            key = _norm_code(f'{r["subject"]}{r["number"]}')
            if key in completed_seen:
                continue
            completed_seen.add(key)

            course, created = get_or_create_course(
                r["subject"],
                r["number"],
                r.get("units"),
            )
            if created:
                auto_created_courses.append(course.code)

            CompletedClass.objects.get_or_create(
                profile=profile,
                course=course,
                defaults={"term": r.get("term", "")},
            )
            completed_added += 1

        # write in-progress (unique)
        for r in ip_rows:
            key = _norm_code(f'{r["subject"]}{r["number"]}')
            if key in ip_seen:
                continue
            ip_seen.add(key)

            course, created = get_or_create_course(
                r["subject"],
                r["number"],
                r.get("units"),
            )
            if created:
                auto_created_courses.append(course.code)

            InProgressClass.objects.get_or_create(
                profile=profile,
                course=course,
                defaults={"term": r.get("term", "")},
            )
            in_progress_added += 1

    if is_ajax:
        return JsonResponse({
            "ok": True,
            "completed_added": completed_added,
            "in_progress_added": in_progress_added,
            "auto_created_count": len(set(auto_created_courses)),
            "auto_created_examples": sorted(set(auto_created_courses))[:20],
        })

    messages.success(
        request,
        f"DPR imported. Completed: {completed_added}, In Progress: {in_progress_added}, "
        f"Auto-created courses: {len(set(auto_created_courses))}."
    )
    return redirect("dashboard")

def _course_ui_state(profile, course):
    is_completed = CompletedClass.objects.filter(profile=profile, course=course).exists()
    is_in_progress = InProgressClass.objects.filter(profile=profile, course=course).exists()

    if is_completed:
        return {
            "progress_status": "completed",
            "progress_label": "Completed",
            "progress_badge_class": "bg-emerald-100 text-emerald-700",
            "action_label": "Remove",
            "can_add": False,
            "can_remove": True,
        }

    if is_in_progress:
        return {
            "progress_status": "in_progress",
            "progress_label": "In Progress",
            "progress_badge_class": "bg-amber-100 text-amber-700",
            "action_label": "Remove",
            "can_add": False,
            "can_remove": True,
        }

    return {
        "progress_status": "",
        "progress_label": "",
        "progress_badge_class": "",
        "action_label": "Add",
        "can_add": True,
        "can_remove": False,
    }


def _apply_course_ui_state(course, state):
    course.progress_status = state["progress_status"]
    course.progress_label = state["progress_label"]
    course.progress_badge_class = state["progress_badge_class"]
    course.action_label = state["action_label"]
    course.can_add = state["can_add"]
    course.can_remove = state["can_remove"]


def _post_action_redirect(request):
    next_url = request.POST.get("next") or request.META.get("HTTP_REFERER") or reverse("courses")
    return redirect(next_url)

@profile_required
def mark_course_completed(request, pk: int):
    if request.method != "POST":
        return redirect("courses")

    profile = request.profile
    course = get_object_or_404(
        Course,
        pk=pk,
        program=profile.program,
        catalog_year=profile.catalog_year,
    )

    InProgressClass.objects.filter(profile=profile, course=course).delete()
    CompletedClass.objects.get_or_create(profile=profile, course=course)

    messages.success(request, f"{course.code} added to Completed.")
    return _post_action_redirect(request)

@profile_required
def mark_course_in_progress(request, pk: int):
    if request.method != "POST":
        return redirect("courses")

    profile = request.profile
    course = get_object_or_404(
        Course,
        pk=pk,
        program=profile.program,
        catalog_year=profile.catalog_year,
    )

    CompletedClass.objects.filter(profile=profile, course=course).delete()
    InProgressClass.objects.get_or_create(profile=profile, course=course)

    messages.success(request, f"{course.code} added to In Progress.")
    return _post_action_redirect(request)

@profile_required
def remove_course_status(request, pk: int):
    if request.method != "POST":
        return redirect("courses")

    profile = request.profile
    course = get_object_or_404(
        Course,
        pk=pk,
        program=profile.program,
        catalog_year=profile.catalog_year,
    )

    CompletedClass.objects.filter(profile=profile, course=course).delete()
    InProgressClass.objects.filter(profile=profile, course=course).delete()

    messages.success(request, f"{course.code} removed from your progress.")
    return _post_action_redirect(request)

@profile_required
def courses_page(request):
    profile = get_object_or_404(StudentProfile, user=request.user)

    term_plans = profile.get_or_create_future_term_plans(count=4)
    semester_choices = [
        {
            "id": tp.id,
            "label": str(tp.term),
        }
        for tp in term_plans
    ]

    q = request.GET.get("q", "").strip()
    level_param = request.GET.get("level", "").strip()
    credits_param = request.GET.get("credits", "").strip()
    tag_param = request.GET.get("tag", "").strip()
    fulfillment_param = request.GET.get("fulfillment", "").strip()
    also_counts_for = request.GET.get("also_counts_for", "").strip()
    count_type = request.GET.get("count_type", "").strip()

    req = ProgramRequirement.objects.filter(
        program=profile.program,
        catalog_year=profile.catalog_year,
    ).first()

    base_qs = (
        Course.objects
        .filter(program=profile.program, catalog_year=profile.catalog_year)
        .prefetch_related("tags")
        .order_by("subject", "code")
    )

    if q:
        normalized_q = re.sub(r"[\s\-]+", "", q.upper())

        search_qs = base_qs.filter(
            Q(code__icontains=q) |
            Q(title__icontains=q) |
            Q(subject__icontains=q) |
            Q(description__icontains=q) |
            Q(section__icontains=q) |
            Q(tags__name__icontains=q)
        ).distinct()

        courses = list(search_qs)

        if not courses:
            all_courses = list(base_qs)

            normalized_matches = []
            for c in all_courses:
                normalized_code = re.sub(r"[\s\-]+", "", (c.code or "").upper())
                normalized_title = re.sub(r"[\s\-]+", "", (c.title or "").upper())
                normalized_subject = re.sub(r"[\s\-]+", "", (c.subject or "").upper())
                normalized_description = re.sub(r"[\s\-]+", "", (c.description or "").upper())
                normalized_section = re.sub(r"[\s\-]+", "", (c.section or "").upper())
                normalized_tags = re.sub(
                    r"[\s\-]+", "",
                    " ".join(t.name for t in c.tags.all()).upper()
                )

                if (
                    normalized_q in normalized_code
                    or normalized_q in normalized_title
                    or normalized_q in normalized_subject
                    or normalized_q in normalized_description
                    or normalized_q in normalized_section
                    or normalized_q in normalized_tags
                ):
                    normalized_matches.append(c)

            courses = normalized_matches
    else:
        courses = list(base_qs)

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
        courses = [
            c for c in courses
            if (
                parse_number_from_code(getattr(c, "code", "")) is not None
                and lo <= parse_number_from_code(getattr(c, "code", "")) <= hi
            )
        ]

    if credits_param:
        try:
            wanted_credits = float(credits_param)
            courses = [
                c for c in courses
                if getattr(c, "credits", None) is not None and float(c.credits) == wanted_credits
            ]
        except ValueError:
            pass

    if tag_param:
        courses = [c for c in courses if any(t.name == tag_param for t in c.tags.all())]

    if fulfillment_param and req:
        block = req.blocks.filter(name=fulfillment_param).first()
        if block:
            block_course_ids = set(block.courses.values_list("id", flat=True))
            courses = [c for c in courses if c.id in block_course_ids]

    if req and courses:
        count_map_all = build_course_counting_map(courses, req)
    else:
        count_map_all = {}

    for c in courses:
        info = count_map_all.get(c.id, {})
        c.eligible_blocks = info.get("eligible_names", [])
        c.applied_blocks = info.get("applied_names", [])
        c.eligible_count = info.get("eligible_count", 0)
        c.applied_count = info.get("applied_count", 0)
        c.is_multi_count = info.get("is_multi_count", False)

    if fulfillment_param and also_counts_for and req:
        filtered = []

        for c in courses:
            applied = set(getattr(c, "applied_blocks", []) or [])
            eligible = set(getattr(c, "eligible_blocks", []) or [])

            if fulfillment_param in applied and also_counts_for in applied:
                filtered.append(c)
                continue

            if (
                getattr(c, "is_multi_count", False)
                and fulfillment_param in eligible
                and also_counts_for in eligible
            ):
                filtered.append(c)

        courses = filtered

    if count_type == "single":
        courses = [c for c in courses if getattr(c, "applied_count", 0) == 1]
    elif count_type == "multi":
        courses = [c for c in courses if getattr(c, "applied_count", 0) > 1]

    completed_ids = set(
        CompletedClass.objects.filter(profile=profile).values_list("course_id", flat=True)
    )
    in_progress_ids = set(
        InProgressClass.objects.filter(profile=profile).values_list("course_id", flat=True)
    )
    planned_course_ids = set(
        PlannedCourse.objects.filter(term_plan__profile=profile).values_list("course_id", flat=True)
    )

    planned_map = {}
    for pc in (
        PlannedCourse.objects
        .filter(term_plan__profile=profile)
        .select_related("course", "term_plan", "term_plan__term")
        .order_by("term_plan__position")
    ):
        if pc.course_id not in planned_map:
            planned_map[pc.course_id] = str(pc.term_plan.term)

    def _course_sort_key(course):
        code = (getattr(course, "code", "") or "").upper()
        title = (getattr(course, "title", "") or "").upper()

        if q:
            normalized_query = re.sub(r"[\s\-]+", "", q.upper())
            normalized_code = re.sub(r"[\s\-]+", "", code)
            normalized_title = re.sub(r"[\s\-]+", "", title)

            if normalized_code == normalized_query:
                search_rank = 0
            elif normalized_code.startswith(normalized_query):
                search_rank = 1
            elif normalized_query in normalized_code:
                search_rank = 2
            elif normalized_title.startswith(normalized_query):
                search_rank = 3
            elif normalized_query in normalized_title:
                search_rank = 4
            else:
                search_rank = 5

            is_completed = course.id in completed_ids
            is_in_progress = course.id in in_progress_ids

            return (
                1 if is_in_progress else 0,
                1 if is_completed else 0,
                search_rank,
                code,
                title,
            )

        is_in_progress = course.id in in_progress_ids
        is_planned = course.id in planned_course_ids

        return (
            0 if is_in_progress else 1,
            0 if is_planned else 1,
            code,
            title,
        )

    courses.sort(key=_course_sort_key)
    results_count = len(courses)

    paginator = Paginator(courses, 30)
    page_number = request.GET.get("page") or 1
    page_obj = paginator.get_page(page_number)
    courses_page_items = list(page_obj.object_list)

    for c in courses_page_items:
        if c.id in completed_ids:
            state = {
                "progress_status": "completed",
                "progress_label": "Completed",
                "progress_badge_class": "bg-emerald-100 text-emerald-700",
                "action_label": "Remove",
                "can_add": False,
                "can_remove": True,
            }
        elif c.id in in_progress_ids:
            state = {
                "progress_status": "in_progress",
                "progress_label": "In Progress",
                "progress_badge_class": "bg-amber-100 text-amber-700",
                "action_label": "Remove",
                "can_add": False,
                "can_remove": True,
            }
        else:
            state = {
                "progress_status": "",
                "progress_label": "",
                "progress_badge_class": "",
                "action_label": "Add",
                "can_add": True,
                "can_remove": False,
            }

        _apply_course_ui_state(c, state)
        c.planned_term_label = planned_map.get(c.id, "")
        c.is_planned = c.id in planned_course_ids
        c.is_locked_for_planning = False
        c.lock_reason = ""

        if not c.is_planned:
            ok_for_some_term = False
            last_reason = "Not ready for planning yet."

            for tp in term_plans:
                ok, reason = profile.can_add_course_to_term_plan(c, tp)
                if ok:
                    ok_for_some_term = True
                    last_reason = ""
                    break
                if reason:
                    last_reason = reason

            c.is_locked_for_planning = not ok_for_some_term
            c.lock_reason = last_reason if not ok_for_some_term else ""

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
        ("2", "2 credit"),
        ("3", "3 credit"),
        ("4", "4 credit"),
        ("5", "5 credit"),
    ]

    fulfillment_labels = []
    if req:
        raw_labels = list(
            req.blocks.order_by("name").values_list("name", flat=True).distinct()
        )

        label_aliases = {
            "F Upper Division Comparative Cultural Studies 2": "F Upper Division Comparative Cultural Studies",
        }

        normalized = []
        seen = set()
        for label in raw_labels:
            final_label = label_aliases.get(label, label)
            if final_label not in seen:
                normalized.append(final_label)
                seen.add(final_label)

        fulfillment_labels = normalized

    fulfillment_choices = [("", "Any fulfillment")] + [
        (label, label) for label in fulfillment_labels
    ]
    also_counts_for_choices = [("", "Also Counts For")] + [
        (label, label) for label in fulfillment_labels
    ]

    count_type_choices = [
        ("", "Any count type"),
        ("single", "Single Count"),
        ("multi", "Multi Count"),
    ]

    ctx = {
        "courses": courses_page_items,
        "page_obj": page_obj,
        "profile": profile,
        "q": q,
        "selected_level": level_param,
        "selected_credits": credits_param,
        "selected_tag": tag_param,
        "selected_fulfillment": fulfillment_param,
        "level_choices": level_choices,
        "credit_choices": credit_choices,
        "fulfillment_choices": fulfillment_choices,
        "results_count": results_count,
        "count_type_choices": count_type_choices,
        "selected_count_type": count_type,
        "also_counts_for_choices": also_counts_for_choices,
        "selected_also_counts_for": also_counts_for,
        "semester_choices": semester_choices,
    }

    if _wants_partial(request):
        return render(request, "courses_list.html", ctx)

    return render(request, "courses.html", ctx)

@profile_required
@require_POST
def remove_course_from_plan(request, course_id):
    profile = request.profile

    course = get_object_or_404(
        Course,
        id=course_id,
        program=profile.program,
        catalog_year=profile.catalog_year,
    )

    deleted_count, _ = PlannedCourse.objects.filter(
        term_plan__profile=profile,
        course=course,
    ).delete()

    if deleted_count:
        messages.success(request, f"{course.code} removed from your degree plan.")
    else:
        messages.info(request, f"{course.code} was not in your degree plan.")

    next_url = request.POST.get("next") or request.META.get("HTTP_REFERER") or reverse("courses")
    return redirect(next_url)

@login_required
def course_detail(request, pk: int):
    profile = get_object_or_404(StudentProfile, user=request.user)

    course = get_object_or_404(
        Course,
        pk=pk,
        program=profile.program,
        catalog_year=profile.catalog_year,
    )

    req = ProgramRequirement.objects.filter(
        program=profile.program,
        catalog_year=profile.catalog_year,
    ).first()

    count_map = build_course_counting_map([course], req) if req else {}
    count_info = count_map.get(course.id, {})

    course.eligible_blocks = count_info.get("eligible_names", [])
    course.applied_blocks = count_info.get("applied_names", [])
    course.eligible_count = count_info.get("eligible_count", 0)
    course.applied_count = count_info.get("applied_count", 0)
    course.is_multi_count = count_info.get("is_multi_count", False)

    tags = course.tags.all().order_by("name")
    prereqs = course.prerequisites.all().order_by("subject", "level", "code")
    groups = (
        course.prereq_groups.all().prefetch_related("options").order_by("name")
        if hasattr(course, "prereq_groups")
        else []
    )
    offered = (
        course.offered_in.all().order_by("year", "season")
        if hasattr(course, "offered_in")
        else []
    )

    state = _course_ui_state(profile, course)

    term_plans = profile.get_or_create_future_term_plans(count=4)
    semester_choices = [
        {"id": tp.id, "label": str(tp.term)}
        for tp in term_plans
    ]

    planned_term_label = ""
    planned_pc = (
        PlannedCourse.objects
        .filter(term_plan__profile=profile, course=course)
        .select_related("term_plan", "term_plan__term")
        .order_by("term_plan__position")
        .first()
    )
    if planned_pc:
        planned_term_label = str(planned_pc.term_plan.term)

    ctx = {
        "c": course,
        "tags": tags,
        "prereqs": prereqs,
        "groups": groups,
        "offered": offered,
        "profile": profile,
        "progress_status": state["progress_status"],
        "progress_label": state["progress_label"],
        "progress_badge_class": state["progress_badge_class"],
        "can_add": state["can_add"],
        "can_remove": state["can_remove"],
        "action_label": state["action_label"],
        "eligible_blocks": course.eligible_blocks,
        "applied_blocks": course.applied_blocks,
        "eligible_count": course.eligible_count,
        "applied_count": course.applied_count,
        "is_multi_count": course.is_multi_count,
        "semester_choices": semester_choices,
        "planned_term_label": planned_term_label,
    }

    if _wants_partial(request):
        return render(request, "course_detail_modal.html", ctx)

    return render(request, "course_detail.html", ctx)