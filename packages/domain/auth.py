import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass

import jwt
from sqlalchemy import select
from .config import JWT_SECRET
from .db import uid
from .errors import require
from .models import AuthSession, Membership, Organization, User


def password_hash(password):
    salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 310000).hex()
    return f"pbkdf2_sha256$310000${salt}${key}"


def verify_password(password, encoded):
    try:
        _, iterations, salt, key = encoded.split("$")
        return hmac.compare_digest(hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations)).hex(), key)
    except (ValueError, AttributeError):
        return False


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def issue_session(s, user):
    refresh = secrets.token_urlsafe(48)
    session = AuthSession(id=uid(), user_id=user.id, token_hash=token_hash(refresh), expires_at=time.time() + 7 * 86400)
    s.add(session)
    s.flush()
    access = jwt.encode({"sub": user.id, "sid": session.id, "iat": int(time.time()), "exp": int(time.time()) + 900}, JWT_SECRET, algorithm="HS256")
    return access, refresh


def authenticate(s, bearer):
    try:
        payload = jwt.decode(bearer.removeprefix("Bearer "), JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        require(False, 401, "UNAUTHENTICATED", "登录已过期，请重新登录")
    user = s.get(User, payload["sub"])
    session = s.get(AuthSession, payload["sid"])
    require(user and user.active and session and not session.revoked and session.expires_at > time.time(), 401, "UNAUTHENTICATED", "账号或会话已失效")
    return user


@dataclass
class Context:
    org_id: str
    user_id: str
    roles: list
    request_id: str = "system"

    def permit(self, *roles):
        require(bool(set(self.roles) & set(roles)), 403, "FORBIDDEN", "当前角色没有此操作权限")


def context(s, user, org_id, request_id, write=False):
    # Membership is checked for every request, including download and refresh.
    org_query = select(Organization).where(Organization.id == org_id)
    org = s.scalar(org_query.with_for_update() if write else org_query)
    member = s.scalar(select(Membership).where(Membership.org_id == org_id, Membership.user_id == user.id, Membership.active.is_(True)))
    require(org and member, 404, "NOT_FOUND", "资源不存在或无权访问")
    return Context(org_id, user.id, member.roles, request_id)


def scoped(s, cls, ident, ctx, lock=False):
    query = select(cls).where(cls.id == ident, cls.org_id == ctx.org_id)
    obj = s.scalar(query.with_for_update() if lock else query)
    require(obj is not None, 404, "NOT_FOUND", "资源不存在或无权访问")
    return obj
