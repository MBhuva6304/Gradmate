# users/otp_views.py

import random
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password, check_password
from django.core.mail import send_mail
from django.shortcuts import render, redirect
from django.utils import timezone

from .models import EmailOTP
from .otp_forms import ForgotPasswordForm, ResetWithOTPForm


User = get_user_model()


def _gen6() -> str:
    """Return a 6-digit zero-padded code as a string."""
    return f"{random.randint(0, 999999):06d}"


def forgot_password(request):
    """
    Step 1: Ask for email, send a 6-digit code, and remember the email in session.
    """
    if request.method == "POST":
        form = ForgotPasswordForm(request.POST)
        if form.is_valid():
            email = form.cleaned_data["email"].strip().lower()
            # Remember email for the next step so the user doesn't retype it
            request.session["reset_email"] = email

            try:
                user = User.objects.get(email__iexact=email)
            except User.DoesNotExist:
                # Do not reveal whether an account exists
                messages.success(request, "If that email exists, we sent a 6-digit code.")
                return redirect("reset-with-otp")

            # Optionally clean up this user's expired or unused old RESET OTPs
            EmailOTP.objects.filter(
                user=user,
                purpose="RESET",
                is_used=False,
                expires_at__lte=timezone.now(),
            ).delete()

            code = _gen6()
            EmailOTP.create_for_user(user, make_password(code), purpose="RESET")

            send_mail(
                subject="Your password reset code",
                message=(
                    f"Your 6-digit code is: {code}\n"
                    f"This code expires in {EmailOTP.expire_minutes()} minutes."
                ),
                from_email=None,
                recipient_list=[email],
                fail_silently=False,
            )

            messages.success(request, "We emailed you a 6-digit code. Check your inbox.")
            return redirect("reset-with-otp")
    else:
        form = ForgotPasswordForm()

    return render(request, "forgot_password.html", {"form": form})


def reset_with_otp(request):
    """
    Step 2: Verify the code and set a new password.
    Email comes from the session and is *not* asked again.
    """
    email = request.session.get("reset_email")
    if not email:
        messages.error(request, "Session expired. Please enter your email again.")
        return redirect("forgot-password")

    if request.method == "POST":
        # Force the form to use the session email (ignore any posted email)
        post = request.POST.copy()
        post["email"] = email
        form = ResetWithOTPForm(post)
        if form.is_valid():
            code = form.cleaned_data["code"].strip()
            new_password = form.cleaned_data["new_password1"]

            try:
                user = User.objects.get(email__iexact=email)
            except User.DoesNotExist:
                messages.error(request, "Invalid email or code.")
                return render(request, "reset_with_otp.html", {"form": form, "email": email})

            otp = (
                EmailOTP.objects.filter(user=user, purpose="RESET", is_used=False)
                .order_by("-created_at")
                .first()
            )

            # No OTP or already expired/max attempts?
            if not otp or not otp.is_valid():
                messages.error(request, "Code expired or invalid. Please request a new one.")
                return redirect("forgot-password")

            # Check the user-entered code against the stored hash
            if not check_password(code, otp.code_hash):
                otp.attempts += 1
                otp.save(update_fields=["attempts"])
                messages.error(request, "Invalid code.")
                return render(request, "reset_with_otp.html", {"form": form, "email": email})

            # Success: consume the OTP and change the password
            otp.mark_used()
            user.set_password(new_password)
            user.save()

            # Optional: delete any remaining unused RESET OTPs
            EmailOTP.objects.filter(user=user, purpose="RESET", is_used=False).delete()

            # Clear the session email now that we're done
            request.session.pop("reset_email", None)

            messages.success(request, "Password changed. You can now sign in.")
            return redirect("login")
    else:
        # Prefill form so field validation messages know about the email,
        # but the template should hide the email input.
        form = ResetWithOTPForm(initial={"email": email})

    return render(request, "reset_with_otp.html", {"form": form, "email": email})
