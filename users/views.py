# users/views.py
from __future__ import annotations

import io
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
    """
    Returns:
      completed_rows: [{term, subject, number, units, grade}]
      ip_rows:        [{term, subject, number, units}]

    Handles DPR lines like:
      23SP  MATH 105  5.0IP  PRE-CALC II
      22FA  COMP 110  3.0 A  INTRO ...
      2023FA COMP-110 3.0 A  INTRO ...
      24SP  COMP110L  1.0 A  LAB ...
    """
    completed: list[dict] = []
    inprog: list[dict] = []

    completed_grade_tokens = {
        "A", "A-", "A+",
        "B", "B-", "B+",
        "C", "C-", "C+",
        "D", "D-", "D+",
        "F",
        "P", "CR",
    }

    for raw in (text or "").splitlines():
        line = " ".join(raw.split())
        if not line:
            continue

        toks = line.split(" ")
        if len(toks) < 3:
            continue

        term = toks[0].upper()
        if not _TERM_RE.match(term):
            continue

        subject = ""
        number = ""
        units_token = ""

        # Case A: COMP 110 3.0 A
        if (
            len(toks) >= 4
            and re.fullmatch(r"[A-Z]{2,6}", toks[1], re.I)
            and re.fullmatch(r"\d{1,4}[A-Z]?", toks[2], re.I)
        ):
            subject = toks[1].upper()
            number = toks[2].upper()
            units_token = toks[3].upper()
            status_index = 4

        # Case B: COMP-110 3.0 A  OR  COMP110L 1.0 A
        else:
            m = _COURSE_TOKEN_RE.match(toks[1])
            if not m or len(toks) < 3:
                continue
            subject = m.group("subj").upper()
            number = m.group("num").upper()
            units_token = toks[2].upper()
            status_index = 3

        # units token may be "5.0IP" or "3.0"
        m2 = re.match(r"^(?P<units>\d+(?:\.\d+)?)(?P<status>[A-Z]{1,3})?$", units_token)
        if not m2:
            continue

        units = float(m2.group("units"))
        glued_status = (m2.group("status") or "").upper()
        next_tok = toks[status_index].upper() if len(toks) > status_index else ""
        status = glued_status or next_tok

        if status == "IP":
            inprog.append({
                "term": term,
                "subject": subject,
                "number": number,
                "units": units,
            })
            continue

        if status in completed_grade_tokens:
            completed.append({
                "term": term,
                "subject": subject,
                "number": number,
                "units": units,
                "grade": status,
            })
            continue

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

    return render(request, "settings.html", {
        "profile_form": profile_form,
        "pwd_form": pwd_form,
        "profile": profile,
        "completed_courses": completed_courses,
    })


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
        "B3 Lab Activity": "B3",
        "B4 Math / Quantitative Reasoning": "B4",
        "B5 Upper Division Scientific Inquiry": "B5",
        "C1 Arts": "C1",
        "C2 Humanities": "C2",
        "D1 Social Sciences": "D1",
        "D3 Constitution of the U.S.": "D3",
        "D4 California State and Local Government": "D4",
        "E Lifelong Learning": "E",
        "F Comparative Cultural Studies": "F",
    }

    major_mapping = {
        "Pre-Major": "PRE",
        "Lower Division Core": "LDC",
        "Lower Division Elective A": "LDE-A",
        "Lower Division Elective B": "LDE-B",
        "Probability / Statistics": "STAT",
        "Upper Division Core": "UDC",
        "Senior Electives": "SE",
        "Ethnic Studies": "ES",
        "Information Competence": "IC",
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
        "A1": 1, "A2": 2, "A3": 3,
        "B1": 4, "B2": 5, "B3": 6, "B4": 7, "B5": 8,
        "C1": 9, "C2": 10,
        "D1": 11, "D3": 12, "D4": 13,
        "E": 14, "F": 15,
    }
    major_order = {
        "PRE": 1,
        "LDC": 2,
        "LDE-A": 3,
        "LDE-B": 4,
        "STAT": 5,
        "UDC": 6,
        "SE": 7,
        "ES": 8,
        "IC": 9,
    }

    ge_progress.sort(key=lambda g: ge_order.get(g["short"], 999))
    major_progress.sort(key=lambda g: major_order.get(g["short"], 999))

    incomplete_block_count = sum(1 for g in (ge_progress + major_progress) if not g.get("done"))

    in_progress_credits = (
        InProgressClass.objects
        .filter(profile=profile)
        .aggregate(total=Sum("course__credits"))
        .get("total") or 0
    )

    remaining_for_chart = max(
        0,
        int(target_credits - completed_credits - float(in_progress_credits))
    )
    denom = target_credits or 1
    progress_percent = int(
        ((completed_credits + float(in_progress_credits)) / denom) * 100
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


@profile_required
def audit(request):
    return render(request, "audit.html", {"profile": request.profile})


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
        # delete old uploaded DPR files + rows
        old_uploads = DPRUpload.objects.filter(user=request.user)
        for old in old_uploads:
            if old.file:
                old.file.delete(save=False)
        old_uploads.delete()

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

    courses = list(base_qs)

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