from django import forms

class ForgotPasswordForm(forms.Form):
    email = forms.EmailField()

class ResetWithOTPForm(forms.Form):
    email = forms.EmailField()
    code  = forms.CharField(label="6-digit code", max_length=6, min_length=6)
    new_password1 = forms.CharField(widget=forms.PasswordInput)
    new_password2 = forms.CharField(widget=forms.PasswordInput)

    def clean(self):
        cleaned = super().clean()
        p1 = cleaned.get("new_password1")
        p2 = cleaned.get("new_password2")
        if p1 and p2 and p1 != p2:
            raise forms.ValidationError("Passwords do not match.")
        return cleaned
