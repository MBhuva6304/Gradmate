# Gradmate

Gradmate is a Django-based degree progress and planning web app built to help students track completed coursework, view degree requirement progress, upload DPR PDFs, and plan their remaining path to graduation.

It is designed around a student profile, a course catalog, requirement groups/blocks, a planner-aware recommendation engine, and DPR-based import of completed and in-progress courses.

======================================
QUICK START
======================================

pip3 install -r requirements.txt
python3 manage.py migrate
python3 manage.py runserver

Open: http://127.0.0.1:8000/

======================================
FEATURES
======================================

Authentication
- User signup with email OTP verification
- Login via email (custom email-based authentication backend)
- Forgot password / reset with OTP flow
- Logout

Student Profile
- Program selection (BS CS)
- Catalog year (2023)
- Average and max credits per term

Dashboard
- Progress summary (completed, in-progress, planned, remaining credits)
- GE and major requirement group progress bars
- Estimated graduation term
- DPR upload modal

Degree Plan Page
- Interactive semester planner with multiple saved terms
- Auto-suggest a full degree plan
- Per-term course recommendations
- Add, move, and remove planned courses
- Per-term unit total with over-limit warning
- Backup and locked course lists
- Requirement section status tracker
- Warning badges for potentially unnecessary planned courses
- Notes per term
- Add or remove individual future terms
- Clear entire degree plan action

Audit Page
- Completed / in-progress / remaining unit totals
- Requirement group and section progress
- Course-level requirement detail per block
- Additional courses counting toward total units
- Below-100-level course section
- Upper Division GE helper section

Course Catalog
- Searchable and filterable course list
- Course detail pages with prerequisite and corequisite info
- Mark a course as completed or in-progress from the catalog
- Remove course status

DPR Upload
- PDF upload and text extraction
- Parsing of completed and in-progress course rows
- Auto-create missing catalog courses
- Clears and rebuilds active audit data on each upload

Settings Page
- Edit profile (name, email, program, catalog year)
- Change password
- View completed course list
- View DPR upload history with current/archive badges
- Download past DPR files
- Clear current audit data (keeps DPR history)

======================================
TECH STACK
======================================

- Python 3
- Django 4+
- SQLite
- HTML templates (Django template engine)
- Tailwind CSS via CDN
- JavaScript for lightweight UI behavior (HTMX-style partial rendering)
- PyPDF2 for DPR PDF text extraction

======================================
PROJECT STRUCTURE
======================================

Gradmate/
|-- campus/
|   |-- settings.py
|   |-- urls.py
|   `-- wsgi.py
|-- users/
|   |-- models.py              (all core models + StudentProfile logic)
|   |-- views.py               (all main views)
|   |-- forms.py
|   |-- admin.py
|   |-- backends.py            (email-based auth backend)
|   |-- middleware.py          (profile_required enforcement)
|   |-- otp_forms.py
|   |-- otp_views.py           (forgot password / OTP reset)
|   |-- requirement_engine.py  (course counting and block assignment helpers)
|   |-- management/            (custom management commands)
|   `-- migrations/
|-- accounts/
|-- progress/
|-- frontend/
|-- templates/
|   |-- base.html
|   |-- home.html
|   |-- dashboard.html
|   |-- degree_plan.html
|   |-- audit.html
|   |-- courses.html
|   |-- courses_list.html
|   |-- course_detail.html
|   |-- course_detail_modal.html
|   |-- settings.html
|   |-- upload_dpr.html
|   |-- login.html
|   |-- signup.html
|   |-- verify_email.html
|   |-- forgot_password.html
|   |-- reset_with_otp.html
|   `-- setup_profile.html
|-- media/
|-- db.sqlite3
|-- manage.py
`-- requirements.txt

======================================
CORE MODELS
======================================

Term
Represents a academic term (season + year). Supports next() chaining for graduation estimates.

StudentProfile
Per-user academic settings. Provides:
- completed / in-progress / planned course code lookups
- requirement progress and graduation estimate
- degree plan recommendation engine
- planner-aware prerequisite checking

Tag
Freeform label attached to courses.

Course
Catalog course with code, title, credits, subject, description, tags,
prerequisites, corequisites, prerequisite groups, and offered terms.

ProgramRequirement
Top-level requirement record for a (program, catalog_year) pair.

ProgramRequirementGroup
Legacy grouping model (still supported as fallback).

RequirementBlock
Granular requirement definition supporting:
- min_required count or unit threshold
- count_group exclusivity (no double-counting)
- allow_double_count override
- Senior Electives tracked by units

PathRule
Either/or path logic attached to a RequirementBlock.

PrerequisiteGroup
"Any N of these" prerequisite option groups for a course.

CompletedClass
Imported/saved completed course records for a student.

InProgressClass
Imported/saved in-progress course records for a student.

TermPlan
A single planned semester in a student's degree plan.

PlannedCourse
A course assigned to a TermPlan, with position ordering.

DPRUpload
Stores uploaded DPR PDF files with current/archive status.

EmailOTP
Stores one-time passwords for email verification and password reset.

======================================
MAIN VIEWS
======================================

dashboard           Progress summary, GE/major progress, grad estimate, DPR upload modal
degree_plan         Interactive semester planner with recommendations and warnings
audit               Full requirement audit with block-level course detail
courses             Searchable course catalog
course_detail       Course info, prerequisites, coreqs, mark status
upload_dpr          DPR PDF upload and import
settings_page       Profile, security, completed courses, DPR history
clear_audit_data    Delete CompletedClass/InProgressClass, keep DPR history
setup_profile       First-time program and catalog year setup
signup              Email-based signup with OTP verification
verify_signup       OTP verification step
forgot_password     Request OTP for password reset
reset_with_otp      Submit OTP and new password

Degree Plan AJAX endpoints:
- auto_suggest_degree_plan
- add_planned_course / add_planned_course_by_form / add_suggested_course_to_term
- remove_planned_course / move_planned_course / remove_term_plan
- save_term_notes / add_future_term / clear_degree_plan

Course status endpoints:
- mark_course_completed / mark_course_in_progress / remove_course_status

======================================
MAIN CONCEPTS
======================================

1. Requirement Engine
   Requirement progress is calculated by RequirementBlock, not by group totals.
   Matching is by course code (not course ID).
   Double-counting is controlled per block via count_group and allow_double_count.
   Senior Electives are tracked by total units, not row count.
   PathRules support either/or requirement paths.

2. Recommendation Engine
   recommend_next_term() scores remaining required courses by:
   - prerequisites satisfied
   - offered in the target term
   - requirement priority and urgency
   - upper division timing rules (60+ units required for UDGE courses)
   - corequisite pairing
   recommend_for_term_plan() applies the same scoring per saved semester.

3. Planner-Aware Prerequisites
   planner_prerequisites_satisfied() allows planned courses in earlier
   term plans to count toward prerequisites, enabling multi-semester
   dependency chains to be validated correctly before graduation.

4. DPR Import
   The DPR parser extracts text from the uploaded PDF and identifies:
   - completed course rows (grade present)
   - in-progress course rows (IP marker)
   Supports standard subjects (COMP 122), lab suffixes (256L),
   and spaced subjects (CH S 346). Missing courses are auto-created.

5. Credit Counting Rule
   Courses below level 100 are excluded from completed degree unit totals
   but still shown in in-progress totals and displayed in a separate
   below-100 section on the audit page.

======================================
SETUP INSTRUCTIONS
======================================

1. Clone the project

   git clone <https://github.com/MBhuva6304/Gradmate>
   cd Gradmate

2. Create and activate a virtual environment

   macOS / Linux:
   python3 -m venv venv
   source venv/bin/activate

   Windows:
   python -m venv venv
   venv\Scripts\activate

3. Install dependencies

   pip install -r requirements.txt

4. Run migrations

   python manage.py migrate

5. Start the development server

   python manage.py runserver

======================================
ADMIN SETUP
======================================

Use Django admin to configure:
- Terms (season + year)
- Tags
- Courses (with prerequisites, corequisites, prereq groups, offered terms)
- ProgramRequirement (one per program + catalog year)
- RequirementBlock (assigned to a ProgramRequirement)
- PathRule (optional either/or block rules)
- ProgramRequirementGroup (legacy fallback)

Important:
The student's program and catalog_year must match the ProgramRequirement
record that contains the relevant blocks and assigned courses.
If a student is on catalog year 2024, all blocks must be configured
under the matching 2024 requirement record.

======================================
SETTINGS PAGE FEATURES
======================================

Profile
- Full name, email, catalog year, program

Security
- Password change

DPR History
- Filename, upload time, current/archive badge
- View and download buttons

Completed Courses
- Compact preview with show/hide toggle

Audit Data Reset
- Clears CompletedClass and InProgressClass
- Clears profile.completed_codes
- Keeps DPR history files and all catalog/requirement data

======================================
DPR UPLOAD NOTES
======================================

- Completed and in-progress classes are imported on each upload
- Missing courses are auto-created in the catalog
- Duplicate parsed rows are deduplicated by course code
- Only one DPR record is "current" at a time; older uploads become archived

PDF parsing edge cases:
- Subjects split across tokens (e.g., CH S 346)
- Lab course suffixes (e.g., 256L)
- Duplicate rows from PDF extraction
- Non-standard transcript formatting

If totals do not match the DPR, recommended debugging:
- Print parsed completed_rows and ip_rows
- Print unique course codes after deduplication
- Compare saved CompletedClass/InProgressClass counts
