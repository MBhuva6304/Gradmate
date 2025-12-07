import datetime
from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.contrib.auth import get_user_model
from django.utils.text import slugify
from django.utils import timezone
from .models import StudentProfile

User = get_user_model()

# Tailwind helper class for inputs
TAILWIND_INPUT = "w-full rounded-lg border border-slate-300 px-3 py-2"

class EmailAuthenticationForm(AuthenticationForm):
    username = forms.EmailField(label="Email", widget=forms.EmailInput(attrs={"autofocus": True}))

class OTPForm(forms.Form):
    code = forms.CharField(
        label="Verification code",
        min_length=6,
        max_length=6,
        widget=forms.TextInput(attrs={
            "autocomplete": "one-time-code",
            "inputmode": "numeric",
            "class": "w-full h-12 rounded-xl border border-gray-300 bg-white px-4 "
                     "outline-none focus:border-blue-600 focus:ring-2 focus:ring-blue-200"
        }),
        help_text="Enter the 6-digit code we emailed you.",
    )

class ProfileSettingsForm(forms.Form):
    full_name = forms.CharField(label="Full name", max_length=150)
    # Email is displayed but not editable/savable
    email = forms.EmailField(label="Email", disabled=True, required=False)
    catalog_year = forms.IntegerField(label="Catalog year", min_value=2000, max_value=2100)
    program = forms.ChoiceField(label="Major", choices=StudentProfile.PROGRAM_CHOICES)

    def __init__(self, user, profile, *args, **kwargs):
        super().__init__(*args, **kwargs)
        full = (user.get_full_name() or "").strip()
        self.fields["full_name"].initial = full if full else (user.username or "")
        self.fields["email"].initial = user.email
        self.fields["email"].disabled = True
        self.fields["catalog_year"].initial = profile.catalog_year
        self.fields["program"].initial = profile.program

    def clean_email(self):
        # Ignore any posted value; keep original.
        return self.initial.get("email")

    def save(self, user, profile):
        name = self.cleaned_data["full_name"].strip()
        parts = name.split()
        user.first_name = " ".join(parts[:-1]) if len(parts) > 1 else name
        user.last_name = parts[-1] if len(parts) > 1 else ""
        user.save(update_fields=["first_name", "last_name"])

        profile.catalog_year = self.cleaned_data["catalog_year"]
        profile.program = self.cleaned_data["program"]
        profile.save(update_fields=["catalog_year", "program"])
        return user, profile

class SignUpForm(UserCreationForm):
    # Full name + email (styled)
    full_name = forms.CharField(
        label="Full Name",
        max_length=150,
        required=True,
        widget=forms.TextInput(attrs={
            "class": "w-full h-12 rounded-xl border border-gray-300 bg-white px-4 "
                     "outline-none focus:border-blue-600 focus:ring-2 focus:ring-blue-200"
        })
    )
    email = forms.EmailField(
        required=True,
        widget=forms.EmailInput(attrs={
            "class": "w-full h-12 rounded-xl border border-gray-300 bg-white px-4 "
                     "outline-none focus:border-blue-600 focus:ring-2 focus:ring-blue-200"
        })
    )
    completed_codes = forms.CharField(
        label="Completed classes (course codes)",
        required=False,
        widget=forms.Textarea(attrs={
            "rows": 3,
            "placeholder": "e.g., CS101, MATH201, ENGL101",
            "class": "w-full rounded-xl border border-gray-300 bg-white px-4 py-3 "
                     "outline-none focus:border-blue-600 focus:ring-2 focus:ring-blue-200"
        }),
        help_text="Separate codes with commas or spaces.",
    )

    # Program + Catalog Year (styled)
    program = forms.ChoiceField(
        label="Program",
        choices=StudentProfile.PROGRAM_CHOICES,
        widget=forms.Select(attrs={
            "class": "w-full h-12 rounded-xl border border-gray-300 bg-white px-4 "
                     "outline-none focus:border-blue-600 focus:ring-2 focus:ring-blue-200"
        })
    )
    _year = datetime.date.today().year
    catalog_year = forms.ChoiceField(
        label="Catalog Year",
        choices=[(y, y) for y in range(_year, _year - 7, -1)],
        initial=_year,
        widget=forms.Select(attrs={
            "class": "w-full h-12 rounded-xl border border-gray-300 bg-white px-4 "
                     "outline-none focus:border-blue-600 focus:ring-2 focus:ring-blue-200"
        }),
    )

    # hidden username; we auto-generate it
    username = forms.CharField(required=False, widget=forms.HiddenInput())

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("email",)  # password1/2 come from the base class

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # style password widgets
        for name in ("password1", "password2"):
            self.fields[name].widget = forms.PasswordInput(attrs={
                "class": "w-full h-12 rounded-xl border border-gray-300 bg-white px-4 "
                         "outline-none focus:border-blue-600 focus:ring-2 focus:ring-blue-200"
            })

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip().lower()
        if not email:
            raise forms.ValidationError("Email is required.")
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with this email already exists.")
        return email

    def _make_username(self, full_name: str, email: str) -> str:
        base = slugify(full_name).replace("-", "") or (email.split("@")[0] or "user").lower()
        candidate, i = base, 0
        while User.objects.filter(username__iexact=candidate).exists():
            i += 1
            candidate = f"{base}{i}"
        return candidate

    def save(self, commit=True):
        user = super().save(commit=False)
        email = self.cleaned_data["email"].strip().lower()
        full_name = self.cleaned_data["full_name"].strip()
        parts = full_name.split(None, 1)
        user.first_name = parts[0] if parts else ""
        user.last_name  = parts[1] if len(parts) > 1 else ""
        user.email = email
        user.username = self._make_username(full_name, email)
        if commit:
            user.save()
        return user

class ProfileSetupForm(forms.ModelForm):
    class Meta:
        model = StudentProfile
        fields = ["program", "catalog_year", "avg_credits_per_term", "max_credits_next_term"]
        labels = {
            "program": "Major",
            "catalog_year": "Catalog year",
            "avg_credits_per_term": "Avg credits per term",
            "max_credits_next_term": "Max credits next term",
        }
        widgets = {
            "program": forms.Select(attrs={"class": TAILWIND_INPUT}),
            "catalog_year": forms.NumberInput(attrs={"class": TAILWIND_INPUT, "min": 2000, "max": 2100}),
            "avg_credits_per_term": forms.NumberInput(attrs={"class": TAILWIND_INPUT, "min": 1, "max": 25}),
            "max_credits_next_term": forms.NumberInput(attrs={"class": TAILWIND_INPUT, "min": 1, "max": 25}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["catalog_year"].initial = timezone.localdate().year
        self.fields["avg_credits_per_term"].initial = 15
        self.fields["max_credits_next_term"].initial = 15
