import re

SIGNED_URL_PATTERN = re.compile(r"https?://\S*[?]\S*(?:x-amz-|signature=|awsaccesskeyid=)\S*", re.IGNORECASE)
TOKEN_PATTERN = re.compile(r"ebs_[A-Za-z0-9_-]+")
SIGNED_FIELD_PATTERN = re.compile(
    r"(?i)((?:x-amz-(?:signature|credential|security-token)|signature|secret_access_key|secretaccesskey)=)[^\s&]+"
)
SENSITIVE_HEADERS = {
    "authorization",
    "cookie",
    "proxy-authorization",
    "x-amz-credential",
    "x-amz-security-token",
    "x-amz-signature",
}


def redact_secrets(value: str) -> str:
    """Remove bearer material without repeating it in another message."""
    value = SIGNED_URL_PATTERN.sub("[signed URL redacted]", value)
    value = TOKEN_PATTERN.sub("ebs_[redacted]", value)
    return SIGNED_FIELD_PATTERN.sub(r"\1[redacted]", value)


def redacted_headers(headers: dict[str, str], *, reveal: bool = False) -> dict[str, str]:
    if reveal:
        return dict(headers)
    return {
        name: "[redacted]" if name.lower() in SENSITIVE_HEADERS else value for name, value in headers.items()
    }
