from django.contrib.auth.tokens import default_token_generator
from django.utils.http import urlsafe_base64_encode
from django.contrib.sites.shortcuts import get_current_site
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils.encoding import force_bytes
from django.urls import reverse
import time
from django.conf import settings
from django.utils.html import strip_tags
from django.core.signing import TimestampSigner
from django.core.mail import EmailMultiAlternatives
from django.shortcuts import redirect, get_object_or_404
from functools import wraps
import uuid
import os
from django.utils.timezone import now
import datetime
from django.db.models.fields.files import FieldFile
import string
from django.db import transaction, IntegrityError
from django.http import HttpResponseBadRequest
from django.utils.text import slugify
from django.utils.http import url_has_allowed_host_and_scheme
import re
import random
import unicodedata
from django.core.files.uploadedfile import UploadedFile
from django.core.cache import cache
from typing import Pattern, Set
import json

# ?-----------------------------end imports-------------------


# * ==========================================================
# * String & Random Utilities
# * ==========================================================


def mask_email(email):
    """
    Masks an email address for privacy.
    """
    if not email or "@" not in email:
        return email
    name, domain = email.split("@", 1)
    if len(name) <= 1:
        return "*" + "@" + domain
    return name[0] + "*" * (len(name) - 1) + "@" + domain


def mask_string(s, visible_start=2, visible_end=2, mask_char="*"):
    """
    Masks the middle part of a string (e.g., for phone numbers or emails).

    Args:
        s (str): The string to be masked.
        visible_start (int): Number of characters to leave visible at the start.
        visible_end (int): Number of characters to leave visible at the end.
        mask_char (str): Character to use for masking.

    Returns:
        str: The masked string.

    Example:
        mask_string("1234567890") -> "12******90"
    """
    try:
        if not isinstance(s, str) or len(s) < visible_start + visible_end:
            return s  # not enough length to mask

        return (
            s[:visible_start]
            + (mask_char * (len(s) - visible_start - visible_end))
            + s[-visible_end:]
        )
    except Exception:
        return s  # fallback if something weird happens


def get_client_ip(request):
    """
    Returns the real client IP address, accounting for common proxy headers.
    """
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        # In case of multiple IPs, take the first one
        ip = x_forwarded_for.split(",")[0].strip()
    else:
        ip = request.META.get("REMOTE_ADDR")
    return ip


def humanize_timedelta(dt):
    """
    Returns a human-readable string for a timedelta or seconds.
    """
    if isinstance(dt, (int, float)):
        dt = datetime.timedelta(seconds=dt)
    seconds = int(dt.total_seconds())
    periods = [
        ("year", 60 * 60 * 24 * 365),
        ("month", 60 * 60 * 24 * 30),
        ("day", 60 * 60 * 24),
        ("hour", 60 * 60),
        ("minute", 60),
        ("second", 1),
    ]
    strings = []
    for period_name, period_seconds in periods:
        if seconds >= period_seconds:
            period_value, seconds = divmod(seconds, period_seconds)
            strings.append(
                f"{period_value} {period_name}{'s' if period_value > 1 else ''}"
            )
    return ", ".join(strings) or "0 seconds"


def random_string(length=12, chars=string.ascii_letters + string.digits):
    """
    Generates a random string of given length.
    """
    return "".join(random.choices(chars, k=length))


def unique_slugify(instance, value, slug_field_name="slug"):
    """
    Generates a unique slug for a model instance.
    """
    slug = slugify(value)
    ModelClass = instance.__class__
    unique_slug = slug
    num = 1
    while (
        ModelClass.objects.filter(**{slug_field_name: unique_slug})
        .exclude(pk=instance.pk)
        .exists()
    ):
        unique_slug = f"{slug}-{num}"
        num += 1
    setattr(instance, slug_field_name, unique_slug)
    return unique_slug


# * ==========================================================
# * URLS and api
# * ==========================================================


def request_rate_limit(
    request, key: str, limit: int = 5, time_window: int = 60
) -> bool:
    """
    Limits the number of requests per user or session for a given key within a time window.

    Args:
        request: Django request object.
        key (str): Identifier for the type of action (e.g., "login").
        limit (int): Number of allowed requests.
        window (int): Time window in seconds.

    Returns:
        bool: True if allowed, False if rate-limited.
    """
    if request.user.is_authenticated:
        identifier = str(request.user.pk)
    else:
        # Use session key or fallback to a random UUID if no session
        identifier = request.session.session_key or uuid.uuid4().hex

    cache_key = f"rate_limit:{key}:{identifier}"
    count = cache.get(cache_key, 0)

    if count >= limit:
        return False

    if count == 0:
        # First request: set with expiration
        cache.set(cache_key, 1, timeout=time_window)
    else:
        # Increment without resetting TTL
        cache.incr(cache_key)

    return True


def safe_redirect(request, url, fallback_url="/"):
    """
    Redirects to a URL only if it's safe; otherwise, redirects to fallback_url.
    """
    if url and url_has_allowed_host_and_scheme(url, allowed_hosts={request.get_host()}):
        return redirect(url)
    return redirect(fallback_url)


# * ==========================================================
# * Checkers
# * ==========================================================


def is_valid_json(json_str: str) -> bool:
    try:
        json.loads(json_str)
        return True
    except json.JSONDecodeError:
        return False


def is_safe_upload(file: UploadedFile, max_size_mb: int = 10) -> bool:
    """
    Validates file uploads (extension, MIME type, size).
    """
    allowed_extensions = {".jpg", ".png", ".pdf"}
    allowed_mime_types = {"image/jpeg", "image/png", "application/pdf"}

    # Check extension
    ext = os.path.splitext(file.name)[1].lower()
    if ext not in allowed_extensions:
        return False

    # Check MIME type
    if file.content_type not in allowed_mime_types:
        return False

    # Check size (convert MB to bytes)
    if file.size > max_size_mb * 1024 * 1024:
        return False

    return True


def contains_malicious_code(text: str) -> bool:
    """
    Checks for suspicious HTML/JS patterns.
    """
    patterns = [
        r"<script.*?>.*?</script>",
        r"onerror=",
        r"onload=",
        r"javascript:",
        r"&lt;script&gt;",
    ]
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def is_strong_password(password: str) -> bool:
    """
    Checks if a password is strong (min 8 chars, mixed case, numbers, symbols).
    """
    if len(password) < 8:
        return False
    if not re.search(r"[A-Z]", password):  # At least 1 uppercase
        return False
    if not re.search(r"[a-z]", password):  # At least 1 lowercase
        return False
    if not re.search(r"[0-9]", password):  # At least 1 digit
        return False
    if not re.search(r"[^A-Za-z0-9]", password):  # At least 1 symbol
        return False
    return True


def check_if_hashed(password: str, algorithm=None) -> bool:
    """
    - Returns True if the password appears to be already hashed ``algorithm$iterations$salt$hash``,
    - Optional : type of hashing in `pbkdf2_sha256` or `argon2` or `bcrypt_sha256`,
    - Otherwise returns False (indicating it's plaintext and needs hashing).
    """
    if not password:
        return False  # No password set

    if algorithm == "pbkdf2_sha256":
        # Refined regex for Django's pbkdf2_sha256 hash format and handle base64 characters like '+' and '='
        return bool(
            re.match(r"^pbkdf2_sha256\$\d+\$[a-zA-Z0-9]+\$[a-zA-Z0-9+/=]+$", password)
        )

    elif algorithm == "argon2":
        # Regex tailored for Argon2 format
        return bool(re.match(r"^argon2[a-z]*\$.+\$.+\$.+$", password))

    elif algorithm == "bcrypt_sha256":
        # Regex tailored for bcrypt format
        return bool(re.match(r"^bcrypt_sha256\$.+$", password))

    elif algorithm == None:
        # general purpose
        return bool(
            re.match(r"^[a-zA-Z0-9_]+\$\d+\$[a-zA-Z0-9]+\$[a-zA-Z0-9+/=]+$", password)
        )

    else:
        return False


class SpamDetector:

    def __init__(self):
        # Pre-compile regex patterns for better performance
        self.allowed_chars_pattern = re.compile(
            rf'^[0-9A-Za-zÀ-ÿêéèœàç\'"(),\.:\-!? \n\r]+$'
        )
        self.url_pattern = re.compile(
            r"""
            (?:https?://|www\.|ftp://)  # Protocols or www
            [^\s/$.?#]+\.[^\s]*        # Domain and rest of URL
        """,
            re.VERBOSE | re.IGNORECASE,
        )

        self.gibberish_pattern = re.compile(
            r"""
            (?:\b(?=\w*[A-Z])(?=\w*[a-z])[A-Za-z]{4,}\b[\s]*){3,}  # TitleCase words
            |\b\w*\d+\w*\b                                         # Words with numbers
            |[A-Za-z]{15,}                                          # Very long words
            |(?:[^a-zA-Z0-9\s]|_){3,}                               # Excessive special chars
        """,
            re.VERBOSE,
        )

        self.emoji_pattern = re.compile(
            r"[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF\U0001F700-\U0001F77F]"
        )

        # generic spam keywords
        self.spam_keywords: Set[str] = {
            "spam",
            "buy now",
            "discount",
            "offer",
            "cheap",
            "casino",
            "bitcoin",
            "crypto",
            "crypto currency",
            "cryptocurrency",
            "poker",
            "free",
            "bet",
            "100%",
            "investment",
            "click here",
            "below",
            "virus",
            "loan",
            "money",
            "profit",
            "win",
            "prize",
            "selected",
            "winner",
            "congratulations",
            "chatgpt",
            "artificial intelligence",
            "password",
            "reseller",
            "chat gpt",
            "price",
            "gambling",
            "gamble",
            "xbet",
            "1xbet",
            "discover",
            "nigga",
            "fuck",
            "bitch",
            "penis",
            "porn",
            "stimulate",
            "viagra",
            "sex",
            "vagina",
            "pussy",
            "paypal",
            "pay pal",
            "seo",
            "clicks",
            "views",
            "exclusive",
            "paye per click",
            "limited time",
            "urgent",
            "act now",
            "bonus",
            "tokens",
            "NFT",
            "promo",
            "trading",
            "forex",
            "earn",
            "rich",
            "million",
            "billion",
            "dollars",
            "USD",
            "BTC",
            "ETH",
        }

        self.whitelist: Set[str] = {"hello world"}

    # the main generic function
    def contains_spam(self, message: str) -> bool:
        # Normalize fancy punctuation and remove zero-width spaces
        message = unicodedata.normalize("NFKC", message)
        message = (
            message.replace("\u200b", "").replace("\u200c", "").replace("\u200d", "")
        )

        message_lower = message.lower()

        # Check allowed characters
        if not self.allowed_chars_pattern.fullmatch(message):
            return True

        # Check for URLs
        if self.url_pattern.search(message):
            return True

        # Check for gibberish (TitleCase, numbers in words, etc.)
        if self.gibberish_pattern.search(message):
            return True

        # Check for excessive emojis
        if len(self.emoji_pattern.findall(message)) > 3:
            return True

        # finally Check for spam keywords (excluding whitelisted terms)
        spam_keywords = self.spam_keywords - self.whitelist
        return any(
            re.search(rf"\b{re.escape(keyword)}\b", message_lower)
            for keyword in spam_keywords
        )

    def contains_contact_info(self, message: str) -> bool:
        # Email pattern
        email_pattern = r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"
        # International phone number pattern (basic)
        phone_pattern = r"(\+\d{1,3}[-\.\s]?)?(\d{3}[-\.\s]?){2}\d{4}"

        return bool(
            re.search(email_pattern, message, re.IGNORECASE)
            or re.search(phone_pattern, message)
        )

    def has_excessive_punctuation(self, message: str, max_allowed: int = 3) -> bool:
        return bool(re.search(r"(!|\?|\.){" + str(max_allowed + 1) + ",}", message))

    def is_all_caps(self, message: str, threshold: float = 0.8) -> bool:
        if len(message) < 5:  # Ignore short messages
            return False

        uppercase_chars = sum(1 for c in message if c.isupper())
        ratio = uppercase_chars / len(message)
        return ratio > threshold

    def has_repeated_chars(self, message: str, max_repeats: int = 3) -> bool:
        """example hellooo wwwooorrrllld"""
        return bool(re.search(r"(.)\1{" + str(max_repeats) + ",}", message))

    def has_suspicious_unicode(self, message: str) -> bool:
        # Check for Cyrillic, Arabic, or other non-Latin scripts mixed with Latin
        return bool(
            re.search(
                r"[\u0400-\u04FF\u0600-\u06FF]", message
            )  # Cyrillic & Arabic ranges
        )

    def contains_profanity(self, message: str, extra_keywords: Set[str]) -> bool:
        """
        this can be generic
        can extend the using https://github.com/zacanger/profane-words/blob/master/words.json
        """
        profanity_words = {
            "fuck",
            "shit",
            "asshole",
            "bitch",
            "cunt",
            "dick",
            "piss",
            "bastard",
            "nigga",
            "nigger",
            "whore",
            "slut",
            "fag",
            "retard",
            "pussy",
            "cock",
        }
        profanity_words.update(extra_keywords)
        message_lower = message.lower()
        return any(
            re.search(rf"\b{re.escape(word)}\b", message_lower)
            for word in profanity_words
        )

    def is_phishing(self, message: str, extra_keywords: Set[str]) -> bool:
        """this can be generic too"""
        phishing_keywords = {
            "verify your account",
            "login required",
            "suspicious activity",
            "account locked",
            "password expired",
            "click to confirm",
            "urgent action required",
            "bank alert",
            "IRS notice",
            "PayPal update",
        }
        phishing_keywords.update(extra_keywords)
        message_lower = message.lower()
        return any(keyword in message_lower for keyword in phishing_keywords)

    def has_hidden_chars(self, message: str) -> bool:
        zero_width_chars = {"\u200b", "\u200c", "\u200d", "\ufeff"}
        return any(char in message for char in zero_width_chars)

    def email_is_allowed(self, email: str, allowed_domains: Set[str]) -> bool:
        """
        Checks if an email's domain is in the allowed set.

        Args:
            email (str): The email address to check.
            allowed_domains (Set[str]): Set of allowed domains (e.g., {"gmail.com", "company.com"}).

        Returns:
            bool: True if the domain is allowed, False otherwise.
        """
        # Extract domain part after '@'
        parts = email.split("@")
        if len(parts) != 2:  # Invalid email format
            return False

        domain = parts[1].lower()  # Case-insensitive comparison
        return domain in allowed_domains


# * ==========================================================
# * Decorators
# * ==========================================================


def not_authenticated_required(view_func):
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect("accounts:profile")
        return view_func(request, *args, **kwargs)

    return _wrapped_view


def ajax_required(header_name="X-Requested-With", header_value="XMLHttpRequest"):
    """
    A decorator to ensure the request is an AJAX request.

    Args:
        header_value (str): The expected value of the header.
        header_name (str): The request header to check (default is 'x-requested-with').

    """

    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            if request.headers.get(header_name) != header_value:
                return HttpResponseBadRequest("AJAX request required.")
            return view_func(request, *args, **kwargs)

        return _wrapped_view

    return decorator


# * ==========================================================
# * Models and queries
# * ==========================================================


def model_to_full_dict(instance):
    """
    Returns a dict of all fields (including FileFields) for a model instance.
    """
    data = {}
    for field in instance._meta.get_fields():
        if hasattr(instance, field.name):
            value = getattr(instance, field.name)
            if isinstance(value, FieldFile):
                data[field.name] = value.url if value else None
            else:
                data[field.name] = value
    return data


def get_or_create_atomic(model, defaults=None, **kwargs):
    """
    Atomic get_or_create to avoid race conditions.
    """
    try:
        with transaction.atomic():
            return model.objects.get_or_create(defaults=defaults, **kwargs)
    except IntegrityError:
        return model.objects.get(**kwargs), False


def get_object_or_none(model, **kwargs):
    """
    Returns an object or None if not found.
    """
    try:
        return model.objects.get(**kwargs)
    except model.DoesNotExist:
        return None


def get_by_natural_key_or_404(model, *args):
    """
    Returns an object by natural key or raises 404.
    Note : in class Meta you must add natural_key_fields = ['name']

    """
    return get_object_or_404(model, **dict(zip(model.natural_key_fields, args)))


# * ==========================================================
# * mailing utilities
# * ==========================================================


def send_verification_email(request, user):
    # Generate the default token
    token = default_token_generator.make_token(user)

    # Get the current timestamp (seconds since the epoch)
    timestamp = int(time.time())
    signer = TimestampSigner()  # cleaner than Signer in this use case
    signed_timestamp = signer.sign(str(timestamp))

    # Encode user ID and timestamp
    uid = urlsafe_base64_encode(force_bytes(user.pk))

    # Prepare the URL with the timestamp included
    domain = get_current_site(request).domain
    link = f"https://{domain}{reverse('accounts:verify_email', kwargs={'uidb64': uid, 'token': token, 'signed_ts': signed_timestamp})}"

    subject = "Activer votre compte"
    html_message = render_to_string(
        "registration/verify_your_email.html",
        {
            "user": user,
            "activation_link": link,
        },
    )

    # Generate a plain text version of the email (for email clients that do not support HTML)
    plain_message = strip_tags(html_message)

    try:
        # message is mandatory as a fallback plain text
        send_mail(
            subject,
            message=plain_message,
            html_message=html_message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            fail_silently=True,
        )

    except Exception:
        pass


def send_2fa_code(request, user, subject="Your 2FA Code", email_template=None):
    """
    Generates a 4-digit 2FA code, stores session info, and sends it to the user's email.
    """

    code = str(random.randint(1000, 9999))
    request.session["2fa_user_id"] = user.id
    request.session["2fa_code"] = code
    request.session["2fa_created_at"] = int(time.time())

    html_message = render_to_string(
        f"{email_template}",
        {
            "code": code,
        },
    )

    plain_message = strip_tags(html_message)

    send_mail(
        subject=subject,
        message=plain_message,
        html_message=html_message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
    )


class EmailMATemplate:
    """
    - this is an efficient utility shortcut for creating EmailMultiAlternatives in case of
        a for loop mailing on a single SMTP connection (instead of send_mail)
    - Usage:

        from django.core.mail import get_connection

        connection = get_connection()  # Reuse the same SMTP connection
        connection.open()  # Explicitly open once for efficiency

        for user in users:

            EmailMATemplate(
                subject="Nouvel arrivage pour vous",
                template_path="emails/new_arrival_email.html", # relative to 'template/' path of course
                context={"user_name": user.get_full_name()},
                to=user.email
                connection=connection
            ).send()

        connection.close()

    """

    def __init__(
        self,
        subject: str,
        template_path: str,
        context: object = None,
        from_email: str = None,
        to: list = None,
        connection=None,
    ):

        if not to:
            raise ValueError("Recipient 'to' must be provided.")

        self.subject = subject
        self.template_path = template_path
        self.context = context or {}
        self.from_email = from_email or settings.DEFAULT_FROM_EMAIL
        self.to = to if isinstance(to, list) else [to]
        self.connection = connection

    def send(self):
        html_body = render_to_string(self.template_path, self.context)
        text_body = strip_tags(html_body)

        email = EmailMultiAlternatives(
            subject=self.subject,
            body=text_body,
            from_email=self.from_email,
            to=self.to,
            connection=self.connection,
        )
        email.attach_alternative(html_body, "text/html")
        email.send()
        return email


class GenerateUniqueFileName:
    """
    A callable class for Django's `upload_to` parameter that generates a unique file path
    for uploaded files, helping prevent filename conflicts and path length issues.

    Features:
    - Generates a short, UUID-based filename to avoid issues like `SuspiciousFileOperation`
    caused by overly long file names or unintended file overwrites.
    - Organizes uploaded files into subdirectories by year.
    - Preserves the original file extension.

    File Path Format:
        ``<base_folder>/<year>/<uuid>.<ext>``

    Example:
        ``
        image = models.ImageField(
            upload_to=GenerateUniqueFileName('product_images'),
            verbose_name="Main Product Image"
        )
        ``
        - **Note:** The `deconstruct()` method is required for Django to serialize callable objects (like this class) in migrations. Without it, `makemigrations` will fail.
        - ⚠️ Also, once this class is used in a migration, it's effectively "frozen" in that file. Deleting or renaming the class later — even if it's no longer used in your models — will break migrations unless you manually edit or fake them.

    """

    def __init__(self, base_folder):
        self.base_folder = base_folder

    def __call__(self, instance, filename):
        ext = os.path.splitext(filename)[1]
        unique_name = f"{uuid.uuid4().hex}{ext}"
        year = now().year
        return os.path.join(self.base_folder, str(year), unique_name)

    def deconstruct(self):
        """
        Tells Django how to serialize this callable for migrations.
        """
        # Return the full import path, no args/kwargs except the base_folder
        return (
            f"{self.__module__}.{self.__class__.__name__}",  # import path
            [self.base_folder],  # positional args
            {},  # keyword args
        )
