# users/views.py
from __future__ import annotations

import io
import os
import random
import re
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.hashers import make_password, check_password
from django.db import transaction
from django.db.models import Q, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render, redirect
from django.urls import reverse
from django.utils import timezone

from .models import (
    Course,
    StudentProfile,
    Term,
    ProgramRequirement,
    CompletedClass,
    InProgressClass,
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

    remaining = profile.remaining_required_courses()
    next_list, next_credits, next_term = profile.recommend_next_term()
    grad_term, remaining_credits, completed_credits, target_credits = profile.approximate_graduation_term()

    group_progress_raw = profile.requirement_group_progress()

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

    in_progress_credits = sum(
        float(c.course.credits or 0)
        for c in InProgressClass.objects.filter(profile=profile).select_related("course")
        if c.course
    )

    remaining_for_chart = max(
        0,
        int(target_credits - completed_credits - float(in_progress_credits))
    )
    denom = target_credits or 1
    progress_percent = min(
        100,
        int(((completed_credits + float(in_progress_credits)) / denom) * 100)
    ) 

    context = {
        "profile": profile,
        "current_term": Term.from_date(),
        "grad_term": grad_term,
        "completed_credits": int(completed_credits),
        "in_progress_credits": int(float(in_progress_credits)),
        "remaining_credits": remaining_for_chart,
        "target_credits": int(target_credits),
        "progress_percent": progress_percent,
        "ge_progress": ge_progress,
        "major_progress": major_progress,
        "remaining": remaining,
        "next_list": next_list,
        "next_credits": int(next_credits),
        "next_term": next_term,
        "req_missing": req_missing,
        "incomplete_block_count": incomplete_block_count,
    }

    return render(request, "dashboard.html", context)

@profile_required
def degree_plan(request):
    return render(request, "degree_plan.html", {"profile": request.profile})


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


def _status_meta(done: bool, completed: float, in_progress: float, required: float):
    used = completed + in_progress
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

    return rows

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
    if hasattr(profile, "requirement_group_progress"):
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

    for bucket_name in bucket_order:
        items = grouped.get(bucket_name, [])
        if not items:
            continue

        subsections = []
        group_required = 0.0
        group_completed = 0.0
        group_in_progress = 0.0

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

                done = bool(item.get("done", False))

                course_rows = _collect_block_course_rows(profile, [
                    "B5 Upper Division Scientific Inquiry",
                    "C Upper Division Arts and Humanities",
                    "D Upper Division Social Sciences",
                    "F Upper Division Comparative Cultural Studies",
                ])

                ip_count = sum(1 for r in course_rows if r["status"] == "ip")

                if not course_rows and not done:
                    missing_count = max(0.0, required - completed - ip_count)
                    if missing_count > 0:
                        course_rows.append({
                            "code": "—",
                            "title": "Course needed",
                            "units": missing_count,
                            "grade": "—",
                            "status": "bad",
                        })

                meta = _status_meta(done, completed, ip_count, required)

                subsection = {
                    "name": "General Education Upper Division",
                    "short": _short_group_name("General Education Upper Division"),
                    "required": required,
                    "completed": completed,
                    "in_progress": ip_count,
                    "remaining": max(0.0, required - completed - ip_count),
                    "done": done,
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

            if raw_name == "F Upper Division Comparative Cultural Studies 2":
                continue

            if raw_name in {
                "C Upper Division Arts and Humanities",
                "D Upper Division Social Sciences",
                "F Upper Division Comparative Cultural Studies",
                "F Upper Division Comparative Cultural Studies 2",
            }:
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

            done = bool(item.get("done", False))

            matched_completed = []
            matched_ip = []

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
                }

            for cc in completed_courses:
                if cc.course and (cc.course.code or "").upper() in block_course_codes:
                    matched_completed.append(cc)

            for ic in in_progress_courses:
                if ic.course and (ic.course.code or "").upper() in block_course_codes:
                    matched_ip.append(ic)

            ip_count = max(int(in_progress_from_group), len(matched_ip))
            course_rows = []

            for cc in matched_completed:
                course_rows.append({
                    "code": cc.course.code,
                    "title": cc.course.title,
                    "units": _safe_float(cc.course.credits),
                    "grade": "Done",
                    "status": "ok",
                })

            for ic in matched_ip:
                course_rows.append({
                    "code": ic.course.code,
                    "title": ic.course.title,
                    "units": _safe_float(ic.course.credits),
                    "grade": "IP",
                    "status": "ip",
                })

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

            if not course_rows and not done:
                missing_count = max(0.0, required - completed - ip_count)
                if missing_count > 0:
                    course_rows.append({
                        "code": "—",
                        "title": "Course needed",
                        "units": missing_count,
                        "grade": "—",
                        "status": "bad",
                    })

            meta = _status_meta(done, completed, ip_count, required)

            subsection = {
                "name": display_name or "Requirement",
                "short": _short_group_name(display_name or raw_name or "Requirement"),
                "required": required,
                "completed": completed,
                "in_progress": ip_count,
                "remaining": max(0.0, required - completed - ip_count),
                "done": done,
                "courses": course_rows,
                "status_label": meta["label"],
                "tag_class": meta["tag_class"],
                "icon": meta["icon"],
            }

            subsections.append(subsection)
            group_required += required
            group_completed += completed
            group_in_progress += ip_count

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

        audit_groups.append({
            "title": bucket_name,
            "required": group_required,
            "completed": group_completed,
            "in_progress": group_in_progress,
            "remaining": max(0.0, group_required - group_completed - group_in_progress),
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

    return {
        "audit_groups": audit_groups,
        "audit_summary": {
            "completed": int(completed_credits),
            "in_progress": int(in_progress_credits),
            "remaining": int(remaining_credits),
            "total": int(target_credits),
        },
        "quick_stats": {
            "major": getattr(profile, "get_program_display", lambda: profile.program)(),
            "catalog_year": getattr(profile, "catalog_year", ""),
            "standing": "Junior" if completed_credits >= 60 else "Sophomore" if completed_credits >= 30 else "Freshman",
        },
        "latest_dpr": DPRUpload.objects.filter(user=user).order_by("-uploaded_at").first(),
        "below_100_completed": below_100_completed,
        "below_100_in_progress": below_100_in_progress,
        "additional_completed": additional_completed,
        "additional_in_progress": additional_in_progress,
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

    base_qs = (
        Course.objects
        .filter(program=profile.program, catalog_year=profile.catalog_year)
        .prefetch_related("tags")
        .order_by("subject", "code")
    )

    q = request.GET.get("q", "").strip()
    if q:
        raw_q = q
        normalized_q = re.sub(r"[\s\-]+", "", raw_q.upper())

        courses_for_search = list(base_qs)
        matched_ids = []

        for c in courses_for_search:
            code = (c.code or "")
            title = (c.title or "")
            subject = (c.subject or "")
            description = (c.description or "")

            normalized_code = re.sub(r"[\s\-]+", "", code.upper())
            normalized_subject = re.sub(r"[\s\-]+", "", subject.upper())
            normalized_title = re.sub(r"[\s\-]+", "", title.upper())
            normalized_description = re.sub(r"[\s\-]+", "", description.upper())

            tag_names = " ".join(t.name for t in c.tags.all())
            normalized_tags = re.sub(r"[\s\-]+", "", tag_names.upper())

            if (
                raw_q.lower() in code.lower()
                or raw_q.lower() in title.lower()
                or raw_q.lower() in subject.lower()
                or raw_q.lower() in description.lower()
                or raw_q.lower() in tag_names.lower()
                or normalized_q in normalized_code
                or normalized_q in normalized_subject
                or normalized_q in normalized_title
                or normalized_q in normalized_description
                or normalized_q in normalized_tags
            ):
                matched_ids.append(c.id)

        base_qs = base_qs.filter(id__in=matched_ids).distinct()

    courses = list(base_qs)

    completed_ids = set(
        CompletedClass.objects.filter(profile=profile).values_list("course_id", flat=True)
    )
    in_progress_ids = set(
        InProgressClass.objects.filter(profile=profile).values_list("course_id", flat=True)
    )

    for c in courses:
        state = _course_ui_state(profile, c)
        _apply_course_ui_state(c, state)

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
        courses = [
            c for c in courses
            if (
                parse_number_from_code(getattr(c, "code", "")) is not None
                and lo <= parse_number_from_code(getattr(c, "code", "")) <= hi
            )
        ]

    credits_param = request.GET.get("credits", "").strip()
    if credits_param:
        try:
            wanted_credits = float(credits_param)
            courses = [
                c for c in courses
                if getattr(c, "credits", None) is not None and float(c.credits) == wanted_credits
            ]
        except ValueError:
            pass

    tag_param = request.GET.get("tag", "").strip()
    if tag_param:
        courses = [c for c in courses if any(t.name == tag_param for t in c.tags.all())]

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
    fulfillment_choices = [("", "Any fulfillment")] + [(label, label) for label in fulfillment_labels]

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
    profile = get_object_or_404(StudentProfile, user=request.user)

    course = get_object_or_404(
        Course,
        pk=pk,
        program=profile.program,
        catalog_year=profile.catalog_year,
    )

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
    }

    if _wants_partial(request):
        return render(request, "course_detail_modal.html", ctx)

    return render(request, "course_detail.html", ctx)