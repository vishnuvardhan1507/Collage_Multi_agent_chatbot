from werkzeug.security import check_password_hash


def verify_password(password_hash: str, password: str) -> bool:
    return check_password_hash(password_hash, password)
