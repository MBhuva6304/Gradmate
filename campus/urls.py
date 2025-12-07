# campus/urls.py

from django.contrib import admin
from django.urls import path
from django.contrib.auth import views as auth_views

from users import views as users_views
from users import otp_views
from users.forms import EmailAuthenticationForm


urlpatterns = [
    # Admin
    path("admin/", admin.site.urls),

    # Core app pages
    path("", users_views.dashboard, name="dashboard"),
    path("settings/", users_views.settings_page, name="settings"),
    path("degree-plan/", users_views.degree_plan, name="degree_plan"),
    path("audit/", users_views.audit, name="audit"),

    # Courses (list + detail)
    # This name **must** be "courses" to match {% url 'courses' %} in base.html
    path("courses/", users_views.courses_page, name="courses"),
    path("courses/<int:pk>/", users_views.course_detail, name="course_detail"),

    # Profile / setup
    path("setup-profile/", users_views.setup_profile, name="setup_profile"),

    # Auth
    path(
        "login/",
        auth_views.LoginView.as_view(
            template_name="login.html",
            authentication_form=EmailAuthenticationForm,
        ),
        name="login",
    ),
    path(
        "logout/",
        auth_views.LogoutView.as_view(next_page="login"),
        name="logout",
    ),

    # Signup + email verification
    path("signup/", users_views.signup, name="signup"),
    path("verify-signup/", users_views.verify_signup, name="verify_signup"),

    # Password reset via OTP
    path(
        "forgot-password/",
        otp_views.forgot_password,
        name="forgot_password",
    ),
    path(
        "reset-with-otp/",
        otp_views.reset_with_otp,
        name="reset-with-otp",
    ),
]

# Optional: serve media in development (safe only when DEBUG=True)
try:
    from django.conf import settings
    from django.conf.urls.static import static

    if settings.DEBUG:
        urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
except Exception:
    # In case settings isn't available for some reason, just skip media URLs
    pass
