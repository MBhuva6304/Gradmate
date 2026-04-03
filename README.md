python3 manage.py migrate
python3 manage.py runserver

#Gradmate

Gradmate is a Django-based degree progress and audit web app built to help students track completed coursework, view degree requirement progress, upload DPR PDFs, and plan the remaining path to graduation.

It is designed around a student profile, a course catalog, requirement groups/blocks, and DPR-based import of completed and in-progress courses.

==================================================
FEATURES
==================================================

- User signup, login, email verification, and password reset support
- Student profile setup with program and catalog year
- Dashboard with progress summary and graduation estimate
- Degree plan page
- Course catalog search and filtering
- Course detail pages
- DPR PDF upload and parsing
- Audit page with:
  - completed / in-progress / remaining totals
  - requirement-group progress
  - course-level requirement detail
  - additional courses counting toward total units
  - below-100-level course section
- Settings page with:
  - profile editing
  - password change
  - completed course list
  - DPR upload history
  - clear current audit data action

==================================================
TECH STACK
==================================================

- Python
- Django
- SQLite
- HTML templates
- Tailwind via CDN
- JavaScript for lightweight UI behavior

==================================================
PROJECT STRUCTURE
==================================================

Gradmate/
|-- campus/
|   |-- settings.py
|   |-- urls.py
|   `-- wsgi.py
|-- users/
|   |-- models.py
|   |-- views.py
|   |-- forms.py
|   |-- admin.py
|   |-- backends.py
|   |-- otp_forms.py
|   |-- otp_views.py
|   `-- migrations/
|-- templates/
|   |-- base.html
|   |-- dashboard.html
|   |-- degree_plan.html
|   |-- courses.html
|   |-- courses_list.html
|   |-- course_detail.html
|   |-- course_detail_modal.html
|   |-- audit.html
|   |-- settings.html
|   |-- upload_dpr.html
|   |-- login.html
|   |-- signup.html
|   |-- verify_email.html
|   |-- setup_profile.html
|   `-- ...
|-- media/
|-- db.sqlite3
|-- manage.py
`-- requirements.txt

==================================================
MAIN CONCEPTS
==================================================

1. Student Profile
Each user has a student profile with:
- program
- catalog year
- average credits per term
- max credits next term
- completed code text fallback

2. Course Catalog
Courses are stored with:
- code
- title
- credits
- subject
- description
- tags
- prerequisites
- corequisites
- offered terms

3. Program Requirements
Requirements are organized by:
- ProgramRequirement
- RequirementBlock
- ProgramRequirementGroup
- PathRule

This supports:
- simple requirements
- "take any N of these" logic
- grouped requirement tracking
- requirement progress calculations

4. DPR Import
Users can upload a DPR PDF.

The system:
- extracts text from the PDF
- parses completed and in-progress course rows
- matches or creates course records
- clears current imported audit progress
- rebuilds CompletedClass and InProgressClass

5. Audit Behavior
The latest uploaded DPR controls the active audit data.

Old DPR files can still remain in history for viewing/downloading, but the system only uses the current imported classes for the live audit.

==================================================
SETUP INSTRUCTIONS
==================================================

1. Clone the project

git clone <your-repo-url>
cd Gradmate

2. Create and activate a virtual environment

macOS / Linux
python3 -m venv venv
source venv/bin/activate

Windows
python -m venv venv
venv\Scripts\activate

3. Install dependencies

pip install -r requirements.txt

4. Run migrations

python manage.py migrate

5. Start the development server

python manage.py runserver

Open:
http://127.0.0.1:8000/

==================================================
ADMIN SETUP
==================================================

Use Django admin to configure:
- courses
- tags
- program requirements
- requirement blocks
- requirement groups
- path rules
- offered terms

Important:
For the audit page to show section-level course details correctly, the student's:
- program
- catalog_year

must match the ProgramRequirement record that contains the relevant requirement blocks and assigned courses.

If the student is on catalog year 2022, the requirement blocks and courses must be configured under the BS_CS 2022 Requirements record, not another year.

==================================================
DPR UPLOAD NOTES
==================================================

Supported behavior:
- completed classes are imported from DPR
- in-progress classes are imported from DPR
- missing courses can be auto-created if needed
- duplicate parsed rows are deduplicated by course code

Important limitation:
PDF parsing depends on extracted text quality. Some DPR layouts may require parser adjustment, especially when:
- subjects are split like CH S
- rows are duplicated by PDF extraction
- transcript rows are not line formatted cleanly

Current parsing improvements:
The parser supports:
- standard subjects like COMP 122
- lab courses like COMP 256L
- spaced subjects like CH S 346
- completed grades and IP

==================================================
CURRENT CREDIT COUNTING RULE
==================================================

The project currently uses this behavior:
- completed courses below level 100 are not counted toward completed total degree units
- in-progress courses below level 100 are still included in in-progress totals
- below-100 courses are shown separately on the audit page

This behavior is implemented in the audit/dashboard credit summary logic.

==================================================
SETTINGS PAGE FEATURES
==================================================

The Settings page includes:

Profile
- full name
- email
- catalog year
- program

Security
- password change

DPR History
Shows past uploaded DPR files with:
- filename
- upload time
- current/archive badge
- view button
- download button

Completed Courses
Shows completed courses with a:
- compact preview
- show all / hide all button

Audit Data Reset
Allows the user to:
- clear current imported completed/in-progress audit data
- keep DPR history files

==================================================
RESET BEHAVIOR
==================================================

The "Clear Current Audit Data" feature:
- deletes CompletedClass
- deletes InProgressClass
- clears profile.completed_codes
- keeps DPR history
- does not delete catalog courses
- does not delete requirement configuration
- does not delete the user account

==================================================
CORE MODELS
==================================================

StudentProfile
Stores student-specific academic settings and provides:
- completed course tracking
- requirement progress
- graduation estimate
- recommendation logic

Course
Stores catalog course data.

ProgramRequirement
Stores the requirement record for a program and catalog year.

RequirementBlock
Stores grouped requirement logic like:
- A1 Oral Communication
- Upper Division Core
- Senior Electives

CompletedClass
Stores imported/saved completed course records for a student.

InProgressClass
Stores imported/saved in-progress course records for a student.

DPRUpload
Stores uploaded DPR PDF files.

==================================================
IMPORTANT VIEWS
==================================================

dashboard
Shows:
- progress summary
- estimated graduation term
- GE and major requirement progress

audit
Shows:
- current audit totals
- grouped requirement status
- requirement course details
- additional courses
- below-100 section

upload_dpr
Handles DPR PDF upload, parsing, and rebuilding of imported progress data.

settings_page
Shows:
- profile form
- security form
- completed courses
- DPR history

clear_audit_data
Clears current imported audit data while keeping DPR history.

==================================================
DEVELOPMENT NOTES
==================================================

Requirement matching
The audit page works best when requirement blocks are properly configured in Django admin with the correct program and catalog year.

History behavior
The app can keep DPR history files for viewing/downloading while still using only the latest upload to drive current audit data.

Parser behavior
If totals do not match the DPR exactly, the likely reasons are:
- PDF extraction duplication
- parser edge cases
- unusual subject formatting
- non-standard transcript rows

Recommended debugging steps:
- print parsed completed_rows and ip_rows
- print unique completed/in-progress course codes
- compare saved CompletedClass/InProgressClass with the DPR text

==================================================
FUTURE IMPROVEMENTS
==================================================

Possible future improvements include:
- full DPR restore / re-activate from history
- compare two DPR uploads
- stronger parser handling for complex transcript layouts
- export audit summary
- improved audit history management
- reset history option with confirmation
- audit status snapshots by upload date

==================================================
LICENSE
==================================================

This project is for educational and project development use.
Add your preferred license if you plan to distribute it publicly.
