from django.contrib.auth.backends import ModelBackend
from django.contrib.auth import get_user_model

class EmailBackend(ModelBackend):
    """
    Authenticate using email + password instead of username.
    Falls back to ModelBackend for perms and is_active checks.
    """
    def authenticate(self, request, username=None, password=None, **kwargs):
        User = get_user_model()
        email = (kwargs.get("email") or username or "").strip().lower()
        if not email or not password:
            return None
        try:
            user = User.objects.get(email__iexact=email)
        except User.DoesNotExist:
            return None
        except User.MultipleObjectsReturned:
            # If duplicates exist, pick the first deterministically
            user = User.objects.filter(email__iexact=email).order_by("id").first()
        if user and user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
