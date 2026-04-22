from django.test import TestCase, Client
from django.contrib.auth import get_user_model
from django.urls import reverse

from users.models import (
    Term,
    StudentProfile,
    Course,
    ProgramRequirement,
    RequirementBlock,
    CompletedClass,
    InProgressClass,
    TermPlan,
    PlannedCourse,
)

User = get_user_model()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def make_user(email="test@example.com", password="testpass123"):
    return User.objects.create_user(username=email, email=email, password=password)


def make_profile(user, program="BS_CS", catalog_year=2024):
    return StudentProfile.objects.create(
        user=user, program=program, catalog_year=catalog_year
    )


def make_course(program="BS_CS", catalog_year=2024, code="COMP101",
                title="Intro", credits=3.0):
    return Course.objects.create(
        program=program, catalog_year=catalog_year,
        code=code, title=title, credits=credits,
    )


def make_term(year=2025, season="SP"):
    term, _ = Term.objects.get_or_create(year=year, season=season)
    return term


# ---------------------------------------------------------------------------
# Term model tests
# ---------------------------------------------------------------------------

class TermNextTest(TestCase):

    def test_spring_next_is_summer(self):
        sp = make_term(2025, "SP")
        su = sp.next()
        self.assertEqual(su.season, "SU")
        self.assertEqual(su.year, 2025)

    def test_summer_next_is_fall(self):
        su = make_term(2025, "SU")
        fa = su.next()
        self.assertEqual(fa.season, "FA")
        self.assertEqual(fa.year, 2025)

    def test_fall_next_is_spring_next_year(self):
        fa = make_term(2025, "FA")
        sp = fa.next()
        self.assertEqual(sp.season, "SP")
        self.assertEqual(sp.year, 2026)

    def test_chaining_three_steps_returns_same_season_next_year(self):
        sp = make_term(2024, "SP")
        result = sp.next().next().next()
        self.assertEqual(result.season, "SP")
        self.assertEqual(result.year, 2025)


# ---------------------------------------------------------------------------
# Course model tests
# ---------------------------------------------------------------------------

class CourseCountsForTotalUnitsTest(TestCase):

    def test_course_100_level_counts(self):
        c = make_course(code="COMP100")
        self.assertTrue(c.counts_for_total_units())

    def test_course_above_100_counts(self):
        c = make_course(code="COMP310")
        self.assertTrue(c.counts_for_total_units())

    def test_course_below_100_does_not_count(self):
        c = make_course(code="COMP099")
        self.assertFalse(c.counts_for_total_units())

    def test_course_zero_level_does_not_count(self):
        c = make_course(code="COMP000")
        self.assertFalse(c.counts_for_total_units())


# ---------------------------------------------------------------------------
# StudentProfile — course code lookups
# ---------------------------------------------------------------------------

class CompletedCourseCodesTest(TestCase):

    def setUp(self):
        self.user = make_user()
        self.profile = make_profile(self.user)
        self.course = make_course(code="COMP101")

    def test_empty_when_no_completed_courses(self):
        self.assertEqual(self.profile.completed_course_codes(), set())

    def test_returns_code_from_completed_class(self):
        CompletedClass.objects.create(profile=self.profile, course=self.course)
        self.assertIn("COMP101", self.profile.completed_course_codes())

    def test_codes_are_uppercased(self):
        CompletedClass.objects.create(profile=self.profile, course=self.course)
        codes = self.profile.completed_course_codes()
        for c in codes:
            self.assertEqual(c, c.upper())

    def test_includes_text_field_codes(self):
        self.profile.completed_codes = "MATH150, ENGL101"
        self.profile.save()
        codes = self.profile.completed_course_codes()
        self.assertIn("MATH150", codes)
        self.assertIn("ENGL101", codes)

    def test_caches_result_on_instance(self):
        CompletedClass.objects.create(profile=self.profile, course=self.course)
        codes_first = self.profile.completed_course_codes()
        # Mutate DB behind the scenes
        CompletedClass.objects.filter(profile=self.profile).delete()
        codes_second = self.profile.completed_course_codes()
        # Should return cached value, not re-query
        self.assertEqual(codes_first, codes_second)


class InProgressCourseCodesTest(TestCase):

    def setUp(self):
        self.user = make_user()
        self.profile = make_profile(self.user)
        self.course = make_course(code="COMP201")

    def test_empty_when_no_in_progress(self):
        self.assertEqual(self.profile.in_progress_course_codes(), set())

    def test_returns_code_from_in_progress_class(self):
        InProgressClass.objects.create(profile=self.profile, course=self.course)
        self.assertIn("COMP201", self.profile.in_progress_course_codes())

    def test_taken_course_codes_is_union(self):
        completed = make_course(code="COMP101")
        # self.course is already COMP201 from setUp
        CompletedClass.objects.create(profile=self.profile, course=completed)
        InProgressClass.objects.create(profile=self.profile, course=self.course)
        taken = self.profile.taken_course_codes()
        self.assertIn("COMP101", taken)
        self.assertIn("COMP201", taken)


class PlannedCourseCodesTest(TestCase):

    def setUp(self):
        self.user = make_user()
        self.profile = make_profile(self.user)
        self.course = make_course(code="COMP301")
        self.term = make_term(2026, "SP")

    def test_empty_when_no_planned_courses(self):
        self.assertEqual(self.profile.planned_course_codes(), set())

    def test_returns_planned_course_code(self):
        tp = TermPlan.objects.create(profile=self.profile, term=self.term, position=1)
        PlannedCourse.objects.create(term_plan=tp, course=self.course, position=1)
        self.assertIn("COMP301", self.profile.planned_course_codes())


# ---------------------------------------------------------------------------
# StudentProfile — prerequisite checking
# ---------------------------------------------------------------------------

class PrerequisitesSatisfiedTest(TestCase):

    def setUp(self):
        self.user = make_user()
        self.profile = make_profile(self.user)
        self.prereq = make_course(code="COMP101")
        self.course = make_course(code="COMP201")
        self.course.prerequisites.add(self.prereq)

    def test_prereq_not_satisfied_when_not_completed(self):
        self.assertFalse(self.profile.prerequisites_satisfied(self.course))

    def test_prereq_satisfied_after_completing_prereq(self):
        CompletedClass.objects.create(profile=self.profile, course=self.prereq)
        self.assertTrue(self.profile.prerequisites_satisfied(self.course))

    def test_any_mode_satisfied_with_one_of_two_prereqs(self):
        prereq2 = make_course(code="COMP102")
        self.course.prerequisites.add(prereq2)
        self.course.prereq_mode = "ANY"
        self.course.save()
        CompletedClass.objects.create(profile=self.profile, course=self.prereq)
        self.assertTrue(self.profile.prerequisites_satisfied(self.course))

    def test_all_mode_not_satisfied_with_only_one_of_two_prereqs(self):
        prereq2 = make_course(code="COMP102")
        self.course.prerequisites.add(prereq2)
        self.course.prereq_mode = "ALL"
        self.course.save()
        CompletedClass.objects.create(profile=self.profile, course=self.prereq)
        self.assertFalse(self.profile.prerequisites_satisfied(self.course))

    def test_no_prereqs_always_satisfied(self):
        standalone = make_course(code="COMP300")
        self.assertTrue(self.profile.prerequisites_satisfied(standalone))


# ---------------------------------------------------------------------------
# StudentProfile — planner-aware prerequisite checking
# ---------------------------------------------------------------------------

class PlannerPrerequisitesSatisfiedTest(TestCase):

    def setUp(self):
        self.user = make_user()
        self.profile = make_profile(self.user)
        self.prereq = make_course(code="COMP101")
        self.course = make_course(code="COMP201")
        self.course.prerequisites.add(self.prereq)

        self.term1 = make_term(2025, "SP")
        self.term2 = make_term(2025, "FA")
        self.tp1 = TermPlan.objects.create(
            profile=self.profile, term=self.term1, position=1
        )
        self.tp2 = TermPlan.objects.create(
            profile=self.profile, term=self.term2, position=2
        )

    def test_prereq_in_earlier_term_satisfies_check(self):
        PlannedCourse.objects.create(term_plan=self.tp1, course=self.prereq, position=1)
        self.assertTrue(
            self.profile.planner_prerequisites_satisfied(self.course, self.tp2)
        )

    def test_prereq_in_same_term_does_not_satisfy(self):
        PlannedCourse.objects.create(term_plan=self.tp2, course=self.prereq, position=1)
        self.assertFalse(
            self.profile.planner_prerequisites_satisfied(self.course, self.tp2)
        )

    def test_prereq_completed_satisfies_any_term(self):
        CompletedClass.objects.create(profile=self.profile, course=self.prereq)
        self.assertTrue(
            self.profile.planner_prerequisites_satisfied(self.course, self.tp1)
        )

    def test_no_prereqs_always_satisfied(self):
        standalone = make_course(code="COMP300")
        self.assertTrue(
            self.profile.planner_prerequisites_satisfied(standalone, self.tp1)
        )


# ---------------------------------------------------------------------------
# StudentProfile — term plan unit totals
# ---------------------------------------------------------------------------

class TermPlanUnitsTest(TestCase):

    def setUp(self):
        self.user = make_user()
        self.profile = make_profile(self.user)
        self.term = make_term(2025, "SP")
        self.tp = TermPlan.objects.create(
            profile=self.profile, term=self.term, position=1
        )

    def test_zero_units_when_no_courses(self):
        self.assertEqual(self.profile.term_plan_total_units(self.tp), 0.0)

    def test_sums_credits_correctly(self):
        c1 = make_course(code="COMP101", credits=3.0)
        c2 = make_course(code="COMP201", credits=4.0)
        PlannedCourse.objects.create(term_plan=self.tp, course=c1, position=1)
        PlannedCourse.objects.create(term_plan=self.tp, course=c2, position=2)
        self.assertEqual(self.profile.term_plan_total_units(self.tp), 7.0)

    def test_total_planned_units_across_all_terms(self):
        term2 = make_term(2025, "FA")
        tp2 = TermPlan.objects.create(profile=self.profile, term=term2, position=2)
        c1 = make_course(code="COMP101", credits=3.0)
        c2 = make_course(code="COMP201", credits=3.0)
        PlannedCourse.objects.create(term_plan=self.tp, course=c1, position=1)
        PlannedCourse.objects.create(term_plan=tp2, course=c2, position=1)
        self.assertEqual(self.profile.total_planned_units(), 6.0)


# ---------------------------------------------------------------------------
# StudentProfile — remaining required courses
# ---------------------------------------------------------------------------

class RemainingRequiredCoursesTest(TestCase):

    def setUp(self):
        self.user = make_user()
        self.profile = make_profile(self.user)
        self.req = ProgramRequirement.objects.create(
            program="BS_CS", catalog_year=2024, required_credits=120
        )
        self.block = RequirementBlock.objects.create(
            requirement=self.req,
            name="Core",
            min_required=2,
            count_group="group1",
        )
        self.c1 = make_course(code="COMP101")
        self.c2 = make_course(code="COMP201")
        self.c3 = make_course(code="COMP301")
        self.block.courses.add(self.c1, self.c2, self.c3)

    def test_returns_empty_when_no_requirement_configured(self):
        profile2 = make_profile(make_user("other@x.com"), catalog_year=9999)
        self.assertFalse(profile2.remaining_required_courses().exists())

    def test_all_courses_remaining_when_nothing_completed(self):
        remaining = list(self.profile.remaining_required_courses())
        codes = {c.code for c in remaining}
        self.assertIn("COMP101", codes)
        self.assertIn("COMP201", codes)

    def test_completed_course_excluded_from_remaining(self):
        CompletedClass.objects.create(profile=self.profile, course=self.c1)
        CompletedClass.objects.create(profile=self.profile, course=self.c2)
        remaining = list(self.profile.remaining_required_courses())
        codes = {c.code for c in remaining}
        self.assertNotIn("COMP101", codes)
        self.assertNotIn("COMP201", codes)

    def test_in_progress_course_excluded_from_remaining(self):
        InProgressClass.objects.create(profile=self.profile, course=self.c1)
        remaining = list(self.profile.remaining_required_courses())
        codes = {c.code for c in remaining}
        self.assertNotIn("COMP101", codes)

    def test_block_satisfied_courses_no_longer_needed(self):
        # Complete min_required=2 courses; block is satisfied, no more needed
        CompletedClass.objects.create(profile=self.profile, course=self.c1)
        CompletedClass.objects.create(profile=self.profile, course=self.c2)
        remaining = list(self.profile.remaining_required_courses())
        codes = {c.code for c in remaining}
        # c3 was in block but block is now done — should not appear
        self.assertNotIn("COMP301", codes)


# ---------------------------------------------------------------------------
# Requirement group progress
# ---------------------------------------------------------------------------

class RequirementGroupProgressTest(TestCase):

    def setUp(self):
        self.user = make_user()
        self.profile = make_profile(self.user)
        self.req = ProgramRequirement.objects.create(
            program="BS_CS", catalog_year=2024, required_credits=120
        )
        self.block = RequirementBlock.objects.create(
            requirement=self.req,
            name="Core",
            min_required=1,
            count_group="group1",
        )
        self.course = make_course(code="COMP101")
        self.block.courses.add(self.course)

    def _get_block(self, progress, name="Core"):
        return next((r for r in progress if r["name"] == name), None)

    def test_block_shows_zero_completed_initially(self):
        progress = self.profile.requirement_group_progress_with_planned()
        row = self._get_block(progress)
        self.assertIsNotNone(row)
        self.assertEqual(float(row["completed"]), 0.0)

    def test_completed_course_increments_completed(self):
        CompletedClass.objects.create(profile=self.profile, course=self.course)
        progress = self.profile.requirement_group_progress_with_planned()
        row = self._get_block(progress)
        self.assertEqual(float(row["completed"]), 1.0)

    def test_planned_course_increments_planned(self):
        term = make_term(2026, "SP")
        tp = TermPlan.objects.create(profile=self.profile, term=term, position=1)
        PlannedCourse.objects.create(term_plan=tp, course=self.course, position=1)
        progress = self.profile.requirement_group_progress_with_planned()
        row = self._get_block(progress)
        self.assertEqual(float(row["planned"]), 1.0)
        self.assertEqual(float(row["completed"]), 0.0)

    def test_in_progress_course_increments_in_progress(self):
        InProgressClass.objects.create(profile=self.profile, course=self.course)
        progress = self.profile.requirement_group_progress_with_planned()
        row = self._get_block(progress)
        self.assertEqual(float(row["in_progress"]), 1.0)


# ---------------------------------------------------------------------------
# View tests — authentication and page load
# ---------------------------------------------------------------------------

class AuthRedirectTest(TestCase):

    def test_dashboard_redirects_unauthenticated(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_degree_plan_redirects_unauthenticated(self):
        response = self.client.get(reverse("degree_plan"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_audit_redirects_unauthenticated(self):
        response = self.client.get(reverse("audit"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_logged_in_without_profile_redirects_to_setup(self):
        user = make_user("noprofile@x.com")
        self.client.force_login(user)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("setup-profile", response["Location"])


class PageLoadTest(TestCase):

    def setUp(self):
        self.user = make_user()
        self.profile = make_profile(self.user)
        self.client.force_login(self.user)

    def test_dashboard_loads(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)

    def test_degree_plan_loads(self):
        response = self.client.get(reverse("degree_plan"))
        self.assertEqual(response.status_code, 200)

    def test_audit_loads(self):
        response = self.client.get(reverse("audit"))
        self.assertEqual(response.status_code, 200)

    def test_courses_page_loads(self):
        response = self.client.get(reverse("courses"))
        self.assertEqual(response.status_code, 200)

    def test_settings_page_loads(self):
        response = self.client.get(reverse("settings"))
        self.assertEqual(response.status_code, 200)

    def test_degree_plan_loads_with_requirements_configured(self):
        req = ProgramRequirement.objects.create(
            program="BS_CS", catalog_year=2024, required_credits=120
        )
        block = RequirementBlock.objects.create(
            requirement=req, name="Core", min_required=1, count_group="group1"
        )
        course = make_course(code="COMP101")
        block.courses.add(course)
        response = self.client.get(reverse("degree_plan"))
        self.assertEqual(response.status_code, 200)

    def test_degree_plan_loads_with_planned_courses(self):
        term = make_term(2026, "SP")
        tp = TermPlan.objects.create(
            profile=self.profile, term=term, position=1
        )
        course = make_course(code="COMP101")
        PlannedCourse.objects.create(term_plan=tp, course=course, position=1)
        response = self.client.get(reverse("degree_plan"))
        self.assertEqual(response.status_code, 200)


class SettingsPageNameTest(TestCase):
    """Verify the settings URL name matches what the view expects."""

    def setUp(self):
        self.user = make_user()
        make_profile(self.user)
        self.client.force_login(self.user)

    def test_settings_url_resolves(self):
        from django.urls import resolve
        match = resolve("/settings/")
        self.assertEqual(match.url_name, "settings")
