from django.contrib import admin
from django.urls import path
from django.contrib.auth import views as auth_views

from users import views as users_views
from users import otp_views
from users.forms import EmailAuthenticationForm

urlpatterns = [
    path("admin/", admin.site.urls),

    path("", users_views.home_page, name="home"),
    path("dashboard/", users_views.dashboard, name="dashboard"),
    path("settings/", users_views.settings_page, name="settings"),
    path("degree-plan/", users_views.degree_plan, name="degree_plan"),
    path("degree-plan/auto-suggest/", users_views.auto_suggest_degree_plan, name="auto_suggest_degree_plan"),
    path("degree-plan/clear/", users_views.clear_degree_plan, name="clear_degree_plan"),
    path("degree-plan/add-term/", users_views.add_future_term, name="add_future_term"),

    path("degree-plan/term/<int:term_plan_id>/remove/", users_views.remove_term_plan, name="remove_term_plan"),
    path("degree-plan/term/<int:term_plan_id>/notes/", users_views.save_term_notes, name="save_term_notes"),

    path("degree-plan/term/<int:term_plan_id>/add-course/<int:course_id>/", users_views.add_planned_course, name="add_planned_course"),
    path("degree-plan/course/<int:course_id>/add/", users_views.add_planned_course_by_form, name="add_planned_course_by_form"),
    path("degree-plan/term/<int:term_plan_id>/suggested-course/<int:course_id>/add/", users_views.add_suggested_course_to_term, name="add_suggested_course_to_term"),

    path("degree-plan/planned-course/<int:planned_course_id>/remove/", users_views.remove_planned_course, name="remove_planned_course"),
    path("degree-plan/planned-course/<int:planned_course_id>/move/", users_views.move_planned_course, name="move_planned_course"),

    path("degree-plan/course/<int:course_id>/remove/", users_views.remove_course_from_plan, name="remove_course_from_plan"),
    path("audit/", users_views.audit, name="audit"),

    path("courses/", users_views.courses_page, name="courses"),
    path("courses/<int:pk>/", users_views.course_detail, name="course_detail"),
    path("courses/<int:pk>/mark-completed/", users_views.mark_course_completed, name="mark_course_completed"),
    path("courses/<int:pk>/mark-in-progress/", users_views.mark_course_in_progress, name="mark_course_in_progress"),
    path("courses/<int:pk>/remove-status/", users_views.remove_course_status, name="remove_course_status"),
    # DPR upload (dashboard modal uses this)
    path("dpr/upload/", users_views.upload_dpr, name="upload_dpr"),
    # Clear Audit Cache
    path("settings/clear-audit/", users_views.clear_audit_data, name="clear_audit_data"),

    path("setup-profile/", users_views.setup_profile, name="setup_profile"),

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
        auth_views.LogoutView.as_view(next_page="home"),
        name="logout",
    ),

    path("signup/", users_views.signup, name="signup"),
    path("verify-signup/", users_views.verify_signup, name="verify_signup"),

    path("forgot-password/", otp_views.forgot_password, name="forgot_password"),
    path("reset-with-otp/", otp_views.reset_with_otp, name="reset-with-otp"),
]

from django.conf import settings
from django.conf.urls.static import static

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
